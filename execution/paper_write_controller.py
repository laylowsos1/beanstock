"""Beanstock PaperWriteController -- the extra arming gate between
BrokerGateway and MoomooPaperBroker.

    ExecutionIntent -> BrokerGateway -> PaperWriteController -> MoomooPaperBroker
        -> SIMULATED US account only

Even once trade:write is eventually granted on the OAuth token, this
module means a single approved ExecutionIntent still cannot reach a
real HTTP write without ALL of the following being true at the moment
of submission (re-checked fresh, every time -- nothing here is cached
from an earlier decision):

  - Beanstock mode is PAPER/SIMULATED (never LIVE)
  - BEANSTOCK_PAPER_WRITE_ENABLED == True (the same flag
    broker/moomoo_paper.py itself already gates on -- this controller
    re-checks it independently, it does not just trust the broker to)
  - This controller's own state == ARMED (defaults DISARMED; arming is
    a separate, explicit, manual action -- see arm()/disarm())
  - BrokerGateway.validate() approves the intent against the broker's
    current live state
  - The simulated US account resolves cleanly, and the write path this
    would use passes this module's OWN path firewall (see below --
    independent of broker.moomoo_paper's own _guard_path)
  - The quote is valid and fresh, and the intent itself is not stale
  - SAFE_MODE / daily-loss / weekly-drawdown gates all clear for
    exposure-increasing actions (BUY/ADD); REDUCE/EXIT are never
    blocked by these
  - audit_reference has not already been processed by this controller

Everything above is independently re-verified here even though
BrokerGateway and MoomooPaperBroker already check most of it -- that
duplication is the point: this module assumes both of those could
individually have a bug, and refuses to be the single point of failure
between an approved decision and a real order.

Path firewall (item 3)
------------------------
guard_read_path()/guard_write_path() are pure functions with their own
independently-defined allowlists -- they do not import
broker.moomoo_readonly.ALLOWED_PATH_PREFIXES or call
broker.moomoo_paper.MoomooPaperBroker._guard_path. If someone edits
that broker-side allowlist by mistake, this firewall still catches a
live-account write attempt on its own. The controller applies
guard_write_path() to the exact path MoomooPaperBroker would use before
ever calling it.

FIRST_ORDER_TEST_MODE (items 4-5)
-----------------------------------
A special, extra-cautious mode for the very first real connectivity
test: exactly one submission attempt per arm() call (auto-disarms
immediately after that attempt's result -- success, business rejection,
or exception, alike), and every exposure-increasing order is capped at
FIRST_TEST_MAX_NOTIONAL ($25 by default), computed on whole shares only
-- never rounded up. If the smallest tradable whole-share quantity would
cost more than the cap, the order is rejected; this module never
rounds up to "make it fit."

Fractional shares (item 6)
-----------------------------
NOT VERIFIED for moomoo's OpenAPI simulated Place Order endpoint. See
broker/MOOMOO_PAPER_ORDER_CONTRACT.md and the note below -- general
moomoo brokerage-app fractional-share trading is real and documented,
but at a different product surface than the OpenAPI this project
targets, and nothing in the OpenAPI's own docs (including the full
open.moomoo.com documentation index) mentions fractional or decimal
quantities for US-stock sim-trade orders. Fractional stays BLOCKED here
exactly as it already is in broker/moomoo_paper.py -- this controller
adds an independent second check of the same fail-closed rule rather
than only relying on the broker to enforce it.
"""

from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
from typing import Optional
import os

from broker.moomoo_readonly import OPEN_ORDERS_PATH_TEMPLATE
from broker.moomoo_paper import ENV_WRITE_ENABLED
from execution.intent import ExecutionIntent
from risk.validator import PAPER_MODES

STATE_DISARMED = "DISARMED"
STATE_ARMED = "ARMED"

STATUS_ALLOWED = "ALLOWED"
STATUS_BLOCKED = "BLOCKED"

