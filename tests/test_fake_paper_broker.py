"""Tests for Beanstock's local FakePaperBroker (broker/fake_paper.py).

This is NOT moomoo and NOT a real broker. Pure unit tests -- no network
access, no broker connection, no order placement anywhere but this
in-memory simulation. Prices are only ever whatever these tests load
into the fake quote store.
"""

import os
import sys
from decimal import Decimal

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest

from broker.fake_paper import FakePaperBroker, REJECT_DUPLICATE
from execution.intent import ExecutionIntent, create_execution_intent, _THE_APPROVAL_TOKEN
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
        "target_price": 125.0,  # reward=25, risk=10 -> R:R 2.5
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


# ---------------------------------------------------------------------------
# A. Account
# ---------------------------------------------------------------------------

def test_account_starts_with_exactly_300_no_positions_equity_equals_cash():
    broker = FakePaperBroker(starting_cash=Decimal("300.00"))
    account = broker.get_account()
    assert account.cash == Decimal("300.00")
    assert account.equity == Decimal("300.00")
    assert broker.get_positions() == []


# ---------------------------------------------------------------------------
# B. Valid BUY
# ---------------------------------------------------------------------------

def test_valid_30_dollar_buy_at_100_fills_correctly():
    broker = FakePaperBroker(starting_cash=Decimal("300.00"))
    broker.set_quote("AAPL", 100)

    intent = make_intent(base_proposal(action="BUY", proposed_dollar_amount=30.0))
    order = broker.submit_execution_intent(intent)

    assert order.status == "FILLED"
    assert order.filled_quantity == Decimal("0.3")
    assert order.fill_price == Decimal("100")

    account = broker.get_account()
    assert account.cash == Decimal("270.00")
    assert account.equity == Decimal("300.00")

    position = broker.get_position("AAPL")
    assert position.quantity == Decimal("0.3")
    assert position.market_value == Decimal("30.0")


# ---------------------------------------------------------------------------
# C. Oversized purchase
# ---------------------------------------------------------------------------

def test_oversized_purchase_rejected_cash_unchanged():
    # A real $400-on-$300 proposal is already blocked upstream by the
    # risk engine's own position-size/cash rules (9/10/11/14), so this
    # tests the BROKER's independent cash-sufficiency check directly via
    # a forged/pre-approved intent -- the same technique used in the
    # M/N/O/P defense-in-depth tests -- to prove the broker itself would
    # refuse even if something upstream let this through.
    broker = FakePaperBroker(starting_cash=Decimal("300.00"))
    broker.set_quote("AAPL", 100)

    forged = ExecutionIntent._create_approved(
        _THE_APPROVAL_TOKEN,
        ticker="AAPL",
        action="BUY",
        instrument_type="stock",
        quantity=None,
        dollar_amount=400.0,
        intended_order_type="LIMIT",
        reference_price=100.0,
        stop_price=90.0,
        target_price=125.0,
        decision_status="APPROVE",
        audit_reference="audit-oversized",
        created_at="2026-01-01T00:00:00+00:00",
        account_mode="PAPER",
    )
    order = broker.submit_execution_intent(forged)

    assert order.status == "REJECTED"
    assert "insufficient" in order.rejection_reason.lower()
    assert broker.get_account().cash == Decimal("300.00")
    assert broker.get_position("AAPL") is None


# ---------------------------------------------------------------------------
# D. Duplicate execution
# ---------------------------------------------------------------------------

def test_duplicate_execution_intent_only_fills_once():
    broker = FakePaperBroker(starting_cash=Decimal("300.00"))
    broker.set_quote("AAPL", 100)

    intent = make_intent(base_proposal(action="BUY", proposed_dollar_amount=30.0))

    first = broker.submit_execution_intent(intent)
    second = broker.submit_execution_intent(intent)

    assert first.status == "FILLED"
    assert second.status == "REJECTED"
    assert REJECT_DUPLICATE in second.rejection_reason
    assert broker.get_account().cash == Decimal("270.00")  # deducted only once


# ---------------------------------------------------------------------------
# E. ADD
# ---------------------------------------------------------------------------

