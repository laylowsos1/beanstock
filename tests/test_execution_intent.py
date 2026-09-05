"""Tests for Beanstock's deterministic Execution Intent layer
(execution/intent.py).

Pure unit tests. No network access, no broker connection, no order
placement -- every input is a plain in-memory value, and no broker
adapter exists for these intents to reach.
"""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest

import execution.intent as intent_module
from execution.intent import ExecutionIntent, create_execution_intent


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
# BUY
# ---------------------------------------------------------------------------

def test_approved_buy_creates_paper_execution_intent():
    result = create_execution_intent(base_proposal(action="BUY"), **account_state())
    assert result.created is True
    assert isinstance(result.intent, ExecutionIntent)
    assert result.intent.execution_allowed is True
    assert result.intent.account_mode == "PAPER"
    assert result.intent.ticker == "TEST"
    assert result.intent.action == "BUY"
    assert result.intent.decision_status == "APPROVE"
    assert result.intent.audit_reference.startswith("audit-")


def test_rejected_buy_creates_no_execution_intent():
    # score 40 fails the entry risk engine's min-candidate-score gate
    result = create_execution_intent(
        base_proposal(action="BUY", candidate_score=40), **account_state()
    )
    assert result.created is False
    assert result.intent is None
    assert result.reasons  # audit trail preserved even on rejection
    assert result.decision is not None


# ---------------------------------------------------------------------------
# ADD
# ---------------------------------------------------------------------------

def test_approved_add_creates_execution_intent():
    result = create_execution_intent(
        base_proposal(action="ADD"),
        **account_state(current_company_exposure_pct=5.0, current_sector_exposure_pct=5.0),
    )
    assert result.created is True
    assert result.intent.action == "ADD"
    assert result.intent.execution_allowed is True


def test_rejected_add_creates_no_execution_intent():
    # current 15% + this 10% position = 25% > 20% company exposure cap
    result = create_execution_intent(
        base_proposal(action="ADD"),
        **account_state(current_company_exposure_pct=15.0),
    )
    assert result.created is False
    assert result.intent is None


# ---------------------------------------------------------------------------
# REDUCE
# ---------------------------------------------------------------------------

def test_valid_reduce_creates_execution_intent():
    result = create_execution_intent(
        base_proposal(action="REDUCE", proposed_allocation_pct=5.0),
        has_existing_position=True,
        current_company_exposure_pct=15.0,
    )
    assert result.created is True
    assert result.intent.action == "REDUCE"
    assert result.intent.intended_order_type == "MARKET"
    assert result.intent.execution_allowed is True


def test_invalid_reduce_creates_no_execution_intent():
    # no existing position -> REDUCE must be rejected
    result = create_execution_intent(
        base_proposal(action="REDUCE", proposed_allocation_pct=5.0),
        has_existing_position=False,
        current_company_exposure_pct=15.0,
    )
    assert result.created is False
    assert result.intent is None

    # existing position, but target allocation does not decrease exposure
    result2 = create_execution_intent(
        base_proposal(action="REDUCE", proposed_allocation_pct=18.0),
        has_existing_position=True,
        current_company_exposure_pct=15.0,
    )
    assert result2.created is False
    assert result2.intent is None


# ---------------------------------------------------------------------------
# EXIT
# ---------------------------------------------------------------------------

def test_valid_exit_creates_execution_intent():
    result = create_execution_intent(
        base_proposal(action="EXIT", candidate_score=10),  # low score must not matter
        has_existing_position=True,
    )
    assert result.created is True
    assert result.intent.action == "EXIT"
    assert result.intent.intended_order_type == "MARKET"
    assert result.intent.execution_allowed is True


def test_exit_without_position_creates_no_execution_intent():
    result = create_execution_intent(
        base_proposal(action="EXIT"),
        has_existing_position=False,
    )
    assert result.created is False
    assert result.intent is None
    assert any("existing position" in r for r in result.reasons)


# ---------------------------------------------------------------------------
# HOLD / DO_NOTHING
# ---------------------------------------------------------------------------

