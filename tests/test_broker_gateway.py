"""Tests for Beanstock's deterministic Broker Safety Gateway
(broker/gateway.py).

Pure unit tests. No network access, no broker connection, no live
trading -- BrokerGateway is a pure validation layer sitting between
ExecutionIntent and FakePaperBroker, and this file proves the broker
adapter is never touched when the gateway rejects.
"""

import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest

from broker.fake_paper import FakePaperBroker
from broker.gateway import REJECT_DUPLICATE, BrokerGateway
from execution.intent import ExecutionIntent, _THE_APPROVAL_TOKEN, create_execution_intent
from models.trade_proposal import TradeProposal


def base_proposal(**overrides):
    proposal = {
        "ticker": "AAPL",
        "instrument_type": "stock",
        "action": "BUY",
        "current_price": 100.0,
        "intended_entry": 100.0,
        "candidate_score": 88,
        "catalyst": "Earnings beat with raised guidance",
        "catalyst_timing": "Reported this morning, guidance raise effective immediately",
        "bull_case": "Margin expansion continues into next two quarters",
        "bear_case": "Multiple compression if growth decelerates",
        "thesis_invalidation": "Guidance is walked back or margins compress q/q",
        "stop_price": 90.0,
        "target_price": 125.0,  # R:R 2.5
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


def make_intent(proposal, **kwargs):
    result = create_execution_intent(proposal, **{**account_state(), **kwargs})
    assert result.created is True, f"expected intent creation to succeed: {result.reasons}"
    return result.intent


def forged_intent(**overrides):
    """Directly construct an ExecutionIntent via the private approval
    token, bypassing create_execution_intent()'s own upstream checks --
    used only to test the gateway's OWN independent defenses, the same
    way execution/intent.py and fake_paper.py tests already do.
    """
    fields = dict(
        ticker="AAPL",
        action="BUY",
        instrument_type="stock",
        quantity=0.3,
        dollar_amount=30.0,
        intended_order_type="LIMIT",
        reference_price=100.0,
        stop_price=90.0,
        target_price=125.0,
        decision_status="APPROVE",
        audit_reference="audit-forged",
        created_at=datetime.now(timezone.utc).isoformat(),
        account_mode="PAPER",
    )
    fields.update(overrides)
    return ExecutionIntent._create_approved(_THE_APPROVAL_TOKEN, **fields)


class _ExplodingBroker(FakePaperBroker):
    """A broker whose submit_execution_intent() blows up if ever called
    -- used to prove the gateway never calls the broker on rejection.
    """

    def submit_execution_intent(self, intent):
        raise AssertionError("broker.submit_execution_intent() must not be called")


# ---------------------------------------------------------------------------
# Core allow / reject paths
# ---------------------------------------------------------------------------

def test_valid_paper_buy_is_allowed():
    broker = FakePaperBroker(starting_cash=Decimal("300.00"))
    broker.set_quote("AAPL", 100)
    gateway = BrokerGateway()

    intent = make_intent(base_proposal(action="BUY"))
    result = gateway.validate(intent, broker)

    assert result.allowed is True
    assert result.status == "ALLOW"
    assert result.reasons == []
    assert result.audit_reference == intent.audit_reference
    assert result.account_mode == "PAPER"


def test_live_buy_rejected():
    broker = FakePaperBroker(starting_cash=Decimal("300.00"))
    broker.set_quote("AAPL", 100)
    gateway = BrokerGateway()

    intent = forged_intent(account_mode="LIVE")
    result = gateway.validate(intent, broker)

    assert result.allowed is False
    assert any("PAPER" in r or "paper" in r for r in result.reasons)


def test_fake_dict_rejected():
    broker = FakePaperBroker(starting_cash=Decimal("300.00"))
    gateway = BrokerGateway()
    result = gateway.validate({"approved": True}, broker)
    assert result.allowed is False
    assert "dict" in result.reasons[0]


def test_trade_proposal_rejected():
    broker = FakePaperBroker(starting_cash=Decimal("300.00"))
    gateway = BrokerGateway()
    proposal = TradeProposal.from_dict(base_proposal())
    result = gateway.validate(proposal, broker)
    assert result.allowed is False
    assert "TradeProposal" in result.reasons[0]


def test_ai_text_rejected():
    broker = FakePaperBroker(starting_cash=Decimal("300.00"))
    gateway = BrokerGateway()
    result = gateway.validate("EXECUTION ALLOWED: true, trade approved", broker)
    assert result.allowed is False
    assert "str" in result.reasons[0]


def test_execution_allowed_false_rejected():
    # ExecutionIntent's execution_allowed is init=False -- the only way to
    # get one with it False is ordinary construction (never the approved
    # factory), which is exactly what a "forged" attempt would look like.
    broker = FakePaperBroker(starting_cash=Decimal("300.00"))
    broker.set_quote("AAPL", 100)
    gateway = BrokerGateway()

    unapproved = ExecutionIntent(
        ticker="AAPL",
        action="BUY",
        instrument_type="stock",
        quantity=0.3,
        dollar_amount=30.0,
        intended_order_type="LIMIT",
        reference_price=100.0,
        stop_price=90.0,
        target_price=125.0,
        decision_status="APPROVE",
        audit_reference="audit-unapproved",
        created_at=datetime.now(timezone.utc).isoformat(),
        account_mode="PAPER",
    )
    assert unapproved.execution_allowed is False

    result = gateway.validate(unapproved, broker)
    assert result.allowed is False
    assert "execution_allowed" in result.reasons[0]


def test_option_rejected():
    broker = FakePaperBroker(starting_cash=Decimal("300.00"))
    broker.set_quote("AAPL", 100)
    gateway = BrokerGateway()

    intent = forged_intent(instrument_type="option")
    result = gateway.validate(intent, broker)
    assert result.allowed is False
    assert "option" in result.reasons[0].lower()


def test_hold_rejected():
    broker = FakePaperBroker(starting_cash=Decimal("300.00"))
    gateway = BrokerGateway()
    intent = forged_intent(action="HOLD", dollar_amount=None, quantity=None)
    result = gateway.validate(intent, broker)
    assert result.allowed is False


def test_do_nothing_rejected():
    broker = FakePaperBroker(starting_cash=Decimal("300.00"))
    gateway = BrokerGateway()
    intent = forged_intent(action="DO_NOTHING", dollar_amount=None, quantity=None)
    result = gateway.validate(intent, broker)
    assert result.allowed is False


# ---------------------------------------------------------------------------
# Numeric validity
# ---------------------------------------------------------------------------

def test_zero_dollar_amount_rejected():
    broker = FakePaperBroker(starting_cash=Decimal("300.00"))
    broker.set_quote("AAPL", 100)
    gateway = BrokerGateway()
    intent = forged_intent(dollar_amount=0.0)
    result = gateway.validate(intent, broker)
    assert result.allowed is False
    assert "dollar_amount" in result.reasons[0]


def test_negative_dollar_amount_rejected():
    broker = FakePaperBroker(starting_cash=Decimal("300.00"))
    broker.set_quote("AAPL", 100)
    gateway = BrokerGateway()
    intent = forged_intent(dollar_amount=-30.0)
    result = gateway.validate(intent, broker)
    assert result.allowed is False
    assert "dollar_amount" in result.reasons[0]


def test_nan_dollar_amount_rejected():
    broker = FakePaperBroker(starting_cash=Decimal("300.00"))
    broker.set_quote("AAPL", 100)
    gateway = BrokerGateway()
    intent = forged_intent(dollar_amount=float("nan"))
    result = gateway.validate(intent, broker)
    assert result.allowed is False


def test_infinity_dollar_amount_rejected():
    broker = FakePaperBroker(starting_cash=Decimal("300.00"))
    broker.set_quote("AAPL", 100)
    gateway = BrokerGateway()
    intent = forged_intent(dollar_amount=float("inf"))
    result = gateway.validate(intent, broker)
    assert result.allowed is False


# ---------------------------------------------------------------------------
# Quotes
# ---------------------------------------------------------------------------

def test_missing_quote_rejected():
    broker = FakePaperBroker(starting_cash=Decimal("300.00"))
    # no set_quote call at all
    gateway = BrokerGateway()
    intent = make_intent(base_proposal(action="BUY"))
    result = gateway.validate(intent, broker)
    assert result.allowed is False
    assert "quote" in result.reasons[0].lower()


def test_stale_quote_rejected():
    broker = FakePaperBroker(starting_cash=Decimal("300.00"))
    broker.set_quote("AAPL", 100, timestamp=datetime.now(timezone.utc) - timedelta(seconds=120))
    gateway = BrokerGateway(max_quote_age_seconds=60.0)
    intent = forged_intent()
    result = gateway.validate(intent, broker)
    assert result.allowed is False
    assert "old" in result.reasons[0].lower()
    assert result.quote_age_seconds is not None and result.quote_age_seconds >= 120


# ---------------------------------------------------------------------------
# Intent staleness
# ---------------------------------------------------------------------------

def test_stale_intent_rejected():
    broker = FakePaperBroker(starting_cash=Decimal("300.00"))
    broker.set_quote("AAPL", 100)
    gateway = BrokerGateway(max_intent_age_seconds=300.0)
    old_created_at = (datetime.now(timezone.utc) - timedelta(seconds=600)).isoformat()
    intent = forged_intent(created_at=old_created_at)
    result = gateway.validate(intent, broker)
    assert result.allowed is False
    assert "old" in result.reasons[0].lower()
    assert result.intent_age_seconds is not None and result.intent_age_seconds >= 600


# ---------------------------------------------------------------------------
# Cash / duplicate
# ---------------------------------------------------------------------------

def test_insufficient_cash_rejected():
    broker = FakePaperBroker(starting_cash=Decimal("10.00"))
    broker.set_quote("AAPL", 100)
    gateway = BrokerGateway()
    intent = forged_intent(dollar_amount=30.0)
    result = gateway.validate(intent, broker)
    assert result.allowed is False
    assert "cash" in result.reasons[0].lower()


def test_duplicate_audit_reference_rejected():
    broker = FakePaperBroker(starting_cash=Decimal("300.00"))
    broker.set_quote("AAPL", 100)
    gateway = BrokerGateway()

    intent = make_intent(base_proposal(action="BUY"))
    first_result, first_order = gateway.submit(intent, broker)
    assert first_result.allowed is True
    assert first_order.status == "FILLED"

    second_result, second_order = gateway.submit(intent, broker)
    assert second_result.allowed is False
    assert REJECT_DUPLICATE in second_result.reasons[0]
    assert second_order is None
    assert broker.get_account().cash == Decimal("270.00")  # deducted only once


# ---------------------------------------------------------------------------
# SAFE_MODE kill switch
# ---------------------------------------------------------------------------

def test_safe_mode_blocks_buy():
    broker = FakePaperBroker(starting_cash=Decimal("300.00"))
    broker.set_quote("AAPL", 100)
    gateway = BrokerGateway(safe_mode=True)
    intent = make_intent(base_proposal(action="BUY"))
    result = gateway.validate(intent, broker)
    assert result.allowed is False
    assert "SAFE_MODE" in result.reasons[0]
    assert result.safety_state["safe_mode"] is True


def test_safe_mode_blocks_add():
    broker = FakePaperBroker(starting_cash=Decimal("300.00"))
    broker.set_quote("AAPL", 100)
    buy_intent = make_intent(base_proposal(action="BUY"))
    gateway = BrokerGateway()
    gateway.submit(buy_intent, broker)

    gateway.safe_mode = True
    add_intent = make_intent(
        base_proposal(action="ADD", proposed_dollar_amount=24.0),
        current_company_exposure_pct=10.0,
    )
    result = gateway.validate(add_intent, broker)
    assert result.allowed is False
    assert "SAFE_MODE" in result.reasons[0]


def test_safe_mode_allows_valid_reduce():
    broker = FakePaperBroker(starting_cash=Decimal("300.00"))
    broker.set_quote("AAPL", 100)
    buy_intent = make_intent(base_proposal(action="BUY"))
    gateway = BrokerGateway()
    gateway.submit(buy_intent, broker)

    gateway.safe_mode = True
    reduce_intent = make_intent(
        base_proposal(action="REDUCE", proposed_dollar_amount=12.0, proposed_allocation_pct=5.0),
        has_existing_position=True,
        current_company_exposure_pct=10.0,
    )
    result, order = gateway.submit(reduce_intent, broker)
    assert result.allowed is True
    assert order.status == "FILLED"


def test_safe_mode_allows_valid_exit():
    broker = FakePaperBroker(starting_cash=Decimal("300.00"))
    broker.set_quote("AAPL", 100)
    buy_intent = make_intent(base_proposal(action="BUY"))
    gateway = BrokerGateway()
    gateway.submit(buy_intent, broker)

    gateway.safe_mode = True
    exit_intent = make_intent(base_proposal(action="EXIT"), has_existing_position=True)
    result, order = gateway.submit(exit_intent, broker)
    assert result.allowed is True
    assert order.status == "FILLED"


# ---------------------------------------------------------------------------
# Daily-loss / weekly-drawdown gates
# ---------------------------------------------------------------------------

def test_daily_loss_blocks_buy():
    broker = FakePaperBroker(starting_cash=Decimal("300.00"))
    broker.set_quote("AAPL", 100)
    gateway = BrokerGateway(daily_loss_breached=True)
    intent = make_intent(base_proposal(action="BUY"))
    result = gateway.validate(intent, broker)
    assert result.allowed is False
    assert "daily loss" in result.reasons[0].lower()


def test_daily_loss_allows_exit():
    broker = FakePaperBroker(starting_cash=Decimal("300.00"))
    broker.set_quote("AAPL", 100)
    buy_intent = make_intent(base_proposal(action="BUY"))
    gateway = BrokerGateway()
    gateway.submit(buy_intent, broker)

    gateway.daily_loss_breached = True
    exit_intent = make_intent(base_proposal(action="EXIT"), has_existing_position=True)
    result, order = gateway.submit(exit_intent, broker)
    assert result.allowed is True
    assert order.status == "FILLED"


def test_weekly_drawdown_blocks_add():
    broker = FakePaperBroker(starting_cash=Decimal("300.00"))
    broker.set_quote("AAPL", 100)
    buy_intent = make_intent(base_proposal(action="BUY"))
    gateway = BrokerGateway()
    gateway.submit(buy_intent, broker)

    gateway.weekly_drawdown_breached = True
    add_intent = make_intent(
        base_proposal(action="ADD", proposed_dollar_amount=24.0),
        current_company_exposure_pct=10.0,
    )
    result = gateway.validate(add_intent, broker)
    assert result.allowed is False
    assert "weekly drawdown" in result.reasons[0].lower()


def test_weekly_drawdown_allows_reduce():
    broker = FakePaperBroker(starting_cash=Decimal("300.00"))
    broker.set_quote("AAPL", 100)
    buy_intent = make_intent(base_proposal(action="BUY"))
    gateway = BrokerGateway()
    gateway.submit(buy_intent, broker)

    gateway.weekly_drawdown_breached = True
    reduce_intent = make_intent(
        base_proposal(action="REDUCE", proposed_dollar_amount=12.0, proposed_allocation_pct=5.0),
        has_existing_position=True,
        current_company_exposure_pct=10.0,
    )
    result, order = gateway.submit(reduce_intent, broker)
    assert result.allowed is True
    assert order.status == "FILLED"


# ---------------------------------------------------------------------------
# REDUCE/EXIT position checks
# ---------------------------------------------------------------------------

def test_exit_with_no_position_rejected():
    broker = FakePaperBroker(starting_cash=Decimal("300.00"))
    broker.set_quote("AAPL", 100)
    gateway = BrokerGateway()
    exit_intent = make_intent(base_proposal(action="EXIT"), has_existing_position=True)
    result = gateway.validate(exit_intent, broker)
    assert result.allowed is False
    assert "existing position" in result.reasons[0]


def test_reduce_larger_than_position_rejected():
    broker = FakePaperBroker(starting_cash=Decimal("300.00"))
    broker.set_quote("AAPL", 100)
    buy_intent = make_intent(base_proposal(action="BUY"))
    gateway = BrokerGateway()
    gateway.submit(buy_intent, broker)  # 0.3 shares held

    reduce_intent = make_intent(
        base_proposal(action="REDUCE", proposed_dollar_amount=60.0, proposed_allocation_pct=1.0),
        has_existing_position=True,
        current_company_exposure_pct=10.0,
    )
    result = gateway.validate(reduce_intent, broker)
    assert result.allowed is False
    assert "short" in result.reasons[0].lower()


# ---------------------------------------------------------------------------
# Broker is never called on rejection
# ---------------------------------------------------------------------------

def test_broker_never_called_when_gateway_rejects():
    broker = _ExplodingBroker(starting_cash=Decimal("300.00"))
    broker.set_quote("AAPL", 100)
    gateway = BrokerGateway(safe_mode=True)  # guarantees rejection
    intent = make_intent(base_proposal(action="BUY"))

    result, order = gateway.submit(intent, broker)
    assert result.allowed is False
    assert order is None  # broker.submit_execution_intent() never ran (no AssertionError raised)


# ---------------------------------------------------------------------------
# Fail-closed on unexpected exceptions
# ---------------------------------------------------------------------------

class _BrokenBroker(FakePaperBroker):
    """A broker whose get_account() blows up mid-validation -- proves the
    gateway fails closed instead of propagating the exception or
    defaulting to allow.
    """

    def get_account(self):
        raise RuntimeError("simulated broker malfunction")


def test_unexpected_exception_fails_closed():
    broker = _BrokenBroker(starting_cash=Decimal("300.00"))
    broker.set_quote("AAPL", 100)
    gateway = BrokerGateway()
    intent = make_intent(base_proposal(action="BUY"))

    result = gateway.validate(intent, broker)
    assert result.allowed is False
    assert result.status == "REJECT"
    assert "unexpected error" in result.reasons[0].lower()


# ---------------------------------------------------------------------------
# Account-snapshot validation (requirement 14)
# ---------------------------------------------------------------------------

def test_cash_materially_changed_since_decision_rejected():
    broker = FakePaperBroker(starting_cash=Decimal("300.00"))
    broker.set_quote("AAPL", 100)
    gateway = BrokerGateway()

    # Simulate another trade having consumed cash between decision-time
    # and now: the AI's decision assumed $300 was available.
    broker.set_quote("MSFT", 50)
    other_intent = forged_intent(
        ticker="MSFT", audit_reference="audit-other", dollar_amount=100.0, reference_price=50.0
    )
    other_result, other_order = gateway.submit(other_intent, broker)
    assert other_result.allowed is True and other_order.status == "FILLED"

    intent = make_intent(base_proposal(action="BUY"))
    result = gateway.validate(intent, broker, expected_cash=Decimal("300.00"))
    assert result.allowed is False
    assert "cash changed" in result.reasons[0].lower()


# ---------------------------------------------------------------------------
# Integration
# ---------------------------------------------------------------------------

def test_full_integration_valid_flow_fills():
    broker = FakePaperBroker(starting_cash=Decimal("300.00"))
    broker.set_quote("ABC", 50)
    gateway = BrokerGateway()

    proposal = base_proposal(
        ticker="ABC",
        action="BUY",
        current_price=50.0,
        intended_entry=50.0,
        stop_price=45.0,
        target_price=62.5,
        proposed_dollar_amount=45.0,  # 15% of $300 -- at the small-account cap
        proposed_allocation_pct=15.0,
    )
    decision = create_execution_intent(proposal, **account_state())
    assert decision.created is True

    result, order = gateway.submit(decision.intent, broker)
    assert result.allowed is True
    assert order.status == "FILLED"
    assert broker.get_position("ABC").quantity == Decimal("0.9")


def test_full_integration_safe_mode_blocks_fill():
    broker = FakePaperBroker(starting_cash=Decimal("300.00"))
    broker.set_quote("ABC", 50)
    gateway = BrokerGateway(safe_mode=True)

    proposal = base_proposal(
        ticker="ABC",
        action="BUY",
        current_price=50.0,
        intended_entry=50.0,
        stop_price=45.0,
        target_price=62.5,
        proposed_dollar_amount=45.0,
        proposed_allocation_pct=15.0,
    )
    decision = create_execution_intent(proposal, **account_state())
    assert decision.created is True  # proposal is valid, intent exists

    result, order = gateway.submit(decision.intent, broker)
    assert result.allowed is False  # but the gateway rejects
    assert order is None
    assert broker.get_orders() == []  # FakePaperBroker received NO order
    assert broker.get_account().cash == Decimal("300.00")