def test_add_updates_quantity_weighted_average_and_cash():
    broker = FakePaperBroker(starting_cash=Decimal("300.00"))
    broker.set_quote("AAPL", 100)
    buy_intent = make_intent(base_proposal(action="BUY", proposed_dollar_amount=30.0))
    broker.submit_execution_intent(buy_intent)

    broker.set_quote("AAPL", 120)
    # Existing 0.3 shares are worth 0.3*120=$36 of $300 equity (12%); a
    # further $24 add is 8% more, landing exactly at the 20% company-
    # exposure cap (rule 10 passes on <=), so this clears the risk engine
    # on its own merits rather than needing a forged intent.
    add_intent = make_intent(
        base_proposal(action="ADD", proposed_dollar_amount=24.0),
        current_company_exposure_pct=12.0,
    )
    order = broker.submit_execution_intent(add_intent)

    assert order.status == "FILLED"
    assert order.filled_quantity == Decimal("0.2")

    position = broker.get_position("AAPL")
    assert position.quantity == Decimal("0.5")
    assert position.average_entry_price == Decimal("108.0")

    account = broker.get_account()
    assert account.cash == Decimal("246.00")

    orders = broker.get_orders()
    assert len(orders) == 2
    assert [o.action for o in orders] == ["BUY", "ADD"]


def test_add_without_existing_position_rejected():
    broker = FakePaperBroker(starting_cash=Decimal("300.00"))
    broker.set_quote("AAPL", 100)
    add_intent = make_intent(base_proposal(action="ADD", proposed_dollar_amount=30.0))
    order = broker.submit_execution_intent(add_intent)
    assert order.status == "REJECTED"
    assert "existing position" in order.rejection_reason


# ---------------------------------------------------------------------------
# F. REDUCE winner
# ---------------------------------------------------------------------------

def test_reduce_winner_realizes_profit_and_credits_cash():
    broker = FakePaperBroker(starting_cash=Decimal("300.00"))
    broker.set_quote("AAPL", 100)
    buy_intent = make_intent(base_proposal(action="BUY", proposed_dollar_amount=30.0))
    broker.submit_execution_intent(buy_intent)

    broker.set_quote("AAPL", 120)
    reduce_intent = make_intent(
        base_proposal(action="REDUCE", proposed_dollar_amount=12.0, proposed_allocation_pct=5.0),
        has_existing_position=True,
        current_company_exposure_pct=10.0,
    )
    order = broker.submit_execution_intent(reduce_intent)

    assert order.status == "FILLED"
    assert order.filled_quantity == Decimal("0.1")
    assert order.realized_pnl == Decimal("2.0")  # (120-100) * 0.1

    account = broker.get_account()
    assert account.cash == Decimal("282.00")  # 270 + 12 proceeds

    position = broker.get_position("AAPL")
    assert position.quantity == Decimal("0.2")
    assert position.average_entry_price == Decimal("100")  # cost basis unchanged


# ---------------------------------------------------------------------------
# G. REDUCE loser
# ---------------------------------------------------------------------------

def test_reduce_loser_realizes_loss_correctly():
    broker = FakePaperBroker(starting_cash=Decimal("300.00"))
    broker.set_quote("AAPL", 100)
    buy_intent = make_intent(base_proposal(action="BUY", proposed_dollar_amount=30.0))
    broker.submit_execution_intent(buy_intent)

    broker.set_quote("AAPL", 90)
    reduce_intent = make_intent(
        base_proposal(action="REDUCE", proposed_dollar_amount=9.0, proposed_allocation_pct=5.0),
        has_existing_position=True,
        current_company_exposure_pct=10.0,
    )
    order = broker.submit_execution_intent(reduce_intent)

    assert order.status == "FILLED"
    assert order.filled_quantity == Decimal("0.1")
    assert order.realized_pnl == Decimal("-1.0")  # (90-100) * 0.1

    account = broker.get_account()
    assert account.cash == Decimal("279.00")  # 270 + 9 proceeds

    position = broker.get_position("AAPL")
    assert position.quantity == Decimal("0.2")


# ---------------------------------------------------------------------------
# H. EXIT
# ---------------------------------------------------------------------------

def test_exit_closes_position_credits_cash_and_realizes_pnl():
    broker = FakePaperBroker(starting_cash=Decimal("300.00"))
    broker.set_quote("AAPL", 100)
    buy_intent = make_intent(base_proposal(action="BUY", proposed_dollar_amount=30.0))
    broker.submit_execution_intent(buy_intent)

    broker.set_quote("AAPL", 150)
    exit_intent = make_intent(
        base_proposal(action="EXIT"),
        has_existing_position=True,
    )
    order = broker.submit_execution_intent(exit_intent)

    assert order.status == "FILLED"
    assert order.filled_quantity == Decimal("0.3")
    assert order.realized_pnl == Decimal("15.0")  # (150-100) * 0.3

    account = broker.get_account()
    assert account.cash == Decimal("315.00")  # 270 + 45 proceeds
    assert account.equity == Decimal("315.00")
    assert broker.get_position("AAPL") is None