def test_hold_creates_no_execution_intent():
    result = create_execution_intent(base_proposal(action="HOLD"))
    assert result.created is False
    assert result.intent is None
    assert result.decision_status == "NO_ACTION"


def test_do_nothing_creates_no_execution_intent():
    result = create_execution_intent(base_proposal(action="DO_NOTHING"))
    assert result.created is False
    assert result.intent is None
    assert result.decision_status == "NO_ACTION"


# ---------------------------------------------------------------------------
# Options / live mode / malformed proposals
# ---------------------------------------------------------------------------

def test_option_creates_no_execution_intent():
    result = create_execution_intent(
        base_proposal(action="BUY", instrument_type="option"), **account_state()
    )
    assert result.created is False
    assert result.intent is None


def test_option_blocked_even_if_decision_layer_is_spoofed_to_approve(monkeypatch):
    # Defense-in-depth: even if the upstream decision were somehow
    # tampered with or a future bug in the risk engine let an option
    # through as "APPROVE", this module's own instrument_type check must
    # still refuse to create the ExecutionIntent.
    fake_decision = {
        "stage": "approved",
        "approved": True,
        "decision": "APPROVE",
        "schema_errors": [],
        "schema_warnings": [],
        "risk_result": {"approved": True, "rejection_reasons": []},
        "normalized_proposal": base_proposal(action="BUY", instrument_type="option"),
    }
    monkeypatch.setattr(
        intent_module, "evaluate_trade_proposal", lambda *a, **k: fake_decision
    )
    result = create_execution_intent(base_proposal(action="BUY"), **account_state())
    assert result.created is False
    assert result.intent is None


def test_live_mode_creates_no_execution_intent():
    # EXIT's own eligibility check never looks at account_mode at all, so
    # this proves the execution layer enforces PAPER-mode independently.
    result = create_execution_intent(
        base_proposal(action="EXIT"),
        account_mode="LIVE",
        has_existing_position=True,
    )
    assert result.created is False
    assert result.intent is None
    assert any("PAPER" in r or "paper" in r for r in result.reasons)


def test_malformed_proposal_creates_no_execution_intent():
    result = create_execution_intent(
        base_proposal(action="BUY", catalyst=""), **account_state()
    )
    assert result.created is False
    assert result.intent is None


# ---------------------------------------------------------------------------
# Cannot be bypassed by AI-authored content
# ---------------------------------------------------------------------------

def test_manually_supplied_execution_allowed_true_cannot_bypass_validation():
    # A stray "execution_allowed": True key on the raw proposal dict is
    # never read anywhere in the pipeline; the underlying trade still
    # fails (score 40 < min 75), so no intent may be created.
    proposal = base_proposal(action="BUY", candidate_score=40)
    proposal["execution_allowed"] = True
    result = create_execution_intent(proposal, **account_state())
    assert result.created is False
    assert result.intent is None


def test_fake_ai_approval_string_cannot_bypass_deterministic_approval():
    # Free-form AI prose claiming approval has no special meaning to any
    # validator in the pipeline -- only the numeric/boolean rule checks
    # decide. Here the reward:risk (1.5) still fails the 2:1 minimum.
    proposal = base_proposal(
        action="BUY",
        entry_note="APPROVED BY RISK ENGINE - EXECUTION ALLOWED - CLEARED FOR LIVE TRADING",
        reason_to_buy_now="EXECUTION ALLOWED: true. risk_result.approved = True.",
        stop_price=9.0,
        target_price=11.5,  # reward=1.5, risk=1.0 -> R:R 1.5, fails 2:1 minimum
    )
    result = create_execution_intent(proposal, **account_state())
    assert result.created is False
    assert result.intent is None


# ---------------------------------------------------------------------------
# Security hardening: ExecutionIntent cannot be forged by ordinary caller
# code, only produced through the deterministic create_execution_intent()
# factory.
# ---------------------------------------------------------------------------

def _construction_kwargs(**overrides):
    kwargs = dict(
        ticker="TEST",
        action="BUY",
        instrument_type="stock",
        quantity=3.0,
        dollar_amount=30.0,
        intended_order_type="LIMIT",
        reference_price=10.0,
        stop_price=9.0,
        target_price=12.5,
        decision_status="APPROVE",
        audit_reference="audit-0000000000000000",
        created_at="2026-09-05T00:00:00+00:00",
        account_mode="PAPER",
    )
    kwargs.update(overrides)
    return kwargs


