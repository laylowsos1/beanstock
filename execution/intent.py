"""Beanstock deterministic Execution Intent layer.

This is the mandatory chokepoint between a validated trading decision and
any future broker adapter:

    AI research -> TradeProposal -> schema validation -> action routing
        -> deterministic risk engine -> DecisionResult -> ExecutionIntent
        -> (future) broker adapter

No broker adapter exists yet. This module makes no network calls,
connects to no broker (moomoo or otherwise), and contains no live-trading
path -- it only ever decides whether an ExecutionIntent, a plain data
record describing an *intended* order, is allowed to exist in memory.

Security model
--------------
create_execution_intent() is the ONLY way to obtain an ExecutionIntent
with execution_allowed=True. It does not accept a pre-built "decision" or
"approval" object from the caller -- it takes the same raw inputs as
models.trade_proposal.evaluate_trade_proposal (the TradeProposal and the
account-state numbers) and calls that deterministic pipeline itself,
every time. This means:

 - There is no parameter, field, or code path by which an AI-authored
   string, a stray "execution_allowed": True key on a proposal dict, or a
   fabricated "decision" object can cause execution_allowed to become
   True. The only thing that can do that is the real, freshly-run
   risk.validator.validate_trade (for BUY/ADD) or the real REDUCE/EXIT
   eligibility checks in models.trade_proposal.
 - account_mode is re-checked independently in this module (not merely
   trusted from the upstream decision), because REDUCE/EXIT never pass
   through the entry risk engine's PAPER-mode gate (rules 1/2) at all --
   without this second check, a REDUCE/EXIT could otherwise slip through
   in a non-paper account_mode.
 - instrument_type is re-checked independently for "option" as
   defense-in-depth, even though the entry risk engine already rejects
   options for BUY/ADD -- so a future change to the risk engine (or a
   spoofed decision passed to the internal helpers directly) cannot, by
   itself, cause an option ExecutionIntent to be created.
 - execution_allowed is not a normal constructor argument
   (dataclasses.field(init=False, default=False)) -- ExecutionIntent(...,
   execution_allowed=True) raises TypeError. The only way to obtain an
   instance with execution_allowed=True is the private
   ExecutionIntent._create_approved() classmethod, which is gated by a
   module-private token that only this file holds, and which only
   create_execution_intent() ever calls. Ordinary caller code -- and an
   AI writing a TradeProposal -- has no path to True other than passing
   every deterministic check in this module and in models.trade_proposal.
 - audit_reference is always recomputed here from the actual decision
   content (a SHA-256 digest); it is never read from the proposal, so a
   forged "audit_reference" value on an incoming proposal dict has no
   effect on the real one produced.

Every call -- approved or rejected -- returns an ExecutionIntentResult,
which carries an audit_reference and the full underlying decision, so
rejected execution attempts leave the same audit trail as approved ones.
"""

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional, Union
import hashlib
import json

from models.trade_proposal import TradeProposal, evaluate_trade_proposal
from risk.validator import PAPER_MODES

DISALLOWED_INSTRUMENT_TYPES = {"option"}

_BUY_ADD_ORDER_TYPE = "LIMIT"
_REDUCE_EXIT_ORDER_TYPE = "MARKET"


class _ApprovalToken:
    """Unforgeable-by-import marker. Only this module can hold a
    reference to _THE_APPROVAL_TOKEN, so only code in this module can
    successfully call ExecutionIntent._create_approved().
    """


_THE_APPROVAL_TOKEN = _ApprovalToken()


@dataclass(frozen=True)
class ExecutionIntent:
    """A record of an order Beanstock intends to place -- not an order
    that has been placed. No broker adapter consumes this yet.

    execution_allowed is init=False: it is not a constructor argument at
    all, so `ExecutionIntent(..., execution_allowed=True)` raises
    TypeError. Every instance starts at execution_allowed=False; the only
    way to flip it is `ExecutionIntent._create_approved()`, which requires
    this module's private approval token.
    """

    ticker: str
    action: str
    instrument_type: str
    quantity: Optional[float]
    dollar_amount: Optional[float]
    intended_order_type: str
    reference_price: Optional[float]
    stop_price: Optional[float]
    target_price: Optional[float]
    decision_status: str
    audit_reference: str
    created_at: str
    account_mode: str
    execution_allowed: bool = field(init=False, default=False)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def _create_approved(cls, _token: "_ApprovalToken", **kwargs) -> "ExecutionIntent":
        """Internal only. Produces an ExecutionIntent with
        execution_allowed=True. Requires possession of this module's
        private _THE_APPROVAL_TOKEN; anything else raises PermissionError.
        Callers should never use this directly -- use
        execution.intent.create_execution_intent() instead, which is the
        only code that calls it.
        """
        if _token is not _THE_APPROVAL_TOKEN:
            raise PermissionError(
                "ExecutionIntent cannot be approved directly; only "
                "execution.intent.create_execution_intent() may do so."
            )
        instance = cls(**kwargs)
        object.__setattr__(instance, "execution_allowed", True)
        return instance


