"""Beanstock MoomooPaperBroker -- write adapter, MOCK-ONLY for this build.

Architecture position:

    Claude research -> TradeProposal -> schema validation
        -> deterministic risk engine -> DecisionResult -> ExecutionIntent
        -> BrokerGateway -> MoomooPaperBroker -> SIMULATED US account ONLY

See broker/MOOMOO_PAPER_ORDER_CONTRACT.md for the verified REST contract
(Place/Modify/Cancel Order, Max Buy/Sell Quantity) this module implements
against. Every endpoint path/field traces to that document; nothing here
is guessed.

Write permission gate
----------------------
BEANSTOCK_PAPER_WRITE_ENABLED (env var, or the `write_enabled` constructor
override used by tests) defaults to False. While False, this class
performs every validation, re-check, and order-request construction a
real submission would need -- so the full pipeline is provably correct
-- but the actual HTTP POST to place/cancel an order is never made. The
gate is checked once, as the very last step before that POST, in
_submit_order_request(); nothing upstream of it depends on the gate's
value, and there is no other code path that reaches the HTTP layer.

Read access (account/positions/quotes/market state) is delegated to a
composed broker.moomoo_readonly.MoomooReadOnlyBroker instance -- this
class does not re-implement or subclass it (subclassing a class named
"read-only" to add writes would be confusing), and does not re-verify
the read contract, already covered by broker/MOOMOO_API_CONTRACT.md and
its own test suite. The small amount of HTTP/envelope plumbing needed
for THIS class's own write calls (_guard_path/_get_json/_post_json/
_interpret_response/_unwrap_envelope) is intentionally duplicated from
MoomooReadOnlyBroker rather than refactored out from under it -- it is
small, stable, and already independently tested there; reaching into
that class's private methods across the class boundary, or refactoring
already-shipped code purely for DRY, was judged riskier than a ~50-line
duplication for this change. Every exception type and shared constant
(ALLOWED_PATH_PREFIXES, US_MARKET_ID, ORDER_SIDE_MAP, ORDER_STATUS_MAP,
OPEN_ORDERS_PATH_TEMPLATE) is imported, never redefined.

Order history / idempotency
-----------------------------
get_orders()/get_order() return THIS broker's own local journal (like
broker.fake_paper.FakePaperBroker), not moomoo's remote order list --
moomoo's records don't carry Beanstock's audit_reference at all, and
broker.gateway.BrokerGateway's duplicate-fill check specifically needs
that field. Local idempotency (_processed_audit_references) is marked
only once a real HTTP place-order call actually succeeds -- never on a
write-disabled rejection, so the exact same approved ExecutionIntent
remains submittable again once BEANSTOCK_PAPER_WRITE_ENABLED is later
turned on. Never marked, never trusted from anywhere but this broker's
own successful-submission bookkeeping.

Fractional shares
------------------
Not confirmed supported by the simulated Place Order endpoint (see the
contract doc). This broker only accepts instrument_type == "stock" and
always computes a whole-share quantity via floor division -- it never
rounds up, and never sends a fractional qty. instrument_type ==
"fractional_share" is rejected outright.
"""

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
from typing import Callable, Optional
import os
import uuid

from broker.base import Account, Broker, Order, Position
from broker.http_transport import (
    HttpResponse,
    HttpTransport,
    TransportError,
    TransportTimeout,
    UrllibHttpTransport,
)
from broker.moomoo_readonly import (
    ALLOWED_PATH_PREFIXES,
    OPEN_ORDERS_PATH_TEMPLATE,
    US_MARKET_ID,
    LiveAccountRejectedError,
    MalformedResponseError,
    MoomooApiError,
    MoomooAuthenticationError,
    MoomooBrokerError,
    MoomooRateLimitError,
    MoomooReadOnlyBroker,
    MoomooServerError,
    MoomooTimeoutError,
    UnexpectedStatusError,
)
from execution.intent import ExecutionIntent
from risk.validator import PAPER_MODES