ALLOWED_ACTIONS = {"BUY", "ADD", "REDUCE", "EXIT"}
EXPOSURE_INCREASING_ACTIONS = {"BUY", "ADD"}

REJECT_DUPLICATE = "REJECT_DUPLICATE"

# Placeholder defaults, matching broker.gateway.BrokerGateway /
# broker.moomoo_paper.MoomooPaperBroker's own defaults for the same
# reason -- not market-derived, just this project's current safe window.
DEFAULT_MAX_INTENT_AGE_SECONDS = 300.0
DEFAULT_MAX_QUOTE_AGE_SECONDS = 60.0

DEFAULT_FIRST_TEST_MAX_NOTIONAL = Decimal("25")

# ---------------------------------------------------------------------
# Independent transport-level path firewall (item 3). Deliberately
# self-contained -- no import from broker.moomoo_readonly's or
# broker.moomoo_paper's own allowlists.
# ---------------------------------------------------------------------

ALLOWED_READ_PATH_PREFIXES = ("/api/v1.0/sim-trade/", "/api/v1.0/quote/")
ALLOWED_WRITE_PATH_PREFIXES = ("/api/v1.0/sim-trade/",)

# Explicitly documented as rejected, not just "anything else falls
# through" -- these are real, documented live-account/legacy path
# families this controller must never treat as acceptable.
EXPLICITLY_REJECTED_PATH_PREFIXES = ("/api/v1.0/trade/", "/api/v1.0/accounts/")


class PathFirewallViolation(Exception):
    """Raised by guard_read_path()/guard_write_path() for any path
    outside this module's own allowlist -- including every explicitly
    rejected prefix and any path family this firewall simply does not
    recognize."""


def guard_read_path(path: str) -> None:
    if not any(path.startswith(prefix) for prefix in ALLOWED_READ_PATH_PREFIXES):
        raise PathFirewallViolation(
            f"Read path {path!r} is not in PaperWriteController's read-path firewall "
            f"({ALLOWED_READ_PATH_PREFIXES!r})."
        )


def guard_write_path(path: str) -> None:
    if not any(path.startswith(prefix) for prefix in ALLOWED_WRITE_PATH_PREFIXES):
        raise PathFirewallViolation(
            f"Write path {path!r} is not in PaperWriteController's write-path firewall "
            f"({ALLOWED_WRITE_PATH_PREFIXES!r}); only documented sim-trade write paths "
            "are ever permitted."
        )


# ---------------------------------------------------------------------
# Small helpers (deliberately duplicated per this project's existing
# per-module convention -- see broker/moomoo_paper.py's own docstring).
# ---------------------------------------------------------------------


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


def _age_seconds_from_datetime(moment: Optional[datetime], now: datetime) -> Optional[float]:
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return (now - moment).total_seconds()


def _intent_age_seconds(created_at: Optional[str], now: datetime) -> Optional[float]:
    if not created_at:
        return None
    try:
        parsed = datetime.fromisoformat(created_at)
    except (TypeError, ValueError):
        return None
    return _age_seconds_from_datetime(parsed, now)


@dataclass
class ControllerResult:
    """Structured outcome of one PaperWriteController.submit() call --
    the audit record for both allowed and blocked attempts.
    """

    allowed: bool
    status: str  # ALLOWED | BLOCKED
    reasons: list
    audit_reference: Optional[str]
    controller_state: str
    write_enabled: bool
    first_order_test_mode: bool
    timestamp: str

    def to_dict(self) -> dict:
        return asdict(self)


