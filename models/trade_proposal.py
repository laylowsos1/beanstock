"""Beanstock structured Trade Proposal schema.

This is the mandatory chokepoint between AI research output and the
deterministic risk engine (risk/validator.py). AI/Claude research must be
converted into a validated, typed TradeProposal here -- free-form prose
never reaches the risk engine or a broker directly.

Design rules:
 - Every field the project owner asked to document before a proposed buy
   is a required, typed field on TradeProposal (see CLAUDE.md, "Before Any
   Proposed Buy"). Missing or malformed values fail schema validation
   before the risk engine is ever consulted.
 - reward:risk is never trusted from the AI. It is always recalculated
   here from intended_entry/stop_price/target_price, and the calculated
   value overrides whatever was supplied (a disagreement is recorded as a
   warning, not silently dropped).
 - instrument_type intentionally allows "option" to pass *schema*
   validation (it is a structurally well-formed proposal) so that the
   *risk engine* -- not this layer -- is the single place that enforces
   "no options" under current Beanstock rules. See evaluate_trade_proposal.

Action routing (evaluate_trade_proposal):
 - BUY / ADD: full schema validation, then the full deterministic entry
   risk engine (risk.validator.validate_trade) -- every hard rule applies,
   including company/sector exposure, cash, position sizing, and
   no-averaging-down-on-decline-alone.
 - REDUCE: risk-reducing action. Never enters the entry risk engine (so
   it can never be blocked by candidate-score/catalyst/reward:risk entry
   gates). Requires an existing position and requires the proposed
   post-trade allocation to be strictly lower than the current one.
 - EXIT: risk-reducing action. Never enters the entry risk engine, so it
   can never be blocked by candidate score, catalyst, sector cap, or the
   new-trades-per-week limit, and an existing concentration breach can
   never prevent an exit. Requires only that a position currently exists.
 - HOLD / DO_NOTHING: terminal, always NO_ACTION. Neither schema
   validation nor the entry risk engine runs, so these actions are
   structurally incapable of producing an execution request.

No network calls, no broker calls, no live trading exists in this module.
"""

from dataclasses import dataclass, fields, asdict
from typing import Any, Optional, Union
import json

from risk.validator import validate_trade

VALID_INSTRUMENT_TYPES = {"stock", "fractional_share", "option"}
VALID_ACTIONS = {"BUY", "ADD", "REDUCE", "EXIT", "HOLD", "DO_NOTHING"}

MIN_CANDIDATE_SCORE = 0
MAX_CANDIDATE_SCORE = 100
REWARD_RISK_DISAGREEMENT_TOLERANCE = 0.01


def _reward_risk_from_levels(entry: Optional[float], stop: Optional[float],
                              target: Optional[float]) -> Optional[float]:
    """Deterministic reward:risk for a long position.

    Returns None if any level is missing or the levels do not form a
    valid long risk structure (stop < entry < target).
    """
    if entry is None or stop is None or target is None:
        return None
    try:
        entry = float(entry)
        stop = float(stop)
        target = float(target)
    except (TypeError, ValueError):
        return None

    risk = entry - stop
    reward = target - entry
    if risk <= 0 or reward <= 0:
        return None
    return reward / risk


@dataclass
class TradeProposal:
    ticker: str
    instrument_type: str
    action: str
    current_price: float
    intended_entry: float
    candidate_score: float
    catalyst: str
    catalyst_timing: str
    bull_case: str
    bear_case: str
    thesis_invalidation: str
    stop_price: float
    target_price: float
    proposed_dollar_amount: float
    proposed_allocation_pct: float
    sector: str
    confidence: float
    holding_period: str
    reason_to_buy_now: str
    reason_to_wait: str
    data_timestamp: str
    reward_risk: Optional[float] = None
    averaging_down_on_decline: bool = False

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict())

    @classmethod
    def from_dict(cls, data: dict) -> "TradeProposal":
        known = {f.name for f in fields(cls)}
        missing_ok_filtered = {k: v for k, v in data.items() if k in known}
        return cls(**missing_ok_filtered)

    @classmethod
    def from_json(cls, data: str) -> "TradeProposal":
        return cls.from_dict(json.loads(data))


