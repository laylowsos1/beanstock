"""Beanstock MoomooReadOnlyBroker.

A read-only adapter from Beanstock's broker.base.Broker interface onto
moomoo's official REST OpenAPI (https://open.moomoo.com/api/...,
served from https://webapi.moomoo.com), scoped permanently to a single
SIMULATED US-stock account. See broker/MOOMOO_API_CONTRACT.md for the
full endpoint-by-endpoint verification this module implements against
that documentation -- every path and field name below traces back to a
specific page there, not to a guess or to the older OpenD/protobuf SDK
docs at openapi.moomoo.com (a different product).

This module implements no order-placement, no order-modification, and
no order-cancellation endpoint anywhere -- submit_execution_intent(),
cancel_order(), and close_position() are hardcoded to raise
ReadOnlyBrokerError unconditionally. There is no constructor flag,
config value, or code path that turns write access on.

Live-account safety boundary
------------------------------
The verified REST API has no unified account list with a live/simulated
discriminator field -- simulated accounts live entirely under
/api/v1.0/sim-trade/*, auto-created on first call; live business
accounts live under /api/v1.0/accounts/* and /api/v1.0/trading/*, which
have no simulated variant at all. So "never touch a live account" is
enforced here as a hardcoded path-prefix allowlist (_ALLOWED_PATH_PREFIXES)
checked on every single request -- not a field check on a response that
could be malformed or spoofed. No code path in this class can construct
a request outside that allowlist.

Account selection
-----------------
No account identifier is ever hardcoded. On first use this adapter calls
GET /api/v1.0/sim-trade/accounts, keeps only entries with market_id == 2
(US, per the documented enum), and requires exactly one match:

    - zero matches                                     -> NoSimulatedAccountError
    - more than one match, no simulated_account_id set -> AmbiguousSimulatedAccountError
    - a configured simulated_account_id not in the set -> NoSimulatedAccountError

Credentials
-----------
This adapter takes an `access_token_provider` callable (zero args,
returns a bearer token string) rather than any OAuth machinery itself --
see auth/moomoo_oauth.py for a PKCE-based OAuth 2.1 client whose
get_valid_access_token() is meant to be passed in here. No token is ever
logged, printed, or included in an exception message; only HTTP status
codes and request paths appear in errors.

Stale-quote handling
---------------------
get_quote_timestamp() returns the server-provided data_time from the
most recent get_quote() call for that ticker (milliseconds since epoch,
per the documented Stock Quote contract), or None if no quote has been
fetched -- it never substitutes datetime.now().
"""

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Callable, Optional

from broker.base import Account, Broker, Order, Position
from broker.http_transport import (
    HttpResponse,
    HttpTransport,
    TransportError,
    TransportTimeout,
    UrllibHttpTransport,
)

DEFAULT_BASE_HOST = "https://webapi.moomoo.com"

# Verified against https://open.moomoo.com/api/... -- see
# broker/MOOMOO_API_CONTRACT.md for the doc page backing each one.
ACCOUNTS_PATH = "/api/v1.0/sim-trade/accounts"
CASH_INFO_PATH_TEMPLATE = "/api/v1.0/sim-trade/{acc_id}/cash-info"
POSITIONS_PATH_TEMPLATE = "/api/v1.0/sim-trade/{acc_id}/positions"
OPEN_ORDERS_PATH_TEMPLATE = "/api/v1.0/sim-trade/{acc_id}/orders"
HISTORY_ORDERS_PATH_TEMPLATE = "/api/v1.0/sim-trade/{acc_id}/history-orders"
QUOTE_PATH = "/api/v1.0/quote/stock-quote"
MARKET_STATE_PATH = "/api/v1.0/quote/market-state"

# Every request this adapter ever makes must start with one of these.
# This -- not a field on a response -- is what makes a live account
# unreachable from this class.
ALLOWED_PATH_PREFIXES = ("/api/v1.0/sim-trade/", "/api/v1.0/quote/")