class PaperWriteController:
    """Defaults to DISARMED. arm()/disarm() are the only ways to change
    that -- like broker.gateway.BrokerGateway's safe_mode and
    broker.moomoo_paper.MoomooPaperBroker's own safe_mode/daily_loss_breached/
    weekly_drawdown_breached, this project's own daily/weekly review
    process (a human, or a deterministic scheduled job -- never the AI
    research layer) is responsible for calling arm().
    """

    def __init__(
        self,
        *,
        write_enabled: Optional[bool] = None,
        first_order_test_mode: bool = False,
        first_test_max_notional: Decimal = DEFAULT_FIRST_TEST_MAX_NOTIONAL,
        max_intent_age_seconds: float = DEFAULT_MAX_INTENT_AGE_SECONDS,
        max_quote_age_seconds: float = DEFAULT_MAX_QUOTE_AGE_SECONDS,
    ):
        self._state = STATE_DISARMED

        if write_enabled is None:
            write_enabled = _env_flag_enabled(os.environ.get(ENV_WRITE_ENABLED))
        self._write_enabled = bool(write_enabled)

        self.first_order_test_mode = first_order_test_mode
        self.first_test_max_notional = _to_decimal(first_test_max_notional) or DEFAULT_FIRST_TEST_MAX_NOTIONAL
        self.max_intent_age_seconds = max_intent_age_seconds
        self.max_quote_age_seconds = max_quote_age_seconds

        # This controller's OWN copy of these flags -- independent of
        # BrokerGateway's and MoomooPaperBroker's own copies, by design.
        self.safe_mode = False
        self.daily_loss_breached = False
        self.weekly_drawdown_breached = False

        self._processed_audit_references: set = set()
        self._audit_log: list = []

    @property
    def state(self) -> str:
        return self._state

    @property
    def write_enabled(self) -> bool:
        return self._write_enabled

    def arm(self) -> None:
        """Explicit, manual arming. Arming never bypasses any other
        gate -- every check in submit() still runs, every time. In
        FIRST_ORDER_TEST_MODE, arming resets this controller's
        single-attempt allowance: exactly one submission attempt is
        permitted per arm() call, after which it auto-disarms again
        regardless of that attempt's outcome.
        """
        self._state = STATE_ARMED

    def disarm(self) -> None:
        self._state = STATE_DISARMED

    def get_audit_log(self) -> list:
        return list(self._audit_log)

    # -----------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------

    def submit(self, intent, gateway, broker):
        """Returns (ControllerResult, Order | None). Order is None
        whenever blocked -- the broker is never touched in that case.
        Never swallows an exception from the broker call itself: on a
        timeout or any other uncertain result, this fails closed by
        propagating the exception (after recording the attempt and,
        in FIRST_ORDER_TEST_MODE, auto-disarming) rather than treating
        it as either success or a clean, retriable rejection.
        """
        now = datetime.now(timezone.utc)
        audit_reference = getattr(intent, "audit_reference", None)

        if not isinstance(intent, ExecutionIntent):
            return self._reject(
                [f"intent must be a hardened ExecutionIntent produced by "
                 f"execution.intent.create_execution_intent(); got {type(intent).__name__!r}."],
                None,
                now,
            ), None

        # --- duplicate check -- this controller's own independent
        #     tracking, separate from the broker's. ---
        if audit_reference is not None and audit_reference in self._processed_audit_references:
            return self._reject(
                [f"{REJECT_DUPLICATE}: audit_reference {audit_reference!r} has already "
                 "been processed by PaperWriteController."],
                audit_reference,
                now,
            ), None

        # --- ARMED gate ---
        if self._state != STATE_ARMED:
            return self._reject([f"PaperWriteController is {self._state}, not ARMED."], audit_reference, now), None

        # --- write-enabled flag -- independent re-check of the exact
        #     same env-var-backed flag broker.moomoo_paper.MoomooPaperBroker
        #     itself gates on. ---
        if not self._write_enabled:
            return self._reject([f"{ENV_WRITE_ENABLED} is False."], audit_reference, now), None

        # --- execution_allowed / account_mode -- LIVE mode can never
        #     get past this, regardless of arming or the write flag. ---
        if intent.execution_allowed is not True:
            return self._reject(["execution_allowed is not True."], audit_reference, now), None

        mode = (intent.account_mode or "").strip().upper()
        if mode not in PAPER_MODES:
            return self._reject(
                [f"account_mode={intent.account_mode!r} is not PAPER/SIMULATED; LIVE mode "
                 "can never arm or pass through this controller."],
                audit_reference,
                now,
            ), None

        action = (intent.action or "").strip().upper()
        if action not in ALLOWED_ACTIONS:
            return self._reject([f"action={intent.action!r} is not executable."], audit_reference, now), None

        # --- instrument_type -- independent second check of the same
        #     fail-closed rule broker.moomoo_paper.MoomooPaperBroker
        #     already enforces: fractional-share execution is NOT
        #     VERIFIED against moomoo's simulated Place Order endpoint
        #     (see this module's docstring and
        #     broker/MOOMOO_PAPER_ORDER_CONTRACT.md), so it stays
        #     blocked here too rather than trusting the broker alone. ---
        instrument_type = (intent.instrument_type or "").strip().lower()
        if instrument_type != "stock":
            return self._reject(
                [f"instrument_type={intent.instrument_type!r} is not supported -- "
                 "PaperWriteController only permits whole-share 'stock' orders. "
                 "Fractional-share execution is NOT VERIFIED; failing closed."],
                audit_reference,
                now,
            ), None

        # --- SAFE_MODE / daily-loss / weekly-drawdown -- only ever
        #     block exposure-increasing actions. ---
        if action in EXPOSURE_INCREASING_ACTIONS:
            if self.safe_mode:
                return self._reject(
                    ["SAFE_MODE is active; BUY/ADD blocked (REDUCE/EXIT remain permitted)."],
                    audit_reference,
                    now,
                ), None
            if self.daily_loss_breached:
                return self._reject(
                    ["Daily loss threshold breached; BUY/ADD blocked."], audit_reference, now
                ), None
            if self.weekly_drawdown_breached:
                return self._reject(
                    ["Weekly drawdown threshold breached; BUY/ADD blocked."], audit_reference, now
                ), None

        # --- stale intent ---
        intent_age = _intent_age_seconds(intent.created_at, now)
        if intent_age is None:
            return self._reject(["intent created_at is missing or unparseable."], audit_reference, now), None
        if intent_age > self.max_intent_age_seconds:
            return self._reject(
                [f"Intent is {intent_age:.1f}s old, exceeding the {self.max_intent_age_seconds:.1f}s safe window."],
                audit_reference,
                now,
            ), None

        # --- BrokerGateway must independently approve too. ---
        gateway_result = gateway.validate(intent, broker)
        if not gateway_result.allowed:
            return self._reject(
                [f"BrokerGateway rejected the intent: {gateway_result.reasons}"], audit_reference, now
            ), None

        # --- resolve the simulated US account, and firewall the exact
        #     write path MoomooPaperBroker would use -- independent of
        #     that broker's own path guard. ---
        try:
            account_id = broker.resolve_simulated_us_account_id()
        except Exception as exc:
            return self._reject([f"Could not resolve a simulated US account: {exc!r}"], audit_reference, now), None

        write_path = OPEN_ORDERS_PATH_TEMPLATE.format(acc_id=account_id)
        try:
            guard_write_path(write_path)
        except PathFirewallViolation as exc:
            return self._reject([str(exc)], audit_reference, now), None

        # --- quote freshness -- independent re-check. ---
        ticker = (intent.ticker or "").strip().upper() if intent.ticker else None
        if not ticker:
            return self._reject(["Missing or invalid ticker."], audit_reference, now), None

        quote = broker.get_quote(ticker)
        if not _is_valid_price(quote):
            return self._reject([f"No valid current quote for {ticker!r}."], audit_reference, now), None
        quote_ts = broker.get_quote_timestamp(ticker)
        quote_age = _age_seconds_from_datetime(quote_ts, now)
        if quote_age is None:
            return self._reject(
                [f"Quote for {ticker!r} has no timestamp; cannot verify freshness."], audit_reference, now
            ), None
        if quote_age > self.max_quote_age_seconds:
            return self._reject(
                [f"Quote for {ticker!r} is {quote_age:.1f}s old, exceeding the "
                 f"{self.max_quote_age_seconds:.1f}s freshness window."],
                audit_reference,
                now,
            ), None

        # --- FIRST_ORDER_TEST_MODE notional cap -- whole shares only,
        #     never rounded up to fit. ---
        if self.first_order_test_mode and action in EXPOSURE_INCREASING_ACTIONS:
            dollar_amount = _to_decimal(intent.dollar_amount)
            if dollar_amount is None or dollar_amount <= 0:
                return self._reject(
                    [f"dollar_amount must be a positive number for a first-order test, got "
                     f"{intent.dollar_amount!r}."],
                    audit_reference,
                    now,
                ), None
            if dollar_amount > self.first_test_max_notional:
                return self._reject(
                    [f"dollar_amount ${dollar_amount} exceeds FIRST_TEST_MAX_NOTIONAL "
                     f"${self.first_test_max_notional}."],
                    audit_reference,
                    now,
                ), None
            whole_shares = (dollar_amount / quote).to_integral_value(rounding=ROUND_FLOOR)
            if whole_shares < 1:
                return self._reject(
                    [f"${dollar_amount} at a quote of ${quote} rounds down to 0 whole shares "
                     f"within the ${self.first_test_max_notional} first-test cap; refusing to "
                     "round up. Use a cheaper test symbol, or verified fractional-share support "
                     "(see broker/MOOMOO_PAPER_ORDER_CONTRACT.md -- currently NOT VERIFIED)."],
                    audit_reference,
                    now,
                ), None
            smallest_cost = whole_shares * quote
            if smallest_cost > self.first_test_max_notional:
                return self._reject(
                    [f"The smallest whole-share order (${smallest_cost}) exceeds the "
                     f"${self.first_test_max_notional} first-test cap; refusing to round up."],
                    audit_reference,
                    now,
                ), None

        # All gates passed. This is the ONLY point in this class that
        # ever calls the broker.
        attempting_first_order = self.first_order_test_mode
        try:
            order = broker.submit_execution_intent(intent)
        except Exception:
            # Fail closed: an uncertain result. Auto-disarm (if in
            # first-order-test mode) and never retry automatically --
            # mark the audit_reference consumed too, since we genuinely
            # don't know whether an order was created.
            if audit_reference is not None:
                self._processed_audit_references.add(audit_reference)
            if attempting_first_order:
                self.disarm()
            raise

        # A clean, certain REJECTED result means nothing was sent --
        # the same audit_reference may legitimately be retried later.
        # Anything else (FILLED/PENDING/CANCELED) means an order really
        # was created, so it's consumed for good.
        if order.status != "REJECTED" and audit_reference is not None:
            self._processed_audit_references.add(audit_reference)

        if attempting_first_order:
            # "after the first order attempt/result" -- disarm
            # regardless of whether the broker filled, rejected, or
            # anything else. Exactly one attempt per arm() call.
            self.disarm()

        result = ControllerResult(
            allowed=True,
            status=STATUS_ALLOWED,
            reasons=[],
            audit_reference=audit_reference,
            controller_state=self._state,
            write_enabled=self._write_enabled,
            first_order_test_mode=self.first_order_test_mode,
            timestamp=now.isoformat(),
        )
        self._audit_log.append({**result.to_dict(), "order_status": order.status, "order_id": order.order_id})
        return result, order

    # -----------------------------------------------------------------
    # Internal
    # -----------------------------------------------------------------

    def _reject(self, reasons: list, audit_reference: Optional[str], now: datetime) -> ControllerResult:
        result = ControllerResult(
            allowed=False,
            status=STATUS_BLOCKED,
            reasons=reasons,
            audit_reference=audit_reference,
            controller_state=self._state,
            write_enabled=self._write_enabled,
            first_order_test_mode=self.first_order_test_mode,
            timestamp=now.isoformat(),
        )
        self._audit_log.append(result.to_dict())
        return result