def validate_trade_proposal(proposal: Union["TradeProposal", dict]) -> dict:
    """Schema-validate a trade proposal.

    Accepts either a TradeProposal instance or a plain dict (the shape an
    AI research step would naturally produce before it is trusted).

    Returns:
        {
            "valid": bool,
            "errors": [str, ...],
            "warnings": [str, ...],
            "calculated_reward_risk": float | None,
            "proposal": dict,  # normalized: instrument_type/action
                                # normalized to canonical case, and
                                # reward_risk replaced by the calculated
                                # value (never the AI-supplied one)
        }
    """
    if isinstance(proposal, TradeProposal):
        data = proposal.to_dict()
    elif isinstance(proposal, dict):
        data = dict(proposal)
    else:
        raise TypeError("proposal must be a TradeProposal or dict")

    errors: list[str] = []
    warnings: list[str] = []

    def require_text(name: str) -> None:
        val = data.get(name)
        if not (isinstance(val, str) and val.strip()):
            errors.append(f"Missing required field: {name}")

    def require_number(name: str) -> Optional[float]:
        val = data.get(name)
        if val is None or isinstance(val, bool) or not isinstance(val, (int, float)):
            errors.append(f"Missing or non-numeric required field: {name}")
            return None
        return float(val)

    # --- ticker ---
    ticker = data.get("ticker")
    if not (isinstance(ticker, str) and ticker.strip()):
        errors.append("Missing required field: ticker")

    # --- instrument_type ---
    instrument_type = (data.get("instrument_type") or "").strip().lower()
    if instrument_type not in VALID_INSTRUMENT_TYPES:
        errors.append(f"Invalid instrument_type: {data.get('instrument_type')!r}")

    # --- action ---
    action = (data.get("action") or "").strip().upper()
    if action not in VALID_ACTIONS:
        errors.append(f"Invalid action: {data.get('action')!r}")

    # --- catalyst / durable thesis ---
    require_text("catalyst")
    require_text("thesis_invalidation")

    # --- remaining documented-decision text fields ---
    require_text("catalyst_timing")
    require_text("bull_case")
    require_text("bear_case")
    require_text("sector")
    require_text("holding_period")
    require_text("reason_to_buy_now")
    require_text("reason_to_wait")
    require_text("data_timestamp")

    # --- numeric fields ---
    current_price = require_number("current_price")
    intended_entry = require_number("intended_entry")
    stop_price = require_number("stop_price")
    target_price = require_number("target_price")
    candidate_score = require_number("candidate_score")
    proposed_dollar_amount = require_number("proposed_dollar_amount")
    proposed_allocation_pct = require_number("proposed_allocation_pct")
    confidence = require_number("confidence")

    for name, val in (
        ("current_price", current_price),
        ("intended_entry", intended_entry),
        ("stop_price", stop_price),
        ("target_price", target_price),
    ):
        if val is not None and val <= 0:
            errors.append(f"{name} must be a positive price, got {val}")

    if proposed_dollar_amount is not None and proposed_dollar_amount < 0:
        errors.append(
            f"proposed_dollar_amount must not be negative, got {proposed_dollar_amount}"
        )

    if proposed_allocation_pct is not None and not (0 <= proposed_allocation_pct <= 100):
        errors.append(
            f"proposed_allocation_pct must be within 0-100, got {proposed_allocation_pct}"
        )

    if candidate_score is not None and not (
        MIN_CANDIDATE_SCORE <= candidate_score <= MAX_CANDIDATE_SCORE
    ):
        errors.append(f"candidate_score must be within 0-100, got {candidate_score}")

    if confidence is not None and not (0 <= confidence <= 1):
        errors.append(f"confidence must be within 0-1, got {confidence}")

    # --- reward:risk: always recalculated, never trusted from the AI ---
    calculated_rr = _reward_risk_from_levels(intended_entry, stop_price, target_price)
    if calculated_rr is None and intended_entry is not None and stop_price is not None \
            and target_price is not None:
        errors.append(
            "stop_price/intended_entry/target_price do not form a valid long risk "
            "structure (requires stop_price < intended_entry < target_price)"
        )

    ai_supplied_rr = data.get("reward_risk")
    if ai_supplied_rr is not None and calculated_rr is not None:
        try:
            ai_rr = float(ai_supplied_rr)
            if abs(ai_rr - calculated_rr) > REWARD_RISK_DISAGREEMENT_TOLERANCE:
                warnings.append(
                    f"AI-supplied reward_risk={ai_rr} disagreed with deterministic "
                    f"reward_risk={calculated_rr}; calculated value overrides."
                )
        except (TypeError, ValueError):
            warnings.append(
                f"AI-supplied reward_risk={ai_supplied_rr!r} was not numeric; ignored."
            )

    normalized = dict(data)
    normalized["instrument_type"] = instrument_type
    normalized["action"] = action
    normalized["reward_risk"] = calculated_rr

    return {
        "valid": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
        "calculated_reward_risk": calculated_rr,
        "proposal": normalized,
    }


