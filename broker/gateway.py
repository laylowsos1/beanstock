"""Beanstock deterministic Broker Safety Gateway.

The mandatory final checkpoint between an ExecutionIntent and ANY
concrete Broker (FakePaperBroker today; a future MoomooPaperBroker or
live MoomooBroker later). No broker adapter may ever receive an
ExecutionIntent without first passing BrokerGateway.validate() (or its
convenience wrapper, submit()):

    ExecutionIntent -> BrokerGateway.validate() -> ALLOW or REJECT
                     -> Broker.submit_execution_intent()  (only if ALLOW)

This module makes no network calls, connects to no broker (moomoo or
otherwise), requires no credentials, and contains no live-trading path.
It is pure, deterministic validation logic over an ExecutionIntent and
whatever Broker instance is handed to it.

Defense in depth
-----------------
The gateway does not trust the intent's own baked-in
reference_price/quantity for anything execution-critical. It always
re-derives the facts that matter -- current cash, current position,
current quote and quote age -- from the Broker passed in at call time,
and it independently re-checks execution_allowed, account_mode,
instrument_type, and action exactly the way execution.intent and
broker.fake_paper already do, on the assumption that no single upstream
layer should be trusted as the last word.

Configurable windows, not hardcoded market assumptions
-------------------------------------------------------
max_intent_age_seconds and max_quote_age_seconds are constructor
parameters with placeholder defaults (5 minutes and 1 minute
respectively) chosen for a slow, catalyst-aware swing-trading paper
system -- NOT derived from any real market's microstructure, latency,
or trading-halt rules. The project owner should tune these once real
timing requirements are known; nothing here assumes a specific market's
behavior. Likewise max_reference_price_deviation_pct (default 20%) is a
generous placeholder tolerance for how far a live quote may have moved
from the price the AI's decision was based on before that decision is
considered stale and re-evaluation is required.

Fail-closed
-----------
Any unexpected exception raised while validating is caught and turned
into a REJECT result. The gateway never lets an exception propagate into
"allow by default," and it never calls the broker in that case.
"""

from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Optional, Union
import re

from broker.base import Broker, Order
from execution.intent import ExecutionIntent
from risk.validator import PAPER_MODES

ALLOWED_INSTRUMENT_TYPES = {"stock", "fractional_share"}
ALLOWED_ACTIONS = {"BUY", "ADD", "REDUCE", "EXIT"}
EXPOSURE_INCREASING_ACTIONS = {"BUY", "ADD"}
RISK_REDUCING_ACTIONS = {"REDUCE", "EXIT"}

_TICKER_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,9}$")

REJECT_DUPLICATE = "REJECT_DUPLICATE"

# Placeholder defaults -- see module docstring. Not market-derived.
DEFAULT_MAX_INTENT_AGE_SECONDS = 300.0  # 5 minutes
DEFAULT_MAX_QUOTE_AGE_SECONDS = 60.0  # 1 minute
DEFAULT_MAX_REFERENCE_PRICE_DEVIATION_PCT = 20.0
DEFAULT_CASH_TOLERANCE = Decimal("0.01")
DEFAULT_POSITION_QUANTITY_TOLERANCE = Decimal("0.0001")

STATUS_ALLOW = "ALLOW"
STATUS_REJECT = "REJECT"


def _to_decimal(value) -> Optional[Decimal]:
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


def _is_finite_positive(value: Optional[Decimal]) -> bool:
    return value is not None and value.is_finite() and value > 0


@dataclass
class GatewayResult:
    """Structured outcome of one BrokerGateway.validate() call -- the
    audit record for both allowed and rejected attempts.
    """

    allowed: bool
    status: str  # ALLOW | REJECT
    reasons: list
    audit_reference: Optional[str]
    account_mode: Optional[str]
    safety_state: dict
    quote_age_seconds: Optional[float]
    intent_age_seconds: Optional[float]
    timestamp: str

    def to_dict(self) -> dict:
        return asdict(self)