# ---------------------------------------------------------------------------
# I. EXIT nonexistent position
# ---------------------------------------------------------------------------

def test_exit_nonexistent_position_rejected_by_broker():
    # has_existing_position=True is only the caller's *claim* to the
    # execution-intent layer -- the broker independently checks its own
    # books and must still refuse, per "do not assume the upstream risk
    # engine is perfect."
    broker = FakePaperBroker(starting_cash=Decimal("300.00"))
    broker.set_quote("AAPL", 100)
    exit_intent = make_intent(
        base_proposal(action="EXIT"),
        has_existing_position=True,
    )
    order = broker.submit_execution_intent(exit_intent)
    assert order.status == "REJECTED"
    assert "existing position" in order.rejection_reason
    assert broker.get_account().cash == Decimal("300.00")


# ---------------------------------------------------------------------------
# J. Over-reduce
# ---------------------------------------------------------------------------

def test_over_reduce_rejected_never_creates_short():
    broker = FakePaperBroker(starting_cash=Decimal("300.00"))
    broker.set_quote("AAPL", 100)
    buy_intent = make_intent(base_proposal(action="BUY", proposed_dollar_amount=30.0))
    broker.submit_execution_intent(buy_intent)

    # 0.3 shares held at $100; requesting a $60 reduce at $100/share asks
    # to sell 0.6 shares -- double what is held.
    reduce_intent = make_intent(
        base_proposal(action="REDUCE", proposed_dollar_amount=60.0, proposed_allocation_pct=1.0),
        has_existing_position=True,
        current_company_exposure_pct=10.0,
    )
    order = broker.submit_execution_intent(reduce_intent)

    assert order.status == "REJECTED"
    assert "short" in order.rejection_reason.lower()

    position = broker.get_position("AAPL")
    assert position.quantity == Decimal("0.3")  # untouched
    assert position.quantity >= 0


# ---------------------------------------------------------------------------
# K. Missing quote
# ---------------------------------------------------------------------------

def test_missing_quote_rejected():
    broker = FakePaperBroker(starting_cash=Decimal("300.00"))
    # no set_quote call for AAPL at all
    intent = make_intent(base_proposal(action="BUY", proposed_dollar_amount=30.0))
    order = broker.submit_execution_intent(intent)
    assert order.status == "REJECTED"
    assert "quote" in order.rejection_reason.lower()
    assert broker.get_account().cash == Decimal("300.00")


# ---------------------------------------------------------------------------
# L. Zero / negative / NaN quote
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad_price", [0, -50, float("nan")])
def test_invalid_quote_values_rejected(bad_price):
    broker = FakePaperBroker(starting_cash=Decimal("300.00"))
    broker.set_quote("AAPL", bad_price)
    intent = make_intent(base_proposal(action="BUY", proposed_dollar_amount=30.0))
    order = broker.submit_execution_intent(intent)
    assert order.status == "REJECTED"
    assert broker.get_account().cash == Decimal("300.00")


# ---------------------------------------------------------------------------
# M. Option intent (defense-in-depth: create_execution_intent() already
#    refuses to ever produce an approved option intent, so this uses the
#    private approval token to simulate a compromised/buggy upstream and
#    prove the broker's OWN independent instrument_type check still
#    holds -- "do not assume the upstream risk engine is perfect.")
# ---------------------------------------------------------------------------

def test_option_intent_rejected_even_if_upstream_were_compromised():
    broker = FakePaperBroker(starting_cash=Decimal("300.00"))
    broker.set_quote("AAPL", 100)

    forged = ExecutionIntent._create_approved(
        _THE_APPROVAL_TOKEN,
        ticker="AAPL",
        action="BUY",
        instrument_type="option",
        quantity=1.0,
        dollar_amount=30.0,
        intended_order_type="LIMIT",
        reference_price=100.0,
        stop_price=90.0,
        target_price=125.0,
        decision_status="APPROVE",
        audit_reference="audit-forged-option",
        created_at="2026-01-01T00:00:00+00:00",
        account_mode="PAPER",
    )
    order = broker.submit_execution_intent(forged)
    assert order.status == "REJECTED"
    assert "option" in order.rejection_reason.lower()
    assert broker.get_account().cash == Decimal("300.00")