# Verified against broker/MOOMOO_PAPER_ORDER_CONTRACT.md.
CANCEL_ORDER_PATH_TEMPLATE = "/api/v1.0/sim-trade/{acc_id}/orders/{order_id}/cancel"
MODIFY_ORDER_PATH_TEMPLATE = "/api/v1.0/sim-trade/{acc_id}/orders/{order_id}/modify"  # not wired -- see contract doc
MAX_BUY_SELL_PATH_TEMPLATE = "/api/v1.0/sim-trade/{acc_id}/max-buy-sell"

ORDER_TYPE_MARKET = 3
ORDER_SIDE_BUY = 1
ORDER_SIDE_SELL = 2

# Fractional shares unverified for this endpoint -- see contract doc.
# "stock" only, never "fractional_share".
ALLOWED_INSTRUMENT_TYPES = {"stock"}
ALLOWED_ACTIONS = {"BUY", "ADD", "REDUCE", "EXIT"}
EXPOSURE_INCREASING_ACTIONS = {"BUY", "ADD"}

ORDER_STATUS_PENDING = "PENDING"
ORDER_STATUS_FILLED = "FILLED"
ORDER_STATUS_REJECTED = "REJECTED"
ORDER_STATUS_CANCELED = "CANCELED"

REJECT_DUPLICATE = "REJECT_DUPLICATE"
REJECT_WRITE_DISABLED = "REJECT_WRITE_DISABLED"

# Same placeholder defaults as broker.gateway.BrokerGateway, for the
# same reason (see that module's docstring) -- not market-derived.
DEFAULT_MAX_INTENT_AGE_SECONDS = 300.0
DEFAULT_MAX_QUOTE_AGE_SECONDS = 60.0

ENV_WRITE_ENABLED = "BEANSTOCK_PAPER_WRITE_ENABLED"


class PaperWriteDisabledError(MoomooBrokerError, PermissionError):
    """Raised by cancel_order() -- an administrative action outside the
    AI decision path, like broker.fake_paper.FakePaperBroker's own
    cancel_order()/close_position() docstring describes -- when
    BEANSTOCK_PAPER_WRITE_ENABLED is False. submit_execution_intent()
    never raises this; it returns a REJECTED Order instead (rejection
    prefix REJECT_WRITE_DISABLED), consistent with how every other
    business-rule rejection in this pipeline is represented.
    """


def _to_decimal(value) -> Optional[Decimal]:
    if value is None or isinstance(value, bool):
        return None
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return d if d.is_finite() else None


def _is_valid_price(price: Optional[Decimal]) -> bool:
    return price is not None and price.is_finite() and price > 0


def _env_flag_enabled(raw: Optional[str]) -> bool:
    return (raw or "").strip().lower() in ("1", "true", "yes", "on")


@dataclass
class _AdminCloseIntent:
    """Minimal stand-in used only by close_position()'s internal call
    into the same order-construction path submit_execution_intent()
    uses -- never accepted by submit_execution_intent() itself, which
    requires a real ExecutionIntent. Mirrors
    broker.fake_paper._AdminCloseIntent.
    """

    ticker: str
    action: str = "EXIT"
    dollar_amount: Optional[Decimal] = None
    audit_reference: Optional[str] = None