US_MARKET_ID = 100  # corrected against a real live API response -- see broker/MOOMOO_API_CONTRACT.md

ACCOUNT_MODE = "SIMULATED"

# https://open.moomoo.com/api/sim-trade/order-list -- documented `side` enum.
ORDER_SIDE_MAP = {
    1: "BUY",
    2: "SELL",
    3: "SHORT_SELL",
    4: "BUY_BACK",
}

# https://open.moomoo.com/api/sim-trade/order-list -- documented `status`
# enum, mapped onto Beanstock's narrower PENDING/FILLED/REJECTED/CANCELED
# vocabulary. Partially Filled (3) is mapped to FILLED as the closest
# available status -- Beanstock's Order model has no PARTIAL state; this
# is a deliberate adapter simplification, not part of the moomoo contract.
ORDER_STATUS_MAP = {
    2: "PENDING",  # Submitted
    3: "FILLED",  # Partially Filled (simplification -- see above)
    4: "FILLED",  # Filled
    5: "CANCELED",  # Cancelled
    6: "REJECTED",  # Rejected
}


# ---------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------


class MoomooBrokerError(Exception):
    """Base for every error this adapter raises. Messages are always
    built from HTTP status codes, request paths, and known-safe JSON
    fields -- never from a raw response body, and never from a token.
    """


class ReadOnlyBrokerError(MoomooBrokerError, PermissionError):
    """Raised unconditionally by every write-shaped method on this
    broker (submit_execution_intent, cancel_order, close_position).
    """


class MoomooAuthenticationError(MoomooBrokerError):
    pass


class MoomooRateLimitError(MoomooBrokerError):
    pass


class MoomooServerError(MoomooBrokerError):
    """5xx response, or a transport-level failure contacting moomoo."""


class MoomooTimeoutError(MoomooBrokerError):
    pass


class UnexpectedStatusError(MoomooBrokerError):
    pass


class MalformedResponseError(MoomooBrokerError):
    """Response was not valid JSON, used the wrong envelope shape, or
    was missing/had invalid required fields. Includes an unrecognized
    order side/status enum value -- this adapter never guesses at the
    meaning of an unknown code.
    """


class MoomooApiError(MoomooBrokerError):
    """HTTP succeeded (2xx) but the documented ret_code field was
    non-zero -- an application-level failure moomoo reports inside a
    200 response.
    """


class NoSimulatedAccountError(MoomooBrokerError):
    pass


class AmbiguousSimulatedAccountError(MoomooBrokerError):
    pass


class LiveAccountRejectedError(MoomooBrokerError):
    """Raised if any code path ever attempts to call an endpoint outside
    ALLOWED_PATH_PREFIXES -- i.e. a live-account endpoint. Under normal
    operation this is unreachable; it exists as a hard backstop against
    a future bug, not as something a moomoo response can trigger.
    """


def _to_decimal(value) -> Optional[Decimal]:
    if value is None or isinstance(value, bool):
        return None
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return d if d.is_finite() else None