# ---------------------------------------------------------------------------
# N. Live-mode intent (same rationale as M: create_execution_intent()
#    already refuses LIVE mode, so this proves the broker's own
#    independent account_mode check as a second line of defense.)
# ---------------------------------------------------------------------------

def test_live_mode_intent_rejected_even_if_upstream_were_compromised():
    broker = FakePaperBroker(starting_cash=Decimal("300.00"))
    broker.set_quote("AAPL", 100)

    forged = ExecutionIntent._create_approved(
        _THE_APPROVAL_TOKEN,
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
        audit_reference="audit-forged-live",
        created_at="2026-01-01T00:00:00+00:00",
        account_mode="LIVE",
    )
    order = broker.submit_execution_intent(forged)
    assert order.status == "REJECTED"
    assert "PAPER" in order.rejection_reason or "paper" in order.rejection_reason
    assert broker.get_account().cash == Decimal("300.00")


# ---------------------------------------------------------------------------
# O / P. HOLD / DO_NOTHING submitted directly (create_execution_intent()
#    never produces these -- again using the private token to simulate a
#    forged intent slipping past the execution-intent layer.)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("action", ["HOLD", "DO_NOTHING"])
def test_hold_and_do_nothing_rejected_if_submitted_directly(action):
    broker = FakePaperBroker(starting_cash=Decimal("300.00"))
    broker.set_quote("AAPL", 100)

    forged = ExecutionIntent._create_approved(
        _THE_APPROVAL_TOKEN,
        ticker="AAPL",
        action=action,
        instrument_type="stock",
        quantity=None,
        dollar_amount=None,
        intended_order_type="LIMIT",
        reference_price=100.0,
        stop_price=None,
        target_price=None,
        decision_status="NO_ACTION",
        audit_reference=f"audit-forged-{action.lower()}",
        created_at="2026-01-01T00:00:00+00:00",
        account_mode="PAPER",
    )
    order = broker.submit_execution_intent(forged)
    assert order.status == "REJECTED"
    assert broker.get_account().cash == Decimal("300.00")

    log = broker.get_audit_log()
    assert any(entry["action"] == action for entry in log)  # attempt is logged


# ---------------------------------------------------------------------------
# Q. Forged object types
# ---------------------------------------------------------------------------

def test_string_cannot_be_submitted():
    broker = FakePaperBroker(starting_cash=Decimal("300.00"))
    with pytest.raises(TypeError):
        broker.submit_execution_intent("APPROVED")


def test_dict_cannot_be_submitted():
    broker = FakePaperBroker(starting_cash=Decimal("300.00"))
    with pytest.raises(TypeError):
        broker.submit_execution_intent({"approved": True})


def test_trade_proposal_cannot_be_submitted_directly():
    broker = FakePaperBroker(starting_cash=Decimal("300.00"))
    broker.set_quote("AAPL", 100)
    proposal = TradeProposal.from_dict(base_proposal())
    with pytest.raises(TypeError):
        broker.submit_execution_intent(proposal)


# ---------------------------------------------------------------------------
# R / S / T. Accounting invariants
# ---------------------------------------------------------------------------

def test_cash_never_negative_across_a_sequence_of_attempts():
    # A real $300-on-$300 BUY would already be blocked upstream by the
    # risk engine's position-size caps (rule 9), so this stress-tests the
    # BROKER's own cash invariant directly via forged/approved intents --
    # the same private-token technique used in the M/N/O/P defense-in-
    # depth tests above -- rather than the business-rule caps that a real
    # AI-authored proposal would already run into.
    broker = FakePaperBroker(starting_cash=Decimal("300.00"))
    broker.set_quote("AAPL", 100)

    def forged(action, dollar_amount, ref):
        return ExecutionIntent._create_approved(
            _THE_APPROVAL_TOKEN,
            ticker="AAPL",
            action=action,
            instrument_type="stock",
            quantity=None,
            dollar_amount=dollar_amount,
            intended_order_type="LIMIT",
            reference_price=100.0,
            stop_price=90.0,
            target_price=125.0,
            decision_status="APPROVE",
            audit_reference=ref,
            created_at="2026-01-01T00:00:00+00:00",
            account_mode="PAPER",
        )

    broker.submit_execution_intent(forged("BUY", 200.0, "audit-cash-1"))
    for i in range(5):
        broker.submit_execution_intent(forged("ADD", 500.0, f"audit-cash-add-{i}"))

    assert broker.get_account().cash >= 0