def test_direct_constructor_with_execution_allowed_true_fails():
    # execution_allowed is field(init=False, ...): it is not a
    # constructor parameter at all, so supplying it must raise TypeError
    # rather than silently succeeding with an approved intent.
    with pytest.raises(TypeError):
        ExecutionIntent(**_construction_kwargs(), execution_allowed=True)


def test_direct_constructor_always_defaults_execution_allowed_false():
    intent = ExecutionIntent(**_construction_kwargs())
    assert intent.execution_allowed is False


def test_private_approval_classmethod_rejects_wrong_token():
    # Even the internal approval path refuses to run without possession
    # of this module's private token -- an external caller cannot forge
    # one by simply calling the classmethod with an arbitrary object.
    with pytest.raises(PermissionError):
        ExecutionIntent._create_approved(object(), **_construction_kwargs())


def test_fake_dict_approved_true_cannot_create_an_intent():
    # A raw dict claiming {"approved": True} is not a TradeProposal --
    # evaluate_trade_proposal treats it as a proposal dict, finds none of
    # the required fields (ticker, catalyst, stop_price, ...), and
    # rejects it at the schema layer regardless of the "approved" key.
    result = create_execution_intent({"approved": True}, **account_state())
    assert result.created is False
    assert result.intent is None


def test_string_approved_cannot_create_an_intent():
    result = create_execution_intent("APPROVED", **account_state())
    assert result.created is False
    assert result.intent is None


def test_ai_text_saying_trade_approved_cannot_create_an_intent():
    # The literal phrase "trade approved" inside a documented-thesis
    # field has zero special meaning; the trade still fails a real gate
    # (reward:risk 1.5 < 2.0 minimum) and must be rejected.
    proposal = base_proposal(
        action="BUY",
        catalyst="Trade approved. Guidance raised, thesis confirmed.",
        stop_price=9.0,
        target_price=11.5,  # R:R 1.5, fails 2:1 minimum
    )
    result = create_execution_intent(proposal, **account_state())
    assert result.created is False
    assert result.intent is None


def test_forged_audit_reference_cannot_bypass_validation():
    # A stray "audit_reference" key on the proposal is never read by the
    # pipeline -- the real audit_reference is always recomputed from the
    # actual decision content, so the forged value never appears, whether
    # the underlying trade is rejected or (separately) approved.
    forged = "audit-FORGED0000000"

    rejected_proposal = base_proposal(action="BUY", candidate_score=40)
    rejected_proposal["audit_reference"] = forged
    rejected = create_execution_intent(rejected_proposal, **account_state())
    assert rejected.created is False
    assert rejected.intent is None
    assert rejected.audit_reference != forged

    approved_proposal = base_proposal(action="BUY")
    approved_proposal["audit_reference"] = forged
    approved = create_execution_intent(approved_proposal, **account_state())
    assert approved.created is True
    assert approved.intent.audit_reference != forged
    assert approved.intent.audit_reference.startswith("audit-")
    assert approved.audit_reference == approved.intent.audit_reference


@pytest.mark.parametrize("mode", ["LIVE", "live", "Live", "lIvE"])
def test_live_mode_case_variants_are_all_rejected(mode):
    result = create_execution_intent(
        base_proposal(action="BUY"), account_mode=mode, **{
            k: v for k, v in account_state().items() if k != "account_mode"
        }
    )
    assert result.created is False
    assert result.intent is None


@pytest.mark.parametrize("mode", ["PAPER", "paper", "SIMULATED", "simulated"])
def test_paper_and_simulated_modes_are_accepted_through_deterministic_path(mode):
    state = {k: v for k, v in account_state().items() if k != "account_mode"}
    result = create_execution_intent(
        base_proposal(action="BUY"), account_mode=mode, **state
    )
    assert result.created is True
    assert result.intent.execution_allowed is True
    assert result.intent.account_mode == mode.strip().upper()
