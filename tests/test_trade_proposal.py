"""Tests for Beanstock's structured Trade Proposal layer (models/trade_proposal.py).

Pure unit tests. No network access, no broker connection, no order
placement -- every input is a plain in-memory value.
"""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from models.trade_proposal import (
    TradeProposal,
    validate_trade_proposal,
    to_risk_engine_trade,
    evaluate_trade_proposal,
)


def base_proposal(**overrides):
    proposal = {
        "ticker": "TEST",
        "instrument_type": "stock",
        "action": "BUY",
        "current_price": 10.0,
        "intended_entry": 10.0,
        "candidate_score": 88,
        "catalyst": "Earnings beat with raised guidance",
        "catalyst_timing": "Reported this morning, guidance raise effective immediately",
        "bull_case": "Margin expansion continues into next two quarters",
        "bear_case": "Multiple compression if growth decelerates",
        "thesis_invalidation": "Guidance is walked back or margins compress q/q",
        "stop_price": 9.0,
        "target_price": 12.5,  # reward=2.5, risk=1.0 -> R:R 2.5
        "proposed_dollar_amount": 30.0,
        "proposed_allocation_pct": 10.0,
        "sector": "Technology",
        "confidence": 0.7,
        "holding_period": "2-6 weeks",
        "reason_to_buy_now": "Catalyst is fresh and thesis is falsifiable today",
        "reason_to_wait": "None - waiting risks missing the re-rating",
        "data_timestamp": "2026-09-05T09:35:00Z",
        "reward_risk": 2.5,
    }
    proposal.update(overrides)
    return proposal


def account_state(**overrides):
    state = dict(
        account_equity=300.0,
        available_cash=300.0,
        current_positions=0,
        trades_this_week=0,
        current_company_exposure_pct=0.0,
        current_sector_exposure_pct=0.0,
        blocked_sectors=[],
        account_mode="PAPER",
    )
    state.update(overrides)
    return state


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------

def test_complete_valid_proposal_passes_schema_validation():
    result = validate_trade_proposal(base_proposal())
    assert result["valid"] is True
    assert result["errors"] == []
    assert result["calculated_reward_risk"] == 2.5


def test_missing_catalyst_rejects():
    result = validate_trade_proposal(base_proposal(catalyst=""))
    assert result["valid"] is False
    assert any("catalyst" in e for e in result["errors"])

    result_none = validate_trade_proposal(base_proposal(catalyst=None))
    assert result_none["valid"] is False


def test_missing_stop_rejects():
    result = validate_trade_proposal(base_proposal(stop_price=None))
    assert result["valid"] is False
    assert any("stop_price" in e for e in result["errors"])
    assert result["calculated_reward_risk"] is None


def test_missing_target_rejects():
    result = validate_trade_proposal(base_proposal(target_price=None))
    assert result["valid"] is False
    assert any("target_price" in e for e in result["errors"])
    assert result["calculated_reward_risk"] is None


def test_missing_sector_rejects():
    result = validate_trade_proposal(base_proposal(sector=""))
    assert result["valid"] is False
    assert any("sector" in e for e in result["errors"])


def test_score_101_rejects():
    result = validate_trade_proposal(base_proposal(candidate_score=101))
    assert result["valid"] is False
    assert any("candidate_score" in e for e in result["errors"])


def test_zero_entry_price_rejects():
    result = validate_trade_proposal(base_proposal(intended_entry=0))
    assert result["valid"] is False
    assert any("intended_entry" in e for e in result["errors"])


def test_negative_price_rejects():
    result = validate_trade_proposal(base_proposal(current_price=-5.0))
    assert result["valid"] is False
    assert any("current_price" in e for e in result["errors"])


def test_malformed_action_rejects():
    result = validate_trade_proposal(base_proposal(action="YOLO"))
    assert result["valid"] is False
    assert any("action" in e for e in result["errors"])


def test_missing_ticker_rejects():
    result = validate_trade_proposal(base_proposal(ticker=""))
    assert result["valid"] is False
    assert any("ticker" in e for e in result["errors"])


def test_missing_thesis_invalidation_rejects():
    result = validate_trade_proposal(base_proposal(thesis_invalidation=""))
    assert result["valid"] is False
    assert any("thesis_invalidation" in e for e in result["errors"])


def test_invalid_instrument_type_rejects():
    result = validate_trade_proposal(base_proposal(instrument_type="crypto"))
    assert result["valid"] is False
    assert any("instrument_type" in e for e in result["errors"])


def test_confidence_out_of_range_rejects():
    result = validate_trade_proposal(base_proposal(confidence=1.5))
    assert result["valid"] is False
    assert any("confidence" in e for e in result["errors"])