def test_position_quantity_never_negative_after_over_reduce_attempts():
    broker = FakePaperBroker(starting_cash=Decimal("300.00"))
    broker.set_quote("AAPL", 100)
    buy_intent = make_intent(base_proposal(action="BUY", proposed_dollar_amount=30.0))
    broker.submit_execution_intent(buy_intent)

    for _ in range(3):
        reduce_intent = make_intent(
            base_proposal(
                action="REDUCE", proposed_dollar_amount=1000.0, proposed_allocation_pct=1.0
            ),
            has_existing_position=True,
            current_company_exposure_pct=10.0,
        )
        broker.submit_execution_intent(reduce_intent)

    position = broker.get_position("AAPL")
    assert position.quantity == Decimal("0.3")
    assert position.quantity >= 0


def test_equity_accounting_remains_consistent_through_buy_reduce_exit():
    broker = FakePaperBroker(starting_cash=Decimal("300.00"))
    broker.set_quote("AAPL", 100)
    buy_intent = make_intent(base_proposal(action="BUY", proposed_dollar_amount=30.0))
    broker.submit_execution_intent(buy_intent)
    assert broker.get_account().equity == Decimal("300.00")

    broker.set_quote("AAPL", 120)
    assert broker.get_account().equity == Decimal("306.00")  # 270 cash + 0.3*120 mv

    reduce_intent = make_intent(
        base_proposal(action="REDUCE", proposed_dollar_amount=12.0, proposed_allocation_pct=5.0),
        has_existing_position=True,
        current_company_exposure_pct=10.0,
    )
    broker.submit_execution_intent(reduce_intent)
    assert broker.get_account().equity == Decimal("306.00")  # unchanged by a fair-price trim

    exit_intent = make_intent(base_proposal(action="EXIT"), has_existing_position=True)
    broker.submit_execution_intent(exit_intent)
    account = broker.get_account()
    assert account.equity == account.cash  # no positions left
    assert account.equity == Decimal("306.00")


# ---------------------------------------------------------------------------
# 16. Complete integration test
# ---------------------------------------------------------------------------

def test_full_integration_buy_then_exit_workflow():
    broker = FakePaperBroker(starting_cash=Decimal("300.00"))
    broker.set_quote("ABC", 50)
    broker.set_quote("XYZ", 25)

    proposal = base_proposal(
        ticker="ABC",
        action="BUY",
        current_price=50.0,
        intended_entry=50.0,
        stop_price=45.0,
        target_price=62.5,  # R:R 2.5
        proposed_dollar_amount=45.0,  # 15% of $300 -- exactly at the small-account cap
        proposed_allocation_pct=15.0,
    )

    decision = create_execution_intent(proposal, **account_state())
    assert decision.created is True, decision.reasons
    assert decision.decision_status == "APPROVE"
    assert decision.decision["risk_result"]["approved"] is True

    buy_order = broker.submit_execution_intent(decision.intent)
    assert buy_order.status == "FILLED"
    assert buy_order.filled_quantity == Decimal("0.9")  # $45 / $50

    position = broker.get_position("ABC")
    assert position.quantity == Decimal("0.9")
    assert position.average_entry_price == Decimal("50")

    # price moves up before the reduce/exit workflow
    broker.set_quote("ABC", 65)

    exit_proposal = base_proposal(
        ticker="ABC",
        action="EXIT",
        current_price=65.0,
        intended_entry=65.0,
    )
    exit_decision = create_execution_intent(
        exit_proposal, has_existing_position=True, **account_state()
    )
    assert exit_decision.created is True
    assert exit_decision.decision_status == "EXIT_ALLOWED"

    exit_order = broker.submit_execution_intent(exit_decision.intent)
    assert exit_order.status == "FILLED"
    assert exit_order.realized_pnl == Decimal("13.5")  # (65-50) * 0.9

    account = broker.get_account()
    assert account.cash == Decimal("313.50")  # 300 - 45 + 58.5
    assert account.equity == account.cash
    assert broker.get_position("ABC") is None

    # no network/broker calls anywhere in this workflow -- FakePaperBroker
    # only ever reads from its own in-memory quote store.
    audit_log = broker.get_audit_log()
    assert len(audit_log) == 2
    assert [entry["status"] for entry in audit_log] == ["FILLED", "FILLED"]