class BrokerGateway:
    """The one required checkpoint between an ExecutionIntent and any
    Broker.submit_execution_intent() call.

    safe_mode / daily_loss_breached / weekly_drawdown_breached are plain
    mutable attributes, not re-derived from anywhere -- they are the
    deterministic system safety state the project's own daily/weekly
    review process is responsible for setting. The gateway does not
    compute P&L or drawdown itself; it only enforces the consequence of
    those flags being True.
    """

    def __init__(
        self,
        *,
        safe_mode: bool = False,
        daily_loss_breached: bool = False,
        weekly_drawdown_breached: bool = False,
        max_intent_age_seconds: float = DEFAULT_MAX_INTENT_AGE_SECONDS,
        max_quote_age_seconds: float = DEFAULT_MAX_QUOTE_AGE_SECONDS,
        max_reference_price_deviation_pct: float = DEFAULT_MAX_REFERENCE_PRICE_DEVIATION_PCT,
        cash_tolerance: Decimal = DEFAULT_CASH_TOLERANCE,
        position_quantity_tolerance: Decimal = DEFAULT_POSITION_QUANTITY_TOLERANCE,
    ):
        self.safe_mode = safe_mode
        self.daily_loss_breached = daily_loss_breached
        self.weekly_drawdown_breached = weekly_drawdown_breached
        self.max_intent_age_seconds = max_intent_age_seconds
        self.max_quote_age_seconds = max_quote_age_seconds
        self.max_reference_price_deviation_pct = max_reference_price_deviation_pct
        self.cash_tolerance = cash_tolerance
        self.position_quantity_tolerance = position_quantity_tolerance
        self._audit_log: list = []

    def get_audit_log(self) -> list:
        return list(self._audit_log)

    # -----------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------

    def validate(
        self,
        intent,
        broker: Broker,
        *,
        expected_cash: Optional[Decimal] = None,
        expected_position_quantity: Optional[Decimal] = None,
        now: Optional[datetime] = None,
    ) -> GatewayResult:
        """Decide ALLOW/REJECT for `intent` against `broker`'s CURRENT
        state. Never raises -- any unexpected error fails closed as a
        REJECT result. Never calls broker.submit_execution_intent()
        itself; use submit() for that.
        """
        call_time = now or datetime.now(timezone.utc)

        if not isinstance(intent, ExecutionIntent):
            return self._reject(
                reasons=[
                    "intent must be a hardened ExecutionIntent produced by "
                    "execution.intent.create_execution_intent(); got "
                    f"{type(intent).__name__!r}."
                ],
                audit_reference=None,
                account_mode=None,
                call_time=call_time,
            )

        try:
            return self._validate_intent(
                intent, broker, expected_cash, expected_position_quantity, call_time
            )
        except Exception as exc:  # fail closed -- never let an exception mean "allow"
            return self._reject(
                reasons=[
                    f"Unexpected error during gateway validation; failing closed: {exc!r}"
                ],
                audit_reference=getattr(intent, "audit_reference", None),
                account_mode=getattr(intent, "account_mode", None),
                call_time=call_time,
            )

    def submit(
        self,
        intent,
        broker: Broker,
        *,
        expected_cash: Optional[Decimal] = None,
        expected_position_quantity: Optional[Decimal] = None,
        now: Optional[datetime] = None,
    ):
        """validate(), and call broker.submit_execution_intent() ONLY if
        allowed. Returns (GatewayResult, Order | None) -- Order is None
        whenever the gateway rejected, and the broker is never touched
        in that case.
        """
        result = self.validate(
            intent,
            broker,
            expected_cash=expected_cash,
            expected_position_quantity=expected_position_quantity,
            now=now,
        )
        if not result.allowed:
            return result, None
        order = broker.submit_execution_intent(intent)
        return result, order

    # -----------------------------------------------------------------
    # Internal validation pipeline
    # -----------------------------------------------------------------

    def _validate_intent(
        self,
        intent: "ExecutionIntent",
        broker: Broker,
        expected_cash: Optional[Decimal],
        expected_position_quantity: Optional[Decimal],
        call_time: datetime,
    ) -> GatewayResult:
        audit_reference = intent.audit_reference
        account_mode_raw = intent.account_mode

        # --- 2. execution_allowed must be True ---
        if intent.execution_allowed is not True:
            return self._reject(
                reasons=["execution_allowed is not True."],
                audit_reference=audit_reference,
                account_mode=account_mode_raw,
                call_time=call_time,
            )

        # --- 3. account_mode must normalize to PAPER/SIMULATED only ---
        mode = (account_mode_raw or "").strip().upper()
        if mode not in PAPER_MODES:
            return self._reject(
                reasons=[f"account_mode={account_mode_raw!r} is not PAPER/SIMULATED."],
                audit_reference=audit_reference,
                account_mode=account_mode_raw,
                call_time=call_time,
            )

        # --- 4. instrument must be stock/fractional-share only ---
        instrument_type = (intent.instrument_type or "").strip().lower()
        if instrument_type not in ALLOWED_INSTRUMENT_TYPES:
            return self._reject(
                reasons=[
                    f"instrument_type={intent.instrument_type!r} is not supported "
                    "(stock/fractional_share only)."
                ],
                audit_reference=audit_reference,
                account_mode=account_mode_raw,
                call_time=call_time,
            )

        # --- 5. action must be BUY/ADD/REDUCE/EXIT ---
        action = (intent.action or "").strip().upper()
        if action not in ALLOWED_ACTIONS:
            return self._reject(
                reasons=[
                    f"action={intent.action!r} is not executable "
                    "(HOLD/DO_NOTHING never reach the gateway)."
                ],
                audit_reference=audit_reference,
                account_mode=account_mode_raw,
                call_time=call_time,
            )

        # --- 15/16/17. kill switch + daily-loss + weekly-drawdown gates ---
        # These only ever block exposure-increasing actions; risk-reducing
        # actions (REDUCE/EXIT) are still allowed to proceed to the rest
        # of validation.
        if action in EXPOSURE_INCREASING_ACTIONS:
            if self.safe_mode:
                return self._reject(
                    reasons=[
                        "SAFE_MODE is active; new BUY/ADD orders are blocked "
                        "(REDUCE/EXIT remain permitted)."
                    ],
                    audit_reference=audit_reference,
                    account_mode=account_mode_raw,
                    call_time=call_time,
                )
            if self.daily_loss_breached:
                return self._reject(
                    reasons=[
                        "Daily loss threshold has been breached; BUY/ADD orders "
                        "are blocked until the next daily review."
                    ],
                    audit_reference=audit_reference,
                    account_mode=account_mode_raw,
                    call_time=call_time,
                )
            if self.weekly_drawdown_breached:
                return self._reject(
                    reasons=[
                        "Weekly drawdown threshold has been breached; BUY/ADD "
                        "orders are blocked until the next weekly review."
                    ],
                    audit_reference=audit_reference,
                    account_mode=account_mode_raw,
                    call_time=call_time,
                )

        # --- 6. ticker must be non-empty and valid format ---
        ticker = (intent.ticker or "").strip().upper()
        if not ticker or not _TICKER_PATTERN.match(ticker):
            return self._reject(
                reasons=[f"ticker={intent.ticker!r} is missing or not a valid format."],
                audit_reference=audit_reference,
                account_mode=account_mode_raw,
                call_time=call_time,
            )

        # --- 11. duplicate-execution protection ---
        # Ask the broker itself (via its order history) rather than
        # keeping separate gateway-side state that could drift out of
        # sync with what actually filled.
        for order in broker.get_orders():
            if order.audit_reference == audit_reference and order.status == "FILLED":
                return self._reject(
                    reasons=[
                        f"{REJECT_DUPLICATE}: audit_reference {audit_reference!r} has "
                        "already been filled once."
                    ],
                    audit_reference=audit_reference,
                    account_mode=account_mode_raw,
                    call_time=call_time,
                )

        # --- 12. stale intent protection ---
        intent_age_seconds = self._age_seconds(intent.created_at, call_time)
        if intent_age_seconds is None:
            return self._reject(
                reasons=["intent created_at is missing or unparseable."],
                audit_reference=audit_reference,
                account_mode=account_mode_raw,
                call_time=call_time,
            )
        if intent_age_seconds > self.max_intent_age_seconds:
            return self._reject(
                reasons=[
                    f"Intent is {intent_age_seconds:.1f}s old, exceeding the "
                    f"{self.max_intent_age_seconds:.1f}s safe window; re-evaluation required."
                ],
                audit_reference=audit_reference,
                account_mode=account_mode_raw,
                intent_age_seconds=intent_age_seconds,
                call_time=call_time,
            )

        # --- 7. dollar_amount / quantity must be finite and > 0 where
        #     required. EXIT always closes the full position and does not
        #     require a caller-supplied sizing figure. ---
        dollar_amount = _to_decimal(intent.dollar_amount)
        if action in ("BUY", "ADD", "REDUCE"):
            if not _is_finite_positive(dollar_amount):
                return self._reject(
                    reasons=[
                        f"dollar_amount must be a finite positive number for {action}, "
                        f"got {intent.dollar_amount!r}."
                    ],
                    audit_reference=audit_reference,
                    account_mode=account_mode_raw,
                    intent_age_seconds=intent_age_seconds,
                    call_time=call_time,
                )
        quantity = _to_decimal(intent.quantity)
        if intent.quantity is not None and quantity is None:
            return self._reject(
                reasons=[f"quantity is not a finite number: {intent.quantity!r}."],
                audit_reference=audit_reference,
                account_mode=account_mode_raw,
                intent_age_seconds=intent_age_seconds,
                call_time=call_time,
            )

        # --- 8. reference price must be finite and > 0 ---
        reference_price = _to_decimal(intent.reference_price)
        if not _is_finite_positive(reference_price):
            return self._reject(
                reasons=[
                    f"reference_price must be a finite positive number, got "
                    f"{intent.reference_price!r}."
                ],
                audit_reference=audit_reference,
                account_mode=account_mode_raw,
                intent_age_seconds=intent_age_seconds,
                call_time=call_time,
            )

        # --- 13. quote staleness / missing quote ---
        quote = broker.get_quote(ticker)
        if not _is_finite_positive(quote):
            return self._reject(
                reasons=[f"No valid current quote for {ticker!r}."],
                audit_reference=audit_reference,
                account_mode=account_mode_raw,
                intent_age_seconds=intent_age_seconds,
                call_time=call_time,
            )
        quote_timestamp = broker.get_quote_timestamp(ticker)
        quote_age_seconds = self._age_seconds_from_datetime(quote_timestamp, call_time)
        if quote_age_seconds is None:
            return self._reject(
                reasons=[f"Quote for {ticker!r} has no timestamp; cannot verify freshness."],
                audit_reference=audit_reference,
                account_mode=account_mode_raw,
                intent_age_seconds=intent_age_seconds,
                call_time=call_time,
            )
        if quote_age_seconds > self.max_quote_age_seconds:
            return self._reject(
                reasons=[
                    f"Quote for {ticker!r} is {quote_age_seconds:.1f}s old, exceeding the "
                    f"{self.max_quote_age_seconds:.1f}s freshness window."
                ],
                audit_reference=audit_reference,
                account_mode=account_mode_raw,
                quote_age_seconds=quote_age_seconds,
                intent_age_seconds=intent_age_seconds,
                call_time=call_time,
            )

        # --- 9/14 (price half): never rely solely on the intent's own
        #     baked-in reference_price -- compare it against the CURRENT
        #     quote and require re-evaluation if the market has moved too
        #     far from what the decision assumed. ---
        deviation_pct = abs(quote - reference_price) / reference_price * 100
        if deviation_pct > self.max_reference_price_deviation_pct:
            return self._reject(
                reasons=[
                    f"Current quote {quote} deviates {deviation_pct:.1f}% from the intent's "
                    f"reference_price {reference_price}, exceeding the "
                    f"{self.max_reference_price_deviation_pct:.1f}% tolerance; "
                    "re-evaluation required."
                ],
                audit_reference=audit_reference,
                account_mode=account_mode_raw,
                quote_age_seconds=quote_age_seconds,
                intent_age_seconds=intent_age_seconds,
                call_time=call_time,
            )

        # --- 14 (account-state half): compare the decision's cash/position
        #     assumptions against the broker's CURRENT state, if supplied. ---
        account = broker.get_account()
        if expected_cash is not None:
            expected_cash_d = _to_decimal(expected_cash)
            if expected_cash_d is not None and abs(account.cash - expected_cash_d) > self.cash_tolerance:
                return self._reject(
                    reasons=[
                        f"Account cash changed since the decision was made "
                        f"(expected ~{expected_cash_d}, now {account.cash}); "
                        "re-evaluation required."
                    ],
                    audit_reference=audit_reference,
                    account_mode=account_mode_raw,
                    quote_age_seconds=quote_age_seconds,
                    intent_age_seconds=intent_age_seconds,
                    call_time=call_time,
                )

        position = broker.get_position(ticker)
        current_quantity = position.quantity if position else Decimal("0")
        if expected_position_quantity is not None:
            expected_quantity_d = _to_decimal(expected_position_quantity)
            if (
                expected_quantity_d is not None
                and abs(current_quantity - expected_quantity_d) > self.position_quantity_tolerance
            ):
                return self._reject(
                    reasons=[
                        f"Position quantity for {ticker!r} changed since the decision was "
                        f"made (expected ~{expected_quantity_d}, now {current_quantity}); "
                        "re-evaluation required."
                    ],
                    audit_reference=audit_reference,
                    account_mode=account_mode_raw,
                    quote_age_seconds=quote_age_seconds,
                    intent_age_seconds=intent_age_seconds,
                    call_time=call_time,
                )

        # --- 9. BUY/ADD: sufficient CURRENT cash ---
        if action in ("BUY", "ADD"):
            if dollar_amount > account.cash:
                return self._reject(
                    reasons=[
                        f"Insufficient cash: requested {dollar_amount}, available "
                        f"{account.cash}."
                    ],
                    audit_reference=audit_reference,
                    account_mode=account_mode_raw,
                    quote_age_seconds=quote_age_seconds,
                    intent_age_seconds=intent_age_seconds,
                    call_time=call_time,
                )
            if action == "ADD" and (position is None or current_quantity <= 0):
                return self._reject(
                    reasons=[f"ADD requires an existing position in {ticker!r}; none found."],
                    audit_reference=audit_reference,
                    account_mode=account_mode_raw,
                    quote_age_seconds=quote_age_seconds,
                    intent_age_seconds=intent_age_seconds,
                    call_time=call_time,
                )
            if action == "BUY" and position is not None and current_quantity > 0:
                return self._reject(
                    reasons=[
                        f"BUY requires no existing position in {ticker!r}; use ADD instead."
                    ],
                    audit_reference=audit_reference,
                    account_mode=account_mode_raw,
                    quote_age_seconds=quote_age_seconds,
                    intent_age_seconds=intent_age_seconds,
                    call_time=call_time,
                )

        # --- 10. REDUCE/EXIT: require existing position, never over-sell,
        #     never flip short. ---
        if action in RISK_REDUCING_ACTIONS:
            if position is None or current_quantity <= 0:
                return self._reject(
                    reasons=[f"{action} requires an existing position in {ticker!r}; none found."],
                    audit_reference=audit_reference,
                    account_mode=account_mode_raw,
                    quote_age_seconds=quote_age_seconds,
                    intent_age_seconds=intent_age_seconds,
                    call_time=call_time,
                )
            if action == "REDUCE":
                quantity_to_reduce = dollar_amount / quote
                if quantity_to_reduce > current_quantity:
                    return self._reject(
                        reasons=[
                            f"REDUCE would sell {quantity_to_reduce} shares against a held "
                            f"quantity of {current_quantity}; refusing to create a short "
                            "position."
                        ],
                        audit_reference=audit_reference,
                        account_mode=account_mode_raw,
                        quote_age_seconds=quote_age_seconds,
                        intent_age_seconds=intent_age_seconds,
                        call_time=call_time,
                    )

        return self._allow(
            audit_reference=audit_reference,
            account_mode=account_mode_raw,
            quote_age_seconds=quote_age_seconds,
            intent_age_seconds=intent_age_seconds,
            call_time=call_time,
        )

    # -----------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------

    def _age_seconds(self, created_at: Optional[str], call_time: datetime) -> Optional[float]:
        if not created_at:
            return None
        try:
            parsed = datetime.fromisoformat(created_at)
        except (TypeError, ValueError):
            return None
        return self._age_seconds_from_datetime(parsed, call_time)

    def _age_seconds_from_datetime(
        self, moment: Optional[datetime], call_time: datetime
    ) -> Optional[float]:
        if moment is None:
            return None
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return (call_time - moment).total_seconds()

    def _safety_state(self) -> dict:
        return {
            "safe_mode": self.safe_mode,
            "daily_loss_breached": self.daily_loss_breached,
            "weekly_drawdown_breached": self.weekly_drawdown_breached,
        }

    def _record(
        self,
        allowed: bool,
        reasons: list,
        audit_reference: Optional[str],
        account_mode: Optional[str],
        call_time: datetime,
        quote_age_seconds: Optional[float] = None,
        intent_age_seconds: Optional[float] = None,
    ) -> GatewayResult:
        result = GatewayResult(
            allowed=allowed,
            status=STATUS_ALLOW if allowed else STATUS_REJECT,
            reasons=reasons,
            audit_reference=audit_reference,
            account_mode=account_mode,
            safety_state=self._safety_state(),
            quote_age_seconds=quote_age_seconds,
            intent_age_seconds=intent_age_seconds,
            timestamp=call_time.isoformat(),
        )
        self._audit_log.append(result.to_dict())
        return result

    def _reject(
        self,
        reasons: list,
        audit_reference: Optional[str],
        account_mode: Optional[str],
        call_time: datetime,
        quote_age_seconds: Optional[float] = None,
        intent_age_seconds: Optional[float] = None,
    ) -> GatewayResult:
        return self._record(
            allowed=False,
            reasons=reasons,
            audit_reference=audit_reference,
            account_mode=account_mode,
            call_time=call_time,
            quote_age_seconds=quote_age_seconds,
            intent_age_seconds=intent_age_seconds,
        )

    def _allow(
        self,
        audit_reference: Optional[str],
        account_mode: Optional[str],
        call_time: datetime,
        quote_age_seconds: Optional[float] = None,
        intent_age_seconds: Optional[float] = None,
    ) -> GatewayResult:
        return self._record(
            allowed=True,
            reasons=[],
            audit_reference=audit_reference,
            account_mode=account_mode,
            call_time=call_time,
            quote_age_seconds=quote_age_seconds,
            intent_age_seconds=intent_age_seconds,
        )
