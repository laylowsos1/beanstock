"""Beanstock FakePaperBroker.

A purely local, in-memory simulation of a paper-trading broker. This is
NOT moomoo and NOT any real broker -- it makes no network calls, requires
no API credentials, connects to no external system, and has no live-
trading path. Prices come only from an in-memory fake quote store that
tests load explicitly; there is no internet fallback.

Architecture position (see execution/intent.py and models/trade_proposal.py):

    AI research -> TradeProposal -> schema validation -> action routing
        -> deterministic risk engine -> ExecutionIntent
        -> FakePaperBroker -> fake order / fake position
        -> audit + trade log

submit_execution_intent() is the only entry point for placing a
(simulated) order, and it accepts ONLY execution.intent.ExecutionIntent
instances -- never a TradeProposal, a raw dict, a plain string, or any
other AI-authored value. Passing anything else raises TypeError before
any account state is touched.

Defense in depth: this broker does not assume the upstream risk engine
or execution-intent layer is perfect. Every ExecutionIntent is
independently re-verified here -- execution_allowed, account_mode,
instrument_type, action, ticker, and quote validity are all re-checked
from scratch, and an existing position is independently confirmed for
ADD/REDUCE/EXIT regardless of what the intent (or its originating
decision) claims.

Monetary accounting uses decimal.Decimal throughout -- never binary
floating point -- so cash/quantity/price arithmetic is exact for the
values these tests use.
"""

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Optional, Union
import uuid

from broker.base import Account, Broker, Order, Position
from execution.intent import ExecutionIntent
from risk.validator import PAPER_MODES

ALLOWED_INSTRUMENT_TYPES = {"stock", "fractional_share"}
ALLOWED_ACTIONS = {"BUY", "ADD", "REDUCE", "EXIT"}

ORDER_STATUS_PENDING = "PENDING"
ORDER_STATUS_FILLED = "FILLED"
ORDER_STATUS_REJECTED = "REJECTED"
ORDER_STATUS_CANCELED = "CANCELED"

REJECT_DUPLICATE = "REJECT_DUPLICATE"