@dataclass
class ExecutionIntentResult:
    """Outcome of an execution-intent attempt -- always returned, whether
    or not an ExecutionIntent was actually created. This is the audit
    trail for rejected execution attempts (requirement 14): `intent` is
    None on rejection, but `audit_reference`, `decision_status`,
    `reasons`, and the full `decision` are still populated.
    """

    created: bool
    intent: Optional[ExecutionIntent]
    audit_reference: str
    decision_status: Optional[str]
    reasons: list
    decision: dict

    def to_dict(self) -> dict:
        return asdict(self)


def _audit_reference(decision: dict, account_mode: str) -> str:
    """Deterministic audit reference derived from the decision content
    itself (not randomness), so the same inputs always produce the same
    reference and it can be recomputed for verification later.
    """
    payload = json.dumps(
        {"decision": decision, "account_mode": account_mode},
        sort_keys=True,
        default=str,
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"audit-{digest[:16]}"


def _quantity(dollar_amount: Optional[float], reference_price: Optional[float]) -> Optional[float]:
    if dollar_amount is None or reference_price is None:
        return None
    try:
        reference_price = float(reference_price)
    except (TypeError, ValueError):
        return None
    if reference_price <= 0:
        return None
    return dollar_amount / reference_price


def _action_label(decision: dict, normalized: dict) -> Optional[str]:
    status = decision.get("decision")
    if status == "APPROVE":
        # BUY vs ADD is only distinguishable via the schema-normalized
        # proposal action (validate_trade_proposal already uppercases it).
        return normalized.get("action")
    if status in ("REDUCE_ALLOWED", "EXIT_ALLOWED"):
        return status.split("_ALLOWED")[0]
    return None


def create_execution_intent(
    proposal: Union["TradeProposal", dict],
    *,
    account_mode: str = "PAPER",
    account_equity: Optional[float] = None,
    available_cash: Optional[float] = None,
    current_positions: Optional[int] = None,
    trades_this_week: Optional[int] = None,
    current_company_exposure_pct: Optional[float] = None,
    current_sector_exposure_pct: Optional[float] = None,
    blocked_sectors: Optional[list] = None,
    has_existing_position: bool = False,
) -> ExecutionIntentResult:
    """Run the full deterministic pipeline and, only if every gate passes,
    produce an ExecutionIntent. Always returns an ExecutionIntentResult,
    approved or not, so rejected attempts are auditable too.

    This function -- not the caller -- decides execution_allowed. It
    takes raw inputs (the proposal and account-state numbers), not a
    pre-computed decision, so nothing the AI writes into the proposal can
    set execution_allowed directly. `proposal` must be a TradeProposal or
    a dict describing one -- a bare string (e.g. "APPROVED") or any other
    type is rejected here rather than being treated as a decision.
    """
    try:
        decision = evaluate_trade_proposal(
            proposal,
            account_equity=account_equity,
            available_cash=available_cash,
            current_positions=current_positions,
            trades_this_week=trades_this_week,
            current_company_exposure_pct=current_company_exposure_pct,
            current_sector_exposure_pct=current_sector_exposure_pct,
            blocked_sectors=blocked_sectors,
            account_mode=account_mode,
            has_existing_position=has_existing_position,
        )
    except TypeError:
        # proposal was not a TradeProposal or dict (e.g. a bare string or
        # some other AI-authored value) -- reject, do not raise, so this
        # is auditable like any other rejected attempt.
        audit_reference = _audit_reference(
            {"rejected_input_type": type(proposal).__name__}, account_mode
        )
        return ExecutionIntentResult(
            created=False,
            intent=None,
            audit_reference=audit_reference,
            decision_status=None,
            reasons=[
                f"proposal must be a TradeProposal or dict; got "
                f"{type(proposal).__name__!r}, which cannot be evaluated."
            ],
            decision={},
        )

    return _build_result_from_decision(decision, account_mode)


def _build_result_from_decision(decision: dict, account_mode: str) -> ExecutionIntentResult:
    normalized = decision.get("normalized_proposal") or {}
    decision_status = decision.get("decision")
    audit_reference = _audit_reference(decision, account_mode)
    reasons: list = []

    # --- Rules 5/6/7/8: HOLD, DO_NOTHING, and any rejection never create
    #     an ExecutionIntent. ---
    eligible_statuses = {"APPROVE", "REDUCE_ALLOWED", "EXIT_ALLOWED"}
    if decision_status not in eligible_statuses:
        if decision_status == "NO_ACTION":
            reasons.append("HOLD/DO_NOTHING never produces an ExecutionIntent.")
        else:
            reasons.extend(decision.get("schema_errors") or [])
            reasons.extend(decision.get("reasons") or [])
            risk_result = decision.get("risk_result")
            if risk_result:
                reasons.extend(risk_result.get("rejection_reasons") or [])
        if not reasons:
            reasons.append(f"Decision '{decision_status}' does not permit execution.")
        return ExecutionIntentResult(
            created=False,
            intent=None,
            audit_reference=audit_reference,
            decision_status=decision_status,
            reasons=reasons,
            decision=decision,
        )

    # --- decision.approved is the deterministic source of truth; a
    #     mismatched "APPROVE"-shaped status with approved != True cannot
    #     happen from evaluate_trade_proposal, but this module never
    #     trusts the status string alone. ---
    if decision.get("approved") is not True:
        reasons.append("Decision status implied approval but approved was not True.")
        return ExecutionIntentResult(
            created=False,
            intent=None,
            audit_reference=audit_reference,
            decision_status=decision_status,
            reasons=reasons,
            decision=decision,
        )

    # --- Rule 9: live/non-paper account_mode never creates an
    #     ExecutionIntent, independent of what the risk engine did or
    #     didn't check (REDUCE/EXIT never pass through it). ---
    mode = (account_mode or "").strip().upper()
    if mode not in PAPER_MODES:
        reasons.append(
            f"account_mode='{account_mode}' is not PAPER/SIMULATED; "
            "ExecutionIntent may only be created in paper/simulated mode "
            "under current Beanstock rules."
        )
        return ExecutionIntentResult(
            created=False,
            intent=None,
            audit_reference=audit_reference,
            decision_status=decision_status,
            reasons=reasons,
            decision=decision,
        )

    # --- Rule 10: options never create an ExecutionIntent, independent
    #     of decision outcome (defense-in-depth on top of risk rules 3/4).
    instrument_type = (normalized.get("instrument_type") or "").strip().lower()
    if instrument_type in DISALLOWED_INSTRUMENT_TYPES:
        reasons.append(
            f"instrument_type='{instrument_type}' is not supported for execution "
            "under current Beanstock rules."
        )
        return ExecutionIntentResult(
            created=False,
            intent=None,
            audit_reference=audit_reference,
            decision_status=decision_status,
            reasons=reasons,
            decision=decision,
        )

    action = _action_label(decision, normalized)
    if not action:
        reasons.append("Could not determine a valid action for this approved decision.")
        return ExecutionIntentResult(
            created=False,
            intent=None,
            audit_reference=audit_reference,
            decision_status=decision_status,
            reasons=reasons,
            decision=decision,
        )

    ticker = normalized.get("ticker")
    if not (isinstance(ticker, str) and ticker.strip()):
        reasons.append("Missing ticker; cannot construct ExecutionIntent.")
        return ExecutionIntentResult(
            created=False,
            intent=None,
            audit_reference=audit_reference,
            decision_status=decision_status,
            reasons=reasons,
            decision=decision,
        )

    if action in ("BUY", "ADD"):
        order_type = _BUY_ADD_ORDER_TYPE
        reference_price = normalized.get("intended_entry")
        stop_price = normalized.get("stop_price")
        target_price = normalized.get("target_price")
    else:  # REDUCE / EXIT
        order_type = _REDUCE_EXIT_ORDER_TYPE
        reference_price = normalized.get("current_price") or normalized.get("intended_entry")
        stop_price = normalized.get("stop_price")
        target_price = normalized.get("target_price")

    dollar_amount = normalized.get("proposed_dollar_amount")
    if dollar_amount is not None and not isinstance(dollar_amount, (int, float)):
        dollar_amount = None
    quantity = _quantity(dollar_amount, reference_price)

    intent = ExecutionIntent._create_approved(
        _THE_APPROVAL_TOKEN,
        ticker=ticker,
        action=action,
        instrument_type=instrument_type,
        quantity=quantity,
        dollar_amount=dollar_amount,
        intended_order_type=order_type,
        reference_price=reference_price,
        stop_price=stop_price,
        target_price=target_price,
        decision_status=decision_status,
        audit_reference=audit_reference,
        created_at=datetime.now(timezone.utc).isoformat(),
        account_mode=mode,
    )

    return ExecutionIntentResult(
        created=True,
        intent=intent,
        audit_reference=audit_reference,
        decision_status=decision_status,
        reasons=[],
        decision=decision,
    )