class MoomooReadOnlyBroker(Broker):
    def __init__(
        self,
        *,
        access_token_provider: Callable[[], str],
        http_transport: Optional[HttpTransport] = None,
        base_host: str = DEFAULT_BASE_HOST,
        simulated_account_id: Optional[str] = None,
        market_status_reference_ticker: str = "SPY",
        default_market_prefix: str = "US",
        request_timeout_seconds: float = 10.0,
    ):
        if not callable(access_token_provider):
            raise TypeError("access_token_provider must be a zero-argument callable returning a token string")

        self._transport: HttpTransport = http_transport or UrllibHttpTransport(base_host)
        self._access_token_provider = access_token_provider
        self._explicit_account_id = simulated_account_id
        self._market_status_reference_ticker = market_status_reference_ticker
        self._default_market_prefix = default_market_prefix
        self._timeout = request_timeout_seconds

        self._resolved_account_id: Optional[str] = None
        self._quote_cache: dict = {}  # ticker -> (Decimal price, datetime|None timestamp)

    # -----------------------------------------------------------------
    # HTTP plumbing
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
        """Every moomoo REST response documented in
        broker/MOOMOO_API_CONTRACT.md uses the same envelope:
        {"ret_code": int, "ret_msg": str, "data": {...}}. ret_code != 0
        is an application-level failure reported inside a 200 response.
        """
        if "ret_code" not in payload:
            raise MalformedResponseError(f"Response from {path} is missing the documented 'ret_code' envelope field.")
        ret_code = payload.get("ret_code")
        if ret_code != 0:
            raise MoomooApiError(f"{path} returned ret_code={ret_code!r} (ret_msg={payload.get('ret_msg')!r}).")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise MalformedResponseError(f"Response from {path} is missing the documented 'data' object.")
        return data

    def _qualify_code(self, ticker: str) -> str:
        return f"{self._default_market_prefix}.{ticker}"

    # -----------------------------------------------------------------
    # Simulated account resolution
    # -----------------------------------------------------------------

    def refresh_account_selection(self) -> None:
        self._resolved_account_id = None

    def resolve_simulated_us_account_id(self) -> str:
        """Public wrapper so diagnostic tooling (e.g.
        broker/moomoo_smoke_test.py) can show account selection as its
        own step, without reaching into a private method. Same
        resolution/caching/fail-closed behavior as every other method
        on this class.
        """
        return self._resolve_simulated_account_id()

    def _resolve_simulated_account_id(self) -> str:
        if self._resolved_account_id is not None:
            return self._resolved_account_id

        payload = self._get_json(ACCOUNTS_PATH)
        data = self._unwrap_envelope(ACCOUNTS_PATH, payload)
        raw_accounts = data.get("accounts")
        if not isinstance(raw_accounts, list):
            raise MalformedResponseError("Account list response is missing an 'accounts' list.")

        us_account_ids = []
        for raw in raw_accounts:
            if not isinstance(raw, dict):
                raise MalformedResponseError("Account list entry is not an object.")
            account_id = raw.get("account_id")
            market_id = raw.get("market_id")
            if account_id is None or market_id is None:
                raise MalformedResponseError(
                    "Account list entry is missing a required field (account_id/market_id)."
                )
            if market_id == US_MARKET_ID:
                us_account_ids.append(str(account_id))

        if not us_account_ids:
            raise NoSimulatedAccountError(
                f"No simulated US-market account (market_id={US_MARKET_ID}) was found."
            )

        if self._explicit_account_id is not None:
            if self._explicit_account_id not in us_account_ids:
                raise NoSimulatedAccountError(
                    "The configured simulated_account_id is not among the simulated "
                    "US-market accounts moomoo returned."
                )
            self._resolved_account_id = self._explicit_account_id
            return self._resolved_account_id

        if len(us_account_ids) > 1:
            raise AmbiguousSimulatedAccountError(
                f"{len(us_account_ids)} simulated US-market accounts were found; set "
                "simulated_account_id explicitly to disambiguate."
            )

        self._resolved_account_id = us_account_ids[0]
        return self._resolved_account_id

    # -----------------------------------------------------------------
    # Account / positions / orders (read-only)
    # -----------------------------------------------------------------

    def get_account(self) -> Account:
        account_id = self._resolve_simulated_account_id()
        path = CASH_INFO_PATH_TEMPLATE.format(acc_id=account_id)
        payload = self._get_json(path)
        data = self._unwrap_envelope(path, payload)

        cash = _to_decimal(data.get("balance"))
        if cash is None:
            raise MalformedResponseError("Cash-info response is missing a valid 'balance'.")
        total_asset = _to_decimal(data.get("total_asset"))
        equity = total_asset if total_asset is not None else cash
        return Account(cash=cash, equity=equity, account_mode=ACCOUNT_MODE)

    def get_positions(self) -> list:
        account_id = self._resolve_simulated_account_id()
        path = POSITIONS_PATH_TEMPLATE.format(acc_id=account_id)
        # market is documented as an optional query filter, but a real
        # call without it returned a generic backend error -- passing
        # this account's own (already-resolved) US market_id is the
        # evidenced fix, not a guess. See broker/MOOMOO_API_CONTRACT.md.
        payload = self._get_json(path, params={"market": US_MARKET_ID})
        data = self._unwrap_envelope(path, payload)
        raw_positions = data.get("positions")
        if not isinstance(raw_positions, list):
            raise MalformedResponseError("Positions response is missing a 'positions' list.")
        return [self._map_position(raw) for raw in raw_positions]

    def _map_position(self, raw) -> Position:
        if not isinstance(raw, dict):
            raise MalformedResponseError("Position entry is not an object.")
        ticker = raw.get("symbol")
        quantity = _to_decimal(raw.get("qty"))
        average_entry_price = _to_decimal(raw.get("cost_price"))
        if not (isinstance(ticker, str) and ticker.strip()) or quantity is None or average_entry_price is None:
            raise MalformedResponseError("Position entry is missing a required field (symbol/qty/cost_price).")
        market_value = _to_decimal(raw.get("mv"))
        unrealized_pnl = _to_decimal(raw.get("profit"))
        return Position(
            ticker=ticker.strip().upper(),
            quantity=quantity,
            average_entry_price=average_entry_price,
            market_value=market_value if market_value is not None else Decimal("0"),
            unrealized_pnl=unrealized_pnl if unrealized_pnl is not None else Decimal("0"),
        )

    def get_position(self, ticker: str) -> Optional[Position]:
        if not (isinstance(ticker, str) and ticker.strip()):
            return None
        normalized = ticker.strip().upper()
        for position in self.get_positions():
            if position.ticker == normalized:
                return position
        return None

    def get_orders(self) -> list:
        """Merges Today's Orders (open) and History Orders, per
        broker/MOOMOO_API_CONTRACT.md sections 4/5 -- Beanstock's
        Broker.get_orders() expects the full order list, not just one
        of the two moomoo exposes separately. History wins on a
        duplicate order_id, since it reflects the final state.
        """
        account_id = self._resolve_simulated_account_id()
        merged: dict = {}
        for order in self._fetch_orders(OPEN_ORDERS_PATH_TEMPLATE.format(acc_id=account_id)):
            merged[order.order_id] = order
        for order in self._fetch_orders(HISTORY_ORDERS_PATH_TEMPLATE.format(acc_id=account_id)):
            merged[order.order_id] = order
        return list(merged.values())

    def _fetch_orders(self, path: str) -> list:
        # A real call without `market` returned ret_code=-3
        # "missing required parameter: market" -- see
        # broker/MOOMOO_API_CONTRACT.md. Not documented as required by
        # the fetched order-list page, but the live API disagrees.
        payload = self._get_json(path, params={"market": US_MARKET_ID})
        data = self._unwrap_envelope(path, payload)
        raw_orders = data.get("orders")
        if not isinstance(raw_orders, list):
            raise MalformedResponseError(f"Response from {path} is missing an 'orders' list.")
        return [self._map_order(raw) for raw in raw_orders]

    def _map_order(self, raw) -> Order:
        if not isinstance(raw, dict):
            raise MalformedResponseError("Order entry is not an object.")
        order_id = raw.get("order_id")
        raw_status = raw.get("status")
        if order_id is None or raw_status is None:
            raise MalformedResponseError("Order entry is missing order_id/status.")
        status = ORDER_STATUS_MAP.get(raw_status)
        if status is None:
            raise MalformedResponseError(f"Unrecognized order status value: {raw_status!r}.")

        raw_side = raw.get("side")
        action = ORDER_SIDE_MAP.get(raw_side) if raw_side is not None else None
        if raw_side is not None and action is None:
            raise MalformedResponseError(f"Unrecognized order side value: {raw_side!r}.")

        ticker = raw.get("symbol")
        created_at = raw.get("create_time")

        return Order(
            order_id=str(order_id),
            ticker=ticker.strip().upper() if isinstance(ticker, str) and ticker.strip() else None,
            action=action,
            status=status,
            requested_quantity=_to_decimal(raw.get("qty")),
            requested_dollar_amount=None,
            fill_price=_to_decimal(raw.get("price")),
            filled_quantity=_to_decimal(raw.get("cum_qty")),
            realized_pnl=None,
            audit_reference=None,
            rejection_reason=None,
            created_at=str(created_at) if created_at is not None else "",
        )

    def get_order(self, order_id: str) -> Optional[Order]:
        target = str(order_id)
        for order in self.get_orders():
            if order.order_id == target:
                return order
        return None

    # -----------------------------------------------------------------
    # Quotes / market status
    # -----------------------------------------------------------------

    def get_quote(self, ticker: str) -> Optional[Decimal]:
        if not (isinstance(ticker, str) and ticker.strip()):
            return None
        normalized = ticker.strip().upper()

        payload = self._post_json(QUOTE_PATH, json_body={"code_list": [self._qualify_code(normalized)]})
        data = self._unwrap_envelope(QUOTE_PATH, payload)
        raw_quotes = data.get("quote_list")
        if not isinstance(raw_quotes, list) or not raw_quotes:
            raise MalformedResponseError(f"Quote response has no quote_list entry for {normalized!r}.")
        raw = raw_quotes[0]
        if not isinstance(raw, dict):
            raise MalformedResponseError("Quote entry is not an object.")

        price = _to_decimal(raw.get("last_price"))
        if price is None or price <= 0:
            raise MalformedResponseError(f"Quote for {normalized!r} has no valid last_price.")

        timestamp = self._parse_quote_timestamp(raw.get("data_time"))
        self._quote_cache[normalized] = (price, timestamp)
        return price

    def _parse_quote_timestamp(self, raw_data_time) -> Optional[datetime]:
        """data_time is documented as a millisecond epoch timestamp
        (https://open.moomoo.com/api/quote/realtime/stock-quote)."""
        if raw_data_time is None:
            return None
        try:
            millis = float(raw_data_time)
        except (TypeError, ValueError):
            return None
        return datetime.fromtimestamp(millis / 1000.0, tz=timezone.utc)

    def get_quote_timestamp(self, ticker: str) -> Optional[datetime]:
        if not isinstance(ticker, str):
            return None
        entry = self._quote_cache.get(ticker.strip().upper())
        return entry[1] if entry else None

    def get_market_status(self) -> str:
        payload = self._post_json(
            MARKET_STATE_PATH, json_body={"code_list": [self._qualify_code(self._market_status_reference_ticker)]}
        )
        data = self._unwrap_envelope(MARKET_STATE_PATH, payload)
        raw_states = data.get("market_state_list")
        if not isinstance(raw_states, list) or not raw_states:
            raise MalformedResponseError("Market state response is missing market_state_list.")
        raw = raw_states[0]
        if not isinstance(raw, dict):
            raise MalformedResponseError("Market state entry is not an object.")
        state = raw.get("market_state")
        if not isinstance(state, str) or not state.strip():
            raise MalformedResponseError("Market state entry is missing market_state.")
        return state.strip().upper()

    # -----------------------------------------------------------------
    # Execution -- permanently disabled
    # -----------------------------------------------------------------

    def submit_execution_intent(self, intent) -> Order:
        raise ReadOnlyBrokerError(
            "MoomooReadOnlyBroker is read-only: submit_execution_intent() is permanently "
            "disabled. This adapter implements no order-placement endpoint."
        )

    def cancel_order(self, order_id: str) -> Order:
        raise ReadOnlyBrokerError(
            "MoomooReadOnlyBroker is read-only: cancel_order() is permanently disabled. "
            "This adapter implements no order-cancellation endpoint."
        )

    def close_position(self, ticker: str) -> Order:
        raise ReadOnlyBrokerError(
            "MoomooReadOnlyBroker is read-only: close_position() is permanently disabled. "
            "This adapter implements no position-modifying endpoint."
        )