def to_risk_engine_trade(normalized_proposal: dict) -> dict:
    """Map a schema-validated, normalized proposal dict to the shape
    risk.validator.validate_trade expects as `proposed_trade`.

    Beanstock never shorts and this layer never touches margin, so side
    is always "long" and uses_margin is always False.
    """
    return {
        "symbol": normalized_proposal.get("ticker"),
        "instrument_type": normalized_proposal.get("instrument_type"),
        "side": "long",
        "uses_margin": False,
        "cost": normalized_proposal.get("proposed_dollar_amount"),
        "sector": normalized_proposal.get("sector"),
        "candidate_score": normalized_proposal.get("candidate_score"),
        "catalyst": normalized_proposal.get("catalyst"),
        "entry": normalized_proposal.get("intended_entry"),
        "stop": normalized_proposal.get("stop_price"),
        "target": normalized_proposal.get("target_price"),
        "averaging_down_on_decline": bool(
            normalized_proposal.get("averaging_down_on_decline", False)
        ),
    }


def _as_dict(proposal: Union["TradeProposal", dict]) -> dict:
    if isinstance(proposal, TradeProposal):
        return proposal.to_dict()
    if isinstance(proposal, dict):
        return dict(proposal)
    raise TypeError("proposal must be a TradeProposal or dict")


def _get_action(data: dict) -> str:
    return (data.get("action") or "").strip().upper()


def _evaluate_reduce(
    data: dict,
    has_existing_position: bool,
    current_company_exposure_pct: Optional[float],
) -> dict:
    """REDUCE: risk-reducing only. Never touches the entry risk engine, so
    it can never be blocked by candidate-score/catalyst/reward:risk entry
    gates -- only that a position exists and that exposure is decreasing.
    """
    reasons: list[str] = []

    ticker = data.get("ticker")
    if not (isinstance(ticker, str) and ticker.strip()):
        reasons.append("Missing required field: ticker")

    if not has_existing_position:
        reasons.append("REDUCE requires an existing position; none found for this ticker.")

    proposed_allocation_pct = data.get("proposed_allocation_pct")
    valid_allocation = isinstance(proposed_allocation_pct, (int, float)) and not isinstance(
        proposed_allocation_pct, bool
    )
    if not valid_allocation:
        reasons.append(
            "REDUCE requires a numeric proposed_allocation_pct representing the "
            "target allocation after the reduction."
        )
    elif current_company_exposure_pct is None:
        reasons.append(
            "REDUCE requires current_company_exposure_pct to verify exposure is decreasing."
        )
    elif float(proposed_allocation_pct) >= float(current_company_exposure_pct):
        reasons.append(
            f"REDUCE must decrease exposure: target allocation "
            f"{proposed_allocation_pct}% is not less than current exposure "
            f"{current_company_exposure_pct}%."
        )

    approved = len(reasons) == 0
    return {
        "stage": "reduce_evaluated",
        "approved": approved,
        "decision": "REDUCE_ALLOWED" if approved else "REJECT",
        "reasons": reasons,
        "risk_result": None,
        "normalized_proposal": data,
    }