def _to_decimal(value) -> Optional[Decimal]:
    """Convert a plain number to Decimal via its string form (never via
    float's binary representation), rejecting anything that is not a
    finite, usable numeric value.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        d = value
    elif isinstance(value, (int, float, str)):
        try:
            d = Decimal(str(value))
        except (InvalidOperation, ValueError):
            return None
    else:
        return None
    if not d.is_finite():
        return None
    return d


def _is_valid_price(price: Optional[Decimal]) -> bool:
    return price is not None and price.is_finite() and price > 0


@dataclass
class _OpenPosition:
    """Internal mutable position state. Not exposed directly -- callers
    only ever see the frozen broker.base.Position snapshot.
    """

    ticker: str
    quantity: Decimal
    average_entry_price: Decimal
    realized_pnl: Decimal = field(default_factory=lambda: Decimal("0"))


class FakePaperBroker(Broker):
    def __init__(self, starting_cash=Decimal("300.00"), account_mode: str = "PAPER"):
        cash = _to_decimal(starting_cash)
        if cash is None or cash < 0:
            raise ValueError(f"starting_cash must be a non-negative number, got {starting_cash!r}")

        mode = (account_mode or "").strip().upper()
        if mode not in PAPER_MODES:
            raise ValueError(
                f"FakePaperBroker only supports paper/simulated accounts; "
                f"got account_mode={account_mode!r}"
            )

        self._cash: Decimal = cash
        self._account_mode: str = mode
        self._quotes: dict = {}
        self._positions: dict = {}  # ticker -> _OpenPosition
        self._orders: dict = {}  # order_id -> Order
        self._order_sequence: list = []  # preserves submission order
        self._filled_audit_references: set = set()
        self._audit_log: list = []

    # -----------------------------------------------------------------
    # Fake quotes
    # -----------------------------------------------------------------

    def set_quote(self, ticker: str, price) -> None:
        """Load (or overwrite) a fake quote. Accepts int/float/str/Decimal,
        including invalid values (zero, negative, NaN) so tests can
        exercise the broker's price-validation defenses -- set_quote
        itself does not reject them; submit_execution_intent does.
        """
        if not (isinstance(ticker, str) and ticker.strip()):
            raise ValueError("ticker must be a non-empty string")
        d = _to_decimal(price)
        if d is None:
            # store an explicit NaN sentinel rather than silently
            # dropping the (deliberately) bad value
            d = Decimal("NaN")
        self._quotes[ticker.strip().upper()] = d

    def get_quote(self, ticker: str) -> Optional[Decimal]:
        if not isinstance(ticker, str):
            return None
        return self._quotes.get(ticker.strip().upper())

    def get_market_status(self) -> str:
        return "SIMULATED_OPEN"

    # -----------------------------------------------------------------
    # Account / positions / orders (read-only views)
    # -----------------------------------------------------------------

    def get_account(self) -> Account:
        equity = self._cash
        for pos in self._positions.values():
            equity += self._market_value(pos)
        return Account(cash=self._cash, equity=equity, account_mode=self._account_mode)

    def get_positions(self) -> list:
        return [self._position_snapshot(pos) for pos in self._positions.values()]

    def get_position(self, ticker: str) -> Optional[Position]:
        pos = self._positions.get(self._normalize_ticker(ticker))
        return self._position_snapshot(pos) if pos else None

    def get_orders(self) -> list:
        return [self._orders[oid] for oid in self._order_sequence]

    def get_order(self, order_id: str) -> Optional[Order]:
        return self._orders.get(order_id)

    def get_audit_log(self) -> list:
        """Full attempted-execution audit trail, filled and rejected
        alike. Never contains credentials or account identifiers --
        this broker never has any to record.
        """
        return list(self._audit_log)

    def _normalize_ticker(self, ticker) -> Optional[str]:
        if not (isinstance(ticker, str) and ticker.strip()):
            return None
        return ticker.strip().upper()

    def _market_value(self, pos: "_OpenPosition") -> Decimal:
        quote = self.get_quote(pos.ticker)
        if not _is_valid_price(quote):
            return Decimal("0")
        return pos.quantity * quote

    def _position_snapshot(self, pos: Optional["_OpenPosition"]) -> Optional[Position]:
        if pos is None:
            return None
        market_value = self._market_value(pos)
        cost_basis = pos.quantity * pos.average_entry_price
        return Position(
            ticker=pos.ticker,
            quantity=pos.quantity,
            average_entry_price=pos.average_entry_price,
            market_value=market_value,
            unrealized_pnl=market_value - cost_basis,
        )

    # -----------------------------------------------------------------
    # Order submission
    # -----------------------------------------------------------------

    def submit_execution_intent(self, intent: "ExecutionIntent") -> Order:
        """The ONLY way to place a (simulated) order. Accepts ONLY a real
        ExecutionIntent instance -- a TradeProposal, a dict, a string
        ("APPROVED" or otherwise), or any other object is a programming
        error, not a legitimate rejected attempt, so it raises TypeError
        immediately rather than being logged as an order.
        """
        if not isinstance(intent, ExecutionIntent):
            raise TypeError(
                "submit_execution_intent() requires an ExecutionIntent produced by "
                "execution.intent.create_execution_intent(); got "
                f"{type(intent).__name__!r}"
            )

        return self._process_intent(intent)

    def _process_intent(self, intent: "ExecutionIntent") -> Order:
        audit_reference = intent.audit_reference

        # --- duplicate-fill protection: the same audit_reference can
        #     never fill twice. ---
        if audit_reference is not None and audit_reference in self._filled_audit_references:
            return self._reject(
                intent,
                reason=(
                    f"{REJECT_DUPLICATE}: audit_reference {audit_reference!r} has "
                    "already been filled once."
                ),
            )

        # --- defense in depth: never trust the upstream decision alone. ---
        if intent.execution_allowed is not True:
            return self._reject(intent, reason="execution_allowed is not True.")

        mode = (intent.account_mode or "").strip().upper()
        if mode not in PAPER_MODES:
            return self._reject(
                intent,
                reason=f"account_mode={intent.account_mode!r} is not PAPER/SIMULATED.",
            )

        action = (intent.action or "").strip().upper()
        if action not in ALLOWED_ACTIONS:
            return self._reject(
                intent,
                reason=f"action={intent.action!r} is not a broker-executable action "
                "(HOLD/DO_NOTHING never reach the broker).",
            )

        instrument_type = (intent.instrument_type or "").strip().lower()
        if instrument_type not in ALLOWED_INSTRUMENT_TYPES:
            return self._reject(
                intent,
                reason=f"instrument_type={intent.instrument_type!r} is not supported "
                "(stock/fractional_share only -- no options, shorts, or margin).",
            )

        ticker = self._normalize_ticker(intent.ticker)
        if ticker is None:
            return self._reject(intent, reason="Missing or invalid ticker.")

        quote = self.get_quote(ticker)
        if not _is_valid_price(quote):
            return self._reject(
                intent,
                reason=f"No valid fake quote loaded for {ticker!r}; refusing to fill "
                "without a real price (no internet fallback).",
                ticker=ticker,
            )

        if action in ("BUY", "ADD"):
            return self._fill_buy_or_add(intent, ticker, quote, action)
        if action == "REDUCE":
            return self._fill_reduce(intent, ticker, quote)
        return self._fill_exit(intent, ticker, quote)

    # -----------------------------------------------------------------
    # BUY / ADD
    # -----------------------------------------------------------------

    def _fill_buy_or_add(self, intent, ticker: str, quote: Decimal, action: str) -> Order:
        existing = self._positions.get(ticker)

        if action == "ADD" and existing is None:
            return self._reject(
                intent, reason="ADD requires an existing position; none found.", ticker=ticker
            )
        if action == "BUY" and existing is not None:
            return self._reject(
                intent,
                reason="BUY requires no existing position; use ADD to add to one.",
                ticker=ticker,
            )

        dollar_amount = _to_decimal(intent.dollar_amount)
        if dollar_amount is None or dollar_amount <= 0:
            return self._reject(
                intent,
                reason=f"dollar_amount must be a positive number, got {intent.dollar_amount!r}.",
                ticker=ticker,
            )

        # BUY/ADD can never create cash: the fill can never cost more
        # than the fake cash on hand.
        if dollar_amount > self._cash:
            return self._reject(
                intent,
                reason=f"Insufficient cash: requested ${dollar_amount}, available ${self._cash}.",
                ticker=ticker,
                requested_dollar_amount=dollar_amount,
            )

        quantity_filled = dollar_amount / quote

        if existing is None:
            new_quantity = quantity_filled
            new_average = quote
        else:
            new_quantity = existing.quantity + quantity_filled
            new_average = (
                existing.quantity * existing.average_entry_price + quantity_filled * quote
            ) / new_quantity

        self._cash -= dollar_amount
        self._positions[ticker] = _OpenPosition(
            ticker=ticker,
            quantity=new_quantity,
            average_entry_price=new_average,
            realized_pnl=existing.realized_pnl if existing else Decimal("0"),
        )

        return self._fill(
            intent,
            ticker=ticker,
            action=action,
            requested_dollar_amount=dollar_amount,
            fill_price=quote,
            filled_quantity=quantity_filled,
            realized_pnl=None,
        )

    # -----------------------------------------------------------------
    # REDUCE
    # -----------------------------------------------------------------

    def _fill_reduce(self, intent, ticker: str, quote: Decimal) -> Order:
        existing = self._positions.get(ticker)
        if existing is None or existing.quantity <= 0:
            return self._reject(
                intent, reason="REDUCE requires an existing position; none found.", ticker=ticker
            )

        dollar_amount = _to_decimal(intent.dollar_amount)
        if dollar_amount is None or dollar_amount <= 0:
            return self._reject(
                intent,
                reason=f"dollar_amount must be a positive number, got {intent.dollar_amount!r}.",
                ticker=ticker,
            )

        quantity_to_reduce = dollar_amount / quote

        # Never sell more than is held -- never create a short position.
        if quantity_to_reduce > existing.quantity:
            return self._reject(
                intent,
                reason=(
                    f"REDUCE would sell {quantity_to_reduce} shares against a held "
                    f"quantity of {existing.quantity}; refusing to create a short position."
                ),
                ticker=ticker,
                requested_dollar_amount=dollar_amount,
            )

        proceeds = quantity_to_reduce * quote
        realized_pnl = (quote - existing.average_entry_price) * quantity_to_reduce

        remaining_quantity = existing.quantity - quantity_to_reduce
        self._cash += proceeds

        if remaining_quantity <= 0:
            self._positions.pop(ticker, None)
        else:
            self._positions[ticker] = _OpenPosition(
                ticker=ticker,
                quantity=remaining_quantity,
                average_entry_price=existing.average_entry_price,
                realized_pnl=existing.realized_pnl + realized_pnl,
            )

        return self._fill(
            intent,
            ticker=ticker,
            action="REDUCE",
            requested_dollar_amount=dollar_amount,
            fill_price=quote,
            filled_quantity=quantity_to_reduce,
            realized_pnl=realized_pnl,
        )

    # -----------------------------------------------------------------
    # EXIT
    # -----------------------------------------------------------------

    def _fill_exit(self, intent, ticker: str, quote: Decimal) -> Order:
        existing = self._positions.get(ticker)
        if existing is None or existing.quantity <= 0:
            return self._reject(
                intent, reason="EXIT requires an existing position; none found.", ticker=ticker
            )

        # EXIT always closes the full remaining position -- it is never
        # partial and never leaves a residual (short-creating) balance.
        quantity_to_close = existing.quantity
        proceeds = quantity_to_close * quote
        realized_pnl = (quote - existing.average_entry_price) * quantity_to_close

        self._cash += proceeds
        self._positions.pop(ticker, None)

        return self._fill(
            intent,
            ticker=ticker,
            action="EXIT",
            requested_dollar_amount=proceeds,
            fill_price=quote,
            filled_quantity=quantity_to_close,
            realized_pnl=realized_pnl,
        )

    # -----------------------------------------------------------------
    # Order bookkeeping / audit trail
    # -----------------------------------------------------------------

    def _new_order_id(self) -> str:
        return f"order-{uuid.uuid4().hex[:12]}"

    def _record(
        self,
        intent,
        status: str,
        ticker: Optional[str] = None,
        requested_dollar_amount: Optional[Decimal] = None,
        fill_price: Optional[Decimal] = None,
        filled_quantity: Optional[Decimal] = None,
        realized_pnl: Optional[Decimal] = None,
        rejection_reason: Optional[str] = None,
    ) -> Order:
        order_id = self._new_order_id()
        created_at = datetime.now(timezone.utc).isoformat()

        order = Order(
            order_id=order_id,
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
            realized_pnl=realized_pnl,
            audit_reference=getattr(intent, "audit_reference", None),
            rejection_reason=rejection_reason,
            created_at=created_at,
        )
        self._orders[order_id] = order
        self._order_sequence.append(order_id)

        self._audit_log.append(
            {
                "timestamp": created_at,
                "audit_reference": order.audit_reference,
                "order_id": order_id,
                "ticker": order.ticker,
                "action": order.action,
                "requested_dollar_amount": order.requested_dollar_amount,
                "fill_price": order.fill_price,
                "quantity_filled": order.filled_quantity,
                "resulting_cash": self._cash,
                "resulting_position_quantity": (
                    self._positions[order.ticker].quantity
                    if order.ticker in self._positions
                    else Decimal("0")
                ),
                "realized_pnl": order.realized_pnl,
                "status": order.status,
                "rejection_reason": order.rejection_reason,
            }
        )
        return order

    def _fill(
        self,
        intent,
        ticker: str,
        action: str,
        requested_dollar_amount: Decimal,
        fill_price: Decimal,
        filled_quantity: Decimal,
        realized_pnl: Optional[Decimal],
    ) -> Order:
        if intent.audit_reference is not None:
            self._filled_audit_references.add(intent.audit_reference)
        return self._record(
            intent,
            status=ORDER_STATUS_FILLED,
            ticker=ticker,
            requested_dollar_amount=requested_dollar_amount,
            fill_price=fill_price,
            filled_quantity=filled_quantity,
            realized_pnl=realized_pnl,
        )

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

    # -----------------------------------------------------------------
    # Administrative order management (not part of the AI decision path)
    # -----------------------------------------------------------------

    def cancel_order(self, order_id: str) -> Order:
        order = self._orders.get(order_id)
        if order is None:
            raise KeyError(f"No such order_id: {order_id!r}")
        if order.status != ORDER_STATUS_PENDING:
            return order
        canceled = replace(order, status=ORDER_STATUS_CANCELED)
        self._orders[order_id] = canceled
        return canceled

    def close_position(self, ticker: str) -> Order:
        """Administrative full close, independent of the AI decision
        pipeline (e.g. an emergency flatten) -- not something the AI
        research/risk-engine layers call. Still enforces every
        accounting invariant the same way EXIT does.
        """
        normalized = self._normalize_ticker(ticker)
        fake_intent = _AdminCloseIntent(ticker=normalized or ticker)
        if normalized is None:
            return self._reject(fake_intent, reason="Missing or invalid ticker.")

        quote = self.get_quote(normalized)
        if not _is_valid_price(quote):
            return self._reject(
                fake_intent,
                reason=f"No valid fake quote loaded for {normalized!r}.",
                ticker=normalized,
            )
        return self._fill_exit(fake_intent, normalized, quote)


@dataclass
class _AdminCloseIntent:
    """Minimal stand-in used only by close_position()'s internal call
    into _fill_exit()/_reject() -- never accepted by
    submit_execution_intent(), which requires a real ExecutionIntent.
    """

    ticker: str
    action: str = "EXIT"
    dollar_amount: Optional[Decimal] = None
    audit_reference: Optional[str] = None