class MoomooPaperBroker(Broker):
    def __init__(
        self,
        *,
        access_token_provider: Callable[[], str],
        http_transport: Optional[HttpTransport] = None,
        base_host: str = "https://webapi.moomoo.com",
        simulated_account_id: Optional[str] = None,
        write_enabled: Optional[bool] = None,
        max_intent_age_seconds: float = DEFAULT_MAX_INTENT_AGE_SECONDS,
        max_quote_age_seconds: float = DEFAULT_MAX_QUOTE_AGE_SECONDS,
        request_timeout_seconds: float = 10.0,
    ):
        self._transport: HttpTransport = http_transport or UrllibHttpTransport(base_host)
        self._read_broker = MoomooReadOnlyBroker(
            access_token_provider=access_token_provider,
            http_transport=self._transport,
            base_host=base_host,
            simulated_account_id=simulated_account_id,
            request_timeout_seconds=request_timeout_seconds,
        )
        self._access_token_provider = access_token_provider
        self._timeout = request_timeout_seconds

        if write_enabled is None:
            write_enabled = _env_flag_enabled(os.environ.get(ENV_WRITE_ENABLED))
        self._write_enabled = bool(write_enabled)

        self.max_intent_age_seconds = max_intent_age_seconds
        self.max_quote_age_seconds = max_quote_age_seconds

        # Same plain-mutable-attribute pattern as broker.gateway.BrokerGateway
        # -- the project's own daily/weekly review process sets these,
        # here as well as on the gateway, as defense in depth.
        self.safe_mode = False
        self.daily_loss_breached = False
        self.weekly_drawdown_breached = False

        self._orders: dict = {}
        self._order_sequence: list = []
        self._processed_audit_references: set = set()
        self._audit_log: list = []

    @property
    def write_enabled(self) -> bool:
        return self._write_enabled

    # -----------------------------------------------------------------
    # HTTP plumbing (duplicated from MoomooReadOnlyBroker -- see module
    # docstring for why).
    # -----------------------------------------------------------------

    def _guard_path(self, path: str) -> None:
        if not any(path.startswith(prefix) for prefix in ALLOWED_PATH_PREFIXES):
            raise LiveAccountRejectedError(
                f"Refusing to call {path!r}: it is outside the sim-trade/quote "
                "allowlist and could only be a live-account endpoint."
            )

    def _authorized_headers(self) -> dict:
        try:
            token = self._access_token_provider()
        except Exception:
            raise MoomooAuthenticationError("Failed to obtain an access token.") from None
        if not token or not isinstance(token, str):
            raise MoomooAuthenticationError("Access token provider returned no usable token.")
        return {"Authorization": f"Bearer {token}"}

    def _get_json(self, path: str, params: Optional[dict] = None) -> dict:
        self._guard_path(path)
        headers = self._authorized_headers()
        try:
            response = self._transport.get(path, params=params, headers=headers, timeout=self._timeout)
        except TransportTimeout:
            raise MoomooTimeoutError(f"Request to {path} timed out.") from None
        except TransportError as exc:
            raise MoomooServerError(f"Transport error contacting {path} ({type(exc).__name__}).") from None
        return self._interpret_response(path, response)

    def _post_json(self, path: str, json_body: dict) -> dict:
        self._guard_path(path)
        headers = self._authorized_headers()
        try:
            response = self._transport.post(path, json_body=json_body, headers=headers, timeout=self._timeout)
        except TransportTimeout:
            raise MoomooTimeoutError(f"Request to {path} timed out.") from None
        except TransportError as exc:
            raise MoomooServerError(f"Transport error contacting {path} ({type(exc).__name__}).") from None
        return self._interpret_response(path, response)

    def _interpret_response(self, path: str, response: HttpResponse) -> dict:
        status = response.status_code
        if status in (401, 403):
            raise MoomooAuthenticationError(f"Authentication rejected for {path} (HTTP {status}).")
        if status == 429:
            raise MoomooRateLimitError(f"Rate limited by moomoo for {path} (HTTP 429).")
        if 500 <= status < 600:
            raise MoomooServerError(f"moomoo server error for {path} (HTTP {status}).")
        if not (200 <= status < 300):
            raise UnexpectedStatusError(f"Unexpected HTTP status {status} for {path}.")
        try:
            payload = response.json()
        except ValueError:
            raise MalformedResponseError(f"Malformed JSON response from {path}.") from None
        if not isinstance(payload, dict):
            raise MalformedResponseError(f"Unexpected response shape from {path} (expected an object).")
        return payload

    def _unwrap_envelope(self, path: str, payload: dict) -> dict:
        if "ret_code" not in payload:
            raise MalformedResponseError(f"Response from {path} is missing the documented 'ret_code' envelope field.")
        ret_code = payload.get("ret_code")
        if ret_code != 0:
            raise MoomooApiError(f"{path} returned ret_code={ret_code!r} (ret_msg={payload.get('ret_msg')!r}).")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise MalformedResponseError(f"Response from {path} is missing the documented 'data' object.")
        return data

    # -----------------------------------------------------------------
    # Read delegation -- MoomooReadOnlyBroker is the source of truth for
    # account/positions/quotes/market state. Never reimplemented here.
    # -----------------------------------------------------------------

    def get_account(self) -> Account:
        return self._read_broker.get_account()

    def get_positions(self) -> list:
        return self._read_broker.get_positions()

    def get_position(self, ticker: str) -> Optional[Position]:
        return self._read_broker.get_position(ticker)

    def get_quote(self, ticker: str) -> Optional[Decimal]:
        return self._read_broker.get_quote(ticker)

    def get_quote_timestamp(self, ticker: str) -> Optional[datetime]:
        return self._read_broker.get_quote_timestamp(ticker)

    def get_market_status(self) -> str:
        return self._read_broker.get_market_status()

    def resolve_simulated_us_account_id(self) -> str:
        return self._read_broker.resolve_simulated_us_account_id()

    # -----------------------------------------------------------------
    # Orders / audit trail -- LOCAL journal (like FakePaperBroker), not
    # moomoo's remote order list. See module docstring.
    # -----------------------------------------------------------------

    def get_orders(self) -> list:
        return [self._orders[oid] for oid in self._order_sequence]

    def get_order(self, order_id: str) -> Optional[Order]:
        return self._orders.get(order_id)

    def get_audit_log(self) -> list:
        """Full attempted-submission audit trail, including the exact
        constructed order request body for every attempt -- filled,
        rejected, and write-disabled alike. Never contains a token or
        an Authorization header.
        """
        return list(self._audit_log)

    # -----------------------------------------------------------------
    # Order submission
    # -----------------------------------------------------------------

    def submit_execution_intent(self, intent: "ExecutionIntent") -> Order:
        """The ONLY way to place a (simulated) order. Accepts ONLY a
        real ExecutionIntent instance produced by
        execution.intent.create_execution_intent() and already passed
        through broker.gateway.BrokerGateway -- a TradeProposal, a
        dict, a string, a DecisionResult, or any other object is a
        programming error, not a legitimate rejected attempt, so it
        raises TypeError immediately rather than being logged as an
        order.
        """
        if not isinstance(intent, ExecutionIntent):
            raise TypeError(
                "submit_execution_intent() requires an ExecutionIntent produced by "
                "execution.intent.create_execution_intent(); got "
                f"{type(intent).__name__!r}"
            )
        return self._process_intent(intent)

    def _process_intent(self, intent) -> Order:
        audit_reference = intent.audit_reference
        now = datetime.now(timezone.utc)

        # --- idempotency: never resubmit an already-successfully-sent
        #     audit_reference. ---
        if audit_reference is not None and audit_reference in self._processed_audit_references:
            return self._reject(
                intent,
                reason=(
                    f"{REJECT_DUPLICATE}: audit_reference {audit_reference!r} has "
                    "already been submitted once."
                ),
            )

        # --- defense in depth: never trust the upstream decision alone,
        #     even though BrokerGateway already checked all of this. ---
        if intent.execution_allowed is not True:
            return self._reject(intent, reason="execution_allowed is not True.")

        mode = (intent.account_mode or "").strip().upper()
        if mode not in PAPER_MODES:
            return self._reject(
                intent, reason=f"account_mode={intent.account_mode!r} is not PAPER/SIMULATED."
            )

        instrument_type = (intent.instrument_type or "").strip().lower()
        if instrument_type not in ALLOWED_INSTRUMENT_TYPES:
            return self._reject(
                intent,
                reason=(
                    f"instrument_type={intent.instrument_type!r} is not supported -- "
                    "MoomooPaperBroker only trades whole-share 'stock'. Fractional-share "
                    "execution is not confirmed supported by moomoo's simulated Place "
                    "Order endpoint (broker/MOOMOO_PAPER_ORDER_CONTRACT.md); failing "
                    "closed rather than guessing or rounding."
                ),
            )

        action = (intent.action or "").strip().upper()
        if action not in ALLOWED_ACTIONS:
            return self._reject(
                intent,
                reason=f"action={intent.action!r} is not a broker-executable action "
                "(HOLD/DO_NOTHING never reach the broker).",
            )

        # --- SAFE_MODE / daily-loss / weekly-drawdown gates -- only
        #     block exposure-increasing actions, exactly like
        #     BrokerGateway. These are this broker's OWN copy of the
        #     safety flags (see class docstring), independently set. ---
        if action in EXPOSURE_INCREASING_ACTIONS:
            if self.safe_mode:
                return self._reject(
                    intent,
                    reason="SAFE_MODE is active; new BUY/ADD orders are blocked "
                    "(REDUCE/EXIT remain permitted).",
                )
            if self.daily_loss_breached:
                return self._reject(
                    intent,
                    reason="Daily loss threshold has been breached; BUY/ADD orders "
                    "are blocked until the next daily review.",
                )
            if self.weekly_drawdown_breached:
                return self._reject(
                    intent,
                    reason="Weekly drawdown threshold has been breached; BUY/ADD "
                    "orders are blocked until the next weekly review.",
                )

        ticker = self._normalize_ticker(intent.ticker)
        if ticker is None:
            return self._reject(intent, reason="Missing or invalid ticker.")

        # --- stale intent protection ---
        intent_age = self._age_seconds(intent.created_at, now)
        if intent_age is None:
            return self._reject(intent, reason="intent created_at is missing or unparseable.", ticker=ticker)
        if intent_age > self.max_intent_age_seconds:
            return self._reject(
                intent,
                reason=f"Intent is {intent_age:.1f}s old, exceeding the "
                f"{self.max_intent_age_seconds:.1f}s safe window; re-evaluation required.",
                ticker=ticker,
            )

        # From here on, infrastructure failures (auth/malformed
        # response/an attempted live-endpoint call/etc.) are allowed to
        # propagate as exceptions -- they mean the system is broken or
        # misconfigured, not that this specific trade is invalid.
        account_id = self._read_broker.resolve_simulated_us_account_id()

        # --- stale / missing quote protection ---
        quote = self._read_broker.get_quote(ticker)
        if not _is_valid_price(quote):
            return self._reject(intent, reason=f"No valid current quote for {ticker!r}.", ticker=ticker)
        quote_ts = self._read_broker.get_quote_timestamp(ticker)
        quote_age = self._age_seconds_from_datetime(quote_ts, now)
        if quote_age is None:
            return self._reject(
                intent, reason=f"Quote for {ticker!r} has no timestamp; cannot verify freshness.", ticker=ticker
            )
        if quote_age > self.max_quote_age_seconds:
            return self._reject(
                intent,
                reason=f"Quote for {ticker!r} is {quote_age:.1f}s old, exceeding the "
                f"{self.max_quote_age_seconds:.1f}s freshness window.",
                ticker=ticker,
            )

        # --- current account/position state, re-fetched fresh -- never
        #     trust what the intent assumed. ---
        account = self._read_broker.get_account()
        position = self._read_broker.get_position(ticker)
        current_quantity = position.quantity if position else Decimal("0")

        if action in ("BUY", "ADD"):
            return self._construct_buy_or_add(intent, account_id, ticker, quote, account, position, current_quantity, action)
        if action == "REDUCE":
            return self._construct_reduce(intent, account_id, ticker, quote, position, current_quantity)
        return self._construct_exit(intent, account_id, ticker, quote, position, current_quantity)

    # -----------------------------------------------------------------
    # BUY / ADD
    # -----------------------------------------------------------------

    def _construct_buy_or_add(self, intent, account_id, ticker, quote, account, position, current_quantity, action):
        if action == "ADD" and (position is None or current_quantity <= 0):
            return self._reject(intent, reason="ADD requires an existing position; none found.", ticker=ticker)
        if action == "BUY" and position is not None and current_quantity > 0:
            return self._reject(
                intent, reason="BUY requires no existing position; use ADD to add to one.", ticker=ticker
            )

        dollar_amount = _to_decimal(intent.dollar_amount)
        if dollar_amount is None or dollar_amount <= 0:
            return self._reject(
                intent,
                reason=f"dollar_amount must be a positive number, got {intent.dollar_amount!r}.",
                ticker=ticker,
            )

        if dollar_amount > account.cash:
            return self._reject(
                intent,
                reason=f"Insufficient cash: requested ${dollar_amount}, available ${account.cash}.",
                ticker=ticker,
                requested_dollar_amount=dollar_amount,
            )

        # Whole shares only -- floor, never round up, never fractional.
        quantity = (dollar_amount / quote).to_integral_value(rounding=ROUND_FLOOR)
        if quantity < 1:
            return self._reject(
                intent,
                reason=(
                    f"${dollar_amount} at a quote of ${quote} rounds down to 0 whole shares; "
                    "MoomooPaperBroker never trades fractional shares (unverified support)."
                ),
                ticker=ticker,
                requested_dollar_amount=dollar_amount,
            )
        estimated_cost = quantity * quote
        if estimated_cost > account.cash:
            return self._reject(
                intent,
                reason=f"Rounded order cost ${estimated_cost} exceeds available cash ${account.cash}.",
                ticker=ticker,
                requested_dollar_amount=dollar_amount,
            )

        request_body = self._build_order_request(ticker, ORDER_SIDE_BUY, quantity, intent.audit_reference)
        return self._submit_order_request(
            intent, account_id, ticker, request_body, requested_dollar_amount=dollar_amount
        )

    # -----------------------------------------------------------------
    # REDUCE
    # -----------------------------------------------------------------

    def _construct_reduce(self, intent, account_id, ticker, quote, position, current_quantity):
        if position is None or current_quantity <= 0:
            return self._reject(intent, reason="REDUCE requires an existing position; none found.", ticker=ticker)

        dollar_amount = _to_decimal(intent.dollar_amount)
        if dollar_amount is None or dollar_amount <= 0:
            return self._reject(
                intent,
                reason=f"dollar_amount must be a positive number, got {intent.dollar_amount!r}.",
                ticker=ticker,
            )

        quantity = (dollar_amount / quote).to_integral_value(rounding=ROUND_FLOOR)
        if quantity < 1:
            return self._reject(
                intent,
                reason=f"${dollar_amount} at a quote of ${quote} rounds down to 0 whole shares.",
                ticker=ticker,
                requested_dollar_amount=dollar_amount,
            )
        if quantity > current_quantity:
            return self._reject(
                intent,
                reason=(
                    f"REDUCE would sell {quantity} shares against a held quantity of "
                    f"{current_quantity}; refusing to create a short position."
                ),
                ticker=ticker,
                requested_dollar_amount=dollar_amount,
            )

        request_body = self._build_order_request(ticker, ORDER_SIDE_SELL, quantity, intent.audit_reference)
        return self._submit_order_request(
            intent, account_id, ticker, request_body, requested_dollar_amount=dollar_amount
        )

    # -----------------------------------------------------------------
    # EXIT
    # -----------------------------------------------------------------

    def _construct_exit(self, intent, account_id, ticker, quote, position, current_quantity):
        if position is None or current_quantity <= 0:
            return self._reject(intent, reason="EXIT requires an existing position; none found.", ticker=ticker)

        # EXIT always closes at most the full remaining position -- never
        # partial, never more than what is actually held.
        quantity = current_quantity.to_integral_value(rounding=ROUND_FLOOR)
        if quantity != current_quantity or quantity < 1:
            return self._reject(
                intent,
                reason=(
                    f"Held quantity {current_quantity} is not a whole number of shares; "
                    "MoomooPaperBroker cannot construct a fractional-share EXIT order."
                ),
                ticker=ticker,
            )

        request_body = self._build_order_request(ticker, ORDER_SIDE_SELL, quantity, intent.audit_reference)
        proceeds_estimate = quantity * quote
        return self._submit_order_request(
            intent, account_id, ticker, request_body, requested_dollar_amount=proceeds_estimate
        )

    # -----------------------------------------------------------------
    # Order request construction / submission
    # -----------------------------------------------------------------

    def _build_order_request(self, ticker: str, order_side: int, quantity: Decimal, audit_reference: Optional[str]) -> dict:
        body = {
            "market": US_MARKET_ID,
            "symbol": ticker,
            "order_type": ORDER_TYPE_MARKET,
            "order_side": order_side,
            "qty": str(int(quantity)),
        }
        if audit_reference:
            body["text"] = f"beanstock:{audit_reference}"[:100]
        return body

    def max_buy_quantity(self, ticker: str) -> Optional[Decimal]:
        """Read-only pre-flight check against moomoo's own Max Buy/Sell
        Quantity endpoint (broker/MOOMOO_PAPER_ORDER_CONTRACT.md #4).
        Not called automatically by order construction in this build --
        the account-cash check already satisfies "sufficient simulated
        buying power exists"; this is available for a future, more
        broker-authoritative sizing check without expanding this
        build's required test surface.
        """
        account_id = self._read_broker.resolve_simulated_us_account_id()
        normalized = self._normalize_ticker(ticker)
        if normalized is None:
            return None
        path = MAX_BUY_SELL_PATH_TEMPLATE.format(acc_id=account_id)
        payload = self._get_json(path, params={"symbol": normalized, "order_type": ORDER_TYPE_MARKET})
        data = self._unwrap_envelope(path, payload)
        return _to_decimal(data.get("max_cash_buy_qty_round_lot"))

    def _submit_order_request(self, intent, account_id, ticker, request_body, *, requested_dollar_amount) -> Order:
        # The write-permission gate -- the LAST check, after full
        # validation and construction, so the constructed request is
        # always provably correct even while writes are disabled.
        if not self._write_enabled:
            return self._record(
                intent,
                status=ORDER_STATUS_REJECTED,
                ticker=ticker,
                requested_dollar_amount=requested_dollar_amount,
                rejection_reason=(
                    f"{REJECT_WRITE_DISABLED}: {ENV_WRITE_ENABLED} is False; this order "
                    "was fully validated and constructed but never sent."
                ),
                constructed_request=request_body,
            )

        path = OPEN_ORDERS_PATH_TEMPLATE.format(acc_id=account_id)
        payload = self._post_json(path, request_body)
        data = self._unwrap_envelope(path, payload)
        order_id = data.get("order_id")
        if not order_id:
            raise MalformedResponseError("Place-order response is missing 'order_id'.")
        order_id = str(order_id)

        # Mark processed ONLY now -- a write-disabled rejection above
        # never reaches here, so the same audit_reference stays
        # submittable once writes are later enabled.
        if intent.audit_reference is not None:
            self._processed_audit_references.add(intent.audit_reference)

        status, fill_price, filled_quantity = self._lookup_order_status(order_id)

        return self._record(
            intent,
            status=status,
            ticker=ticker,
            order_id=order_id,
            requested_dollar_amount=requested_dollar_amount,
            fill_price=fill_price,
            filled_quantity=filled_quantity,
            constructed_request=request_body,
        )

    def _lookup_order_status(self, order_id: str):
        """The Place Order response carries no fill/status information
        at all (broker/MOOMOO_PAPER_ORDER_CONTRACT.md #1) -- status can
        only be learned by reading it back via the already-verified
        Today's/History Orders endpoints (MoomooReadOnlyBroker).
        """
        order = self._read_broker.get_order(order_id)
        if order is None:
            return ORDER_STATUS_PENDING, None, None
        return order.status, order.fill_price, order.filled_quantity

    # -----------------------------------------------------------------
    # Cancel / admin close
    # -----------------------------------------------------------------

    def cancel_order(self, order_id: str) -> Order:
        order = self._orders.get(order_id)
        if order is None:
            raise KeyError(f"No such order_id: {order_id!r}")
        if order.status != ORDER_STATUS_PENDING:
            return order
        if not self._write_enabled:
            raise PaperWriteDisabledError(
                f"MoomooPaperBroker.cancel_order() cannot make a write HTTP request: "
                f"{ENV_WRITE_ENABLED} is False."
            )
        account_id = self._read_broker.resolve_simulated_us_account_id()
        path = CANCEL_ORDER_PATH_TEMPLATE.format(acc_id=account_id, order_id=order_id)
        payload = self._post_json(path, {})
        self._unwrap_envelope(path, payload)
        canceled = replace(order, status=ORDER_STATUS_CANCELED)
        self._orders[order_id] = canceled
        return canceled

    def close_position(self, ticker: str) -> Order:
        """Administrative full close, independent of the AI decision
        pipeline -- not something the AI research/risk-engine layers
        call. Still enforces every invariant EXIT does, including the
        write-permission gate.
        """
        normalized = self._normalize_ticker(ticker)
        fake_intent = _AdminCloseIntent(ticker=normalized or ticker)
        if normalized is None:
            return self._reject(fake_intent, reason="Missing or invalid ticker.")

        account_id = self._read_broker.resolve_simulated_us_account_id()
        quote = self._read_broker.get_quote(normalized)
        if not _is_valid_price(quote):
            return self._reject(fake_intent, reason=f"No valid current quote for {normalized!r}.", ticker=normalized)
        position = self._read_broker.get_position(normalized)
        current_quantity = position.quantity if position else Decimal("0")
        return self._construct_exit(fake_intent, account_id, normalized, quote, position, current_quantity)

    # -----------------------------------------------------------------
    # Bookkeeping / helpers
    # -----------------------------------------------------------------

    def _normalize_ticker(self, ticker) -> Optional[str]:
        if not (isinstance(ticker, str) and ticker.strip()):
            return None
        return ticker.strip().upper()

    def _age_seconds(self, created_at: Optional[str], now: datetime) -> Optional[float]:
        if not created_at:
            return None
        try:
            parsed = datetime.fromisoformat(created_at)
        except (TypeError, ValueError):
            return None
        return self._age_seconds_from_datetime(parsed, now)

    def _age_seconds_from_datetime(self, moment: Optional[datetime], now: datetime) -> Optional[float]:
        if moment is None:
            return None
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return (now - moment).total_seconds()

    def _new_order_id(self) -> str:
        return f"paper-order-{uuid.uuid4().hex[:12]}"

    def _record(
        self,
        intent,
        status: str,
        ticker: Optional[str] = None,
        order_id: Optional[str] = None,
        requested_dollar_amount: Optional[Decimal] = None,
        fill_price: Optional[Decimal] = None,
        filled_quantity: Optional[Decimal] = None,
        rejection_reason: Optional[str] = None,
        constructed_request: Optional[dict] = None,
    ) -> Order:
        resolved_order_id = order_id or self._new_order_id()
        created_at = datetime.now(timezone.utc).isoformat()

        order = Order(
            order_id=resolved_order_id,
            ticker=ticker if ticker is not None else getattr(intent, "ticker", None),
            action=getattr(intent, "action", None),
            status=status,
            requested_quantity=None,
            requested_dollar_amount=(
                requested_dollar_amount
                if requested_dollar_amount is not None
                else _to_decimal(getattr(intent, "dollar_amount", None))
            ),
            fill_price=fill_price,
            filled_quantity=filled_quantity,
            realized_pnl=None,
            audit_reference=getattr(intent, "audit_reference", None),
            rejection_reason=rejection_reason,
            created_at=created_at,
        )
        self._orders[resolved_order_id] = order
        self._order_sequence.append(resolved_order_id)

        self._audit_log.append(
            {
                "timestamp": created_at,
                "audit_reference": order.audit_reference,
                "order_id": resolved_order_id,
                "ticker": order.ticker,
                "action": order.action,
                "status": order.status,
                "requested_dollar_amount": order.requested_dollar_amount,
                "fill_price": order.fill_price,
                "filled_quantity": order.filled_quantity,
                "rejection_reason": order.rejection_reason,
                "write_enabled": self._write_enabled,
                "constructed_request": constructed_request,
            }
        )
        return order

    def _reject(
        self,
        intent,
        reason: str,
        ticker: Optional[str] = None,
        requested_dollar_amount: Optional[Decimal] = None,
    ) -> Order:
        return self._record(
            intent,
            status=ORDER_STATUS_REJECTED,
            ticker=ticker,
            requested_dollar_amount=requested_dollar_amount,
            rejection_reason=reason,
        )