def _evaluate_exit(data: dict, has_existing_position: bool) -> dict:
    """EXIT: risk-reducing only. Never touches the entry risk engine, so an
    exit can never be blocked by candidate score, catalyst, sector cap, or
    the new-trades-per-week limit, and an existing concentration breach can
    never prevent it. The only requirement is that a position exists.
    """
    reasons: list[str] = []

    ticker = data.get("ticker")
    if not (isinstance(ticker, str) and ticker.strip()):
        reasons.append("Missing required field: ticker")

    if not has_existing_position:
        reasons.append("EXIT requires an existing position; none found for this ticker.")

    approved = len(reasons) == 0
    return {
        "stage": "exit_evaluated",
        "approved": approved,
        "decision": "EXIT_ALLOWED" if approved else "REJECT",
        "reasons": reasons,
        "risk_result": None,
        "normalized_proposal": data,
    }


def evaluate_trade_proposal(
    proposal: Union["TradeProposal", dict],
    *,
    account_equity: Optional[float] = None,
    available_cash: Optional[float] = None,
    current_positions: Optional[int] = None,
    trades_this_week: Optional[int] = None,
    current_company_exposure_pct: Optional[float] = None,
    current_sector_exposure_pct: Optional[float] = None,
    blocked_sectors: Optional[list] = None,
    account_mode: str = "PAPER",
    has_existing_position: bool = False,
) -> dict:
    """Route a proposal by its action, then decide APPROVE / REJECT (or the
    risk-reducing / no-op equivalents). See the module docstring for the
    full routing table.

    Only BUY and ADD ever reach risk.validator.validate_trade (the entry
    risk engine). REDUCE and EXIT are evaluated independently of it.
    HOLD and DO_NOTHING return immediately with no validation of any kind,
    so they cannot produce an execution request.

    No broker calls, no network calls, no live trading path exists
    anywhere in this function.
    """
    data = _as_dict(proposal)
    action = _get_action(data)

    if action in ("HOLD", "DO_NOTHING"):
        return {
            "stage": "no_action",
            "approved": None,
            "decision": "NO_ACTION",
            "reasons": [],
            "risk_result": None,
            "normalized_proposal": data,
        }

    if action == "REDUCE":
        return _evaluate_reduce(data, has_existing_position, current_company_exposure_pct)

    if action == "EXIT":
        return _evaluate_exit(data, has_existing_position)

    # BUY, ADD, and any malformed/unrecognized action fall through to full
    # schema validation + the full entry risk engine. An invalid action
    # value is caught by validate_trade_proposal below, not routed here.
    schema_result = validate_trade_proposal(data)

    if not schema_result["valid"]:
        return {
            "stage": "schema_rejected",
            "approved": False,
            "decision": "REJECT",
            "schema_errors": schema_result["errors"],
            "schema_warnings": schema_result["warnings"],
            "risk_result": None,
            "normalized_proposal": schema_result["proposal"],
        }

    normalized = schema_result["proposal"]
    risk_trade = to_risk_engine_trade(normalized)
    risk_result = validate_trade(
        account_equity=account_equity,
        available_cash=available_cash,
        current_positions=current_positions,
        trades_this_week=trades_this_week,
        proposed_trade=risk_trade,
        current_company_exposure_pct=current_company_exposure_pct,
        current_sector_exposure_pct=current_sector_exposure_pct,
        blocked_sectors=blocked_sectors,
        account_mode=account_mode,
    )

    return {
        "stage": "approved" if risk_result["approved"] else "risk_rejected",
        "approved": risk_result["approved"],
        "decision": "APPROVE" if risk_result["approved"] else "REJECT",
        "schema_errors": [],
        "schema_warnings": schema_result["warnings"],
        "risk_result": risk_result,
        "normalized_proposal": normalized,
    }