def test_incoherent_levels_rejected():
    # stop above entry is not a valid long risk structure
    result = validate_trade_proposal(
        base_proposal(intended_entry=10.0, stop_price=11.0, target_price=12.0)
    )
    assert result["valid"] is False
    assert result["calculated_reward_risk"] is None


# ---------------------------------------------------------------------------
# Deterministic reward:risk overriding the AI-supplied value
# ---------------------------------------------------------------------------

def test_incorrect_ai_supplied_reward_risk_is_overridden():
    result = validate_trade_proposal(base_proposal(reward_risk=99.0))
    assert result["valid"] is True
    assert result["calculated_reward_risk"] == 2.5
    assert result["proposal"]["reward_risk"] == 2.5
    assert any("disagreed" in w for w in result["warnings"])


def test_correct_ai_supplied_reward_risk_produces_no_warning():
    result = validate_trade_proposal(base_proposal(reward_risk=2.5))
    assert result["valid"] is True
    assert result["warnings"] == []


def test_missing_ai_supplied_reward_risk_is_still_calculated():
    result = validate_trade_proposal(base_proposal(reward_risk=None))
    assert result["valid"] is True
    assert result["calculated_reward_risk"] == 2.5
    assert result["proposal"]["reward_risk"] == 2.5


# ---------------------------------------------------------------------------
# JSON serialization / timestamp preservation
# ---------------------------------------------------------------------------

def test_json_round_trip_preserves_fields_and_timestamp():
    proposal = TradeProposal(**base_proposal())
    as_json = proposal.to_json()
    restored = TradeProposal.from_json(as_json)
    assert restored == proposal
    assert restored.data_timestamp == "2026-09-05T09:35:00Z"


def test_from_dict_ignores_unknown_keys():
    data = base_proposal(extra_ai_commentary="ignore me, I am free-form prose")
    proposal = TradeProposal.from_dict(data)
    assert proposal.ticker == "TEST"
    assert not hasattr(proposal, "extra_ai_commentary")


# ---------------------------------------------------------------------------
# Integration: TradeProposal -> risk engine -> APPROVE / REJECT
# ---------------------------------------------------------------------------

def test_valid_proposal_flows_through_to_risk_engine_approval():
    result = evaluate_trade_proposal(base_proposal(), **account_state())
    assert result["stage"] == "approved"
    assert result["approved"] is True
    assert result["risk_result"]["approved"] is True


def test_schema_invalid_proposal_never_reaches_risk_engine():
    result = evaluate_trade_proposal(base_proposal(catalyst=""), **account_state())
    assert result["stage"] == "schema_rejected"
    assert result["approved"] is False
    assert result["risk_result"] is None
    assert any("catalyst" in e for e in result["schema_errors"])


def test_options_proposal_rejected_before_execution_under_current_rules():
    # An option proposal is a structurally well-formed TradeProposal (schema
    # passes), but Beanstock's current hard rules reject all options at the
    # deterministic risk-engine stage, before any execution path.
    proposal = base_proposal(instrument_type="option")
    schema_result = validate_trade_proposal(proposal)
    assert schema_result["valid"] is True

    result = evaluate_trade_proposal(proposal, **account_state())
    assert result["stage"] == "risk_rejected"
    assert result["approved"] is False
    assert result["risk_result"]["approved"] is False
    reasons = " ".join(result["risk_result"]["rejection_reasons"])
    assert "option" in reasons.lower() or "instrument_type" in reasons.lower()


def test_to_risk_engine_trade_maps_fields_and_forces_long_no_margin():
    normalized = validate_trade_proposal(base_proposal())["proposal"]
    risk_trade = to_risk_engine_trade(normalized)
    assert risk_trade["symbol"] == "TEST"
    assert risk_trade["side"] == "long"
    assert risk_trade["uses_margin"] is False
    assert risk_trade["cost"] == 30.0
    assert risk_trade["entry"] == 10.0
    assert risk_trade["stop"] == 9.0
    assert risk_trade["target"] == 12.5


# ---------------------------------------------------------------------------
# Action routing: BUY / ADD -> entry risk engine, REDUCE / EXIT -> risk-
# reducing checks only, HOLD / DO_NOTHING -> no validation at all.
# ---------------------------------------------------------------------------

def test_buy_valid_setup_reaches_entry_risk_engine():
    result = evaluate_trade_proposal(base_proposal(action="BUY"), **account_state())
    assert result["decision"] == "APPROVE"
    assert result["approved"] is True
    assert result["risk_result"] is not None
    assert result["risk_result"]["approved"] is True


def test_buy_invalid_setup_rejected():
    # score 40 fails the entry risk engine's min-candidate-score gate
    result = evaluate_trade_proposal(
        base_proposal(action="BUY", candidate_score=40), **account_state()
    )
    assert result["decision"] == "REJECT"
    assert result["approved"] is False
    assert result["risk_result"] is not None
    assert result["risk_result"]["approved"] is False


def test_add_causing_company_exposure_over_20_percent_rejected():
    result = evaluate_trade_proposal(
        base_proposal(action="ADD", proposed_dollar_amount=30.0),
        **account_state(current_company_exposure_pct=15.0),
    )
    assert result["decision"] == "REJECT"
    reasons = " ".join(result["risk_result"]["rejection_reasons"])
    assert "company_exposure" in reasons or "max_company_exposure" in reasons


def test_add_causing_sector_exposure_over_30_percent_rejected():
    result = evaluate_trade_proposal(
        base_proposal(action="ADD", proposed_dollar_amount=30.0),
        **account_state(current_sector_exposure_pct=25.0),
    )
    assert result["decision"] == "REJECT"
    reasons = " ".join(result["risk_result"]["rejection_reasons"])
    assert "sector_exposure" in reasons or "max_sector_exposure" in reasons


def test_add_averaging_down_solely_on_decline_rejected():
    result = evaluate_trade_proposal(
        base_proposal(action="ADD", averaging_down_on_decline=True),
        **account_state(),
    )
    assert result["decision"] == "REJECT"
    reasons = " ".join(result["risk_result"]["rejection_reasons"])
    assert "averaging_down" in reasons


def test_reduce_existing_position_allowed():
    result = evaluate_trade_proposal(
        base_proposal(action="REDUCE", proposed_allocation_pct=5.0),
        has_existing_position=True,
        current_company_exposure_pct=15.0,
    )
    assert result["decision"] == "REDUCE_ALLOWED"
    assert result["approved"] is True
    assert result["risk_result"] is None


def test_reduce_nonexistent_position_rejected():
    result = evaluate_trade_proposal(
        base_proposal(action="REDUCE", proposed_allocation_pct=5.0),
        has_existing_position=False,
        current_company_exposure_pct=15.0,
    )
    assert result["decision"] == "REJECT"
    assert any("existing position" in r for r in result["reasons"])


def test_reduce_that_would_increase_exposure_rejected():
    result = evaluate_trade_proposal(
        base_proposal(action="REDUCE", proposed_allocation_pct=18.0),
        has_existing_position=True,
        current_company_exposure_pct=15.0,
    )
    assert result["decision"] == "REJECT"
    assert any("decrease exposure" in r for r in result["reasons"])


def test_exit_existing_position_allowed_even_if_score_below_75():
    result = evaluate_trade_proposal(
        base_proposal(action="EXIT", candidate_score=10),
        has_existing_position=True,
    )
    assert result["decision"] == "EXIT_ALLOWED"
    assert result["approved"] is True
    assert result["risk_result"] is None


def test_exit_existing_position_allowed_even_if_sector_blocked():
    # blocked_sectors is a BUY/ADD-entry-gate concept (risk rule 18); an
    # EXIT never touches it at all.
    result = evaluate_trade_proposal(
        base_proposal(action="EXIT", sector="Energy"),
        has_existing_position=True,
    )
    assert result["decision"] == "EXIT_ALLOWED"
    assert result["approved"] is True


def test_exit_nonexistent_position_rejected():
    result = evaluate_trade_proposal(
        base_proposal(action="EXIT"),
        has_existing_position=False,
    )
    assert result["decision"] == "REJECT"
    assert any("existing position" in r for r in result["reasons"])


def test_hold_returns_no_action_and_never_calls_risk_engine(monkeypatch):
    def _boom(*args, **kwargs):
        raise AssertionError("entry risk engine must not be called for HOLD")

    monkeypatch.setattr("models.trade_proposal.validate_trade", _boom)
    result = evaluate_trade_proposal(base_proposal(action="HOLD"))
    assert result["decision"] == "NO_ACTION"
    assert result["risk_result"] is None


def test_do_nothing_returns_no_action_and_never_calls_risk_engine(monkeypatch):
    def _boom(*args, **kwargs):
        raise AssertionError("entry risk engine must not be called for DO_NOTHING")

    monkeypatch.setattr("models.trade_proposal.validate_trade", _boom)
    result = evaluate_trade_proposal(base_proposal(action="DO_NOTHING"))
    assert result["decision"] == "NO_ACTION"
    assert result["risk_result"] is None
