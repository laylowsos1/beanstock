"""Tests for MoomooPaperBroker (broker/moomoo_paper.py) -- MOCK-ONLY.

No test in this file makes a real network call (FakeHttpTransport only),
and BEANSTOCK_PAPER_WRITE_ENABLED is never set in the real environment --
every test that needs the "write enabled" code path passes
write_enabled=True explicitly to the constructor, which is independent
of (and never touches) the real environment variable.
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest

from broker.http_transport import HttpResponse, HttpTransport, TransportError, TransportTimeout
from broker.gateway import BrokerGateway
from broker.moomoo_readonly import (
    ACCOUNTS_PATH,
    CASH_INFO_PATH_TEMPLATE,
    HISTORY_ORDERS_PATH_TEMPLATE,
    OPEN_ORDERS_PATH_TEMPLATE,
    POSITIONS_PATH_TEMPLATE,
    QUOTE_PATH,
    US_MARKET_ID,
)
from broker.moomoo_paper import (
    ENV_WRITE_ENABLED,
    MAX_BUY_SELL_PATH_TEMPLATE,
    ORDER_SIDE_BUY,
    ORDER_SIDE_SELL,
    ORDER_TYPE_MARKET,
    REJECT_DUPLICATE,
    REJECT_WRITE_DISABLED,
    LiveAccountRejectedError,
    MalformedResponseError,
    MoomooAuthenticationError,
    MoomooPaperBroker,
    MoomooRateLimitError,
    MoomooServerError,
    MoomooTimeoutError,
    PaperWriteDisabledError,
)
from execution.intent import ExecutionIntent, create_execution_intent, _THE_APPROVAL_TOKEN
from models.trade_proposal import TradeProposal

DEFAULT_ACCOUNT_ID = "acc-1"


# ---------------------------------------------------------------------
# Fake transport -- no test using this ever opens a socket.
# ---------------------------------------------------------------------


class FakeHttpTransport(HttpTransport):
    def __init__(self):
        self._get_queue: dict = {}
        self._post_queue: dict = {}
        self.calls: list = []

    def queue_get(self, path, response) -> None:
        self._get_queue.setdefault(path, []).append(response)

    def queue_post(self, path, response) -> None:
        self._post_queue.setdefault(path, []).append(response)

    def get(self, path, *, params=None, headers=None, timeout=10.0):
        self.calls.append(("GET", path, params, headers))
        return self._resolve(self._get_queue, path)

    def post(self, path, *, form=None, json_body=None, headers=None, timeout=10.0):
        self.calls.append(("POST", path, form if form is not None else json_body, headers))
        return self._resolve(self._post_queue, path)

    def _resolve(self, table, path):
        queue = table.get(path)
        if not queue:
            raise AssertionError(f"FakeHttpTransport: no response queued for {path!r}")
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        return item


def envelope(data: dict, ret_code: int = 0, ret_msg: str = "success") -> dict:
    return {"ret_code": ret_code, "ret_msg": ret_msg, "data": data}


def json_response(status_code: int, payload) -> HttpResponse:
    return HttpResponse(status_code=status_code, body=json.dumps(payload))


def accounts_envelope(us_account_ids=None):
    return envelope({"accounts": [{"account_id": acc_id, "market_id": US_MARKET_ID} for acc_id in (us_account_ids or [])]})


def _order_record(order_id, symbol="ABC", side=1, status=4, qty="3", cum_qty="3", price="50.00", create_time="2026-09-06T09:35:00Z"):
    return {
        "order_id": order_id,
        "symbol": symbol,
        "side": side,
        "status": status,
        "qty": qty,
        "cum_qty": cum_qty,
        "price": price,
        "create_time": create_time,
    }


def queue_account_resolution(transport, account_id=DEFAULT_ACCOUNT_ID):
    transport.queue_get(ACCOUNTS_PATH, json_response(200, accounts_envelope(us_account_ids=[account_id])))


def queue_cash(transport, account_id=DEFAULT_ACCOUNT_ID, cash="1000.00"):
    transport.queue_get(
        CASH_INFO_PATH_TEMPLATE.format(acc_id=account_id),
        json_response(200, envelope({"balance": cash, "total_asset": cash})),
    )


def queue_positions(transport, account_id=DEFAULT_ACCOUNT_ID, positions=None):
    transport.queue_get(
        POSITIONS_PATH_TEMPLATE.format(acc_id=account_id),
        json_response(200, envelope({"positions": positions or []})),
    )


def queue_quote(transport, ticker="ABC", price="50.00", data_time_ms=None, market_prefix="US"):
    if data_time_ms is None:
        data_time_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    transport.queue_post(
        QUOTE_PATH,
        json_response(200, envelope({"quote_list": [{"code": f"{market_prefix}.{ticker}", "last_price": price, "data_time": data_time_ms}]})),
    )


def queue_full_read_setup(transport, *, account_id=DEFAULT_ACCOUNT_ID, cash="1000.00", positions=None, ticker="ABC", price="50.00", quote_time_ms=None):
    queue_account_resolution(transport, account_id)
    queue_cash(transport, account_id, cash)
    queue_positions(transport, account_id, positions)
    queue_quote(transport, ticker, price, quote_time_ms)


def queue_place_order_accepted(transport, account_id=DEFAULT_ACCOUNT_ID, order_id="moomoo-order-1"):
    transport.queue_post(OPEN_ORDERS_PATH_TEMPLATE.format(acc_id=account_id), json_response(200, {"ret_code": 0, "data": {"order_id": order_id}}))


def queue_order_lookup(transport, account_id=DEFAULT_ACCOUNT_ID, open_orders=None, history_orders=None):
    transport.queue_get(OPEN_ORDERS_PATH_TEMPLATE.format(acc_id=account_id), json_response(200, envelope({"orders": open_orders or []})))
    transport.queue_get(
        HISTORY_ORDERS_PATH_TEMPLATE.format(acc_id=account_id),
        json_response(200, envelope({"orders": history_orders or [], "pagination": {"has_more": False, "next_key": ""}})),
    )


def make_paper_broker(transport, *, account_id_override=DEFAULT_ACCOUNT_ID, write_enabled=False, token=None):
    return MoomooPaperBroker(
        access_token_provider=token or (lambda: "fake-access-token"),
        http_transport=transport,
        simulated_account_id=account_id_override,
        write_enabled=write_enabled,
    )


def forged_intent(**overrides):
    """Directly construct an ExecutionIntent via the private approval
    token, bypassing create_execution_intent()'s own upstream checks --
    used only to test MoomooPaperBroker's OWN independent defenses, the
    same way execution/intent.py, fake_paper.py, and gateway.py tests
    already do.
    """
    fields = dict(
        ticker="ABC",
        action="BUY",
        instrument_type="stock",
        quantity=3.0,
        dollar_amount=150.0,
        intended_order_type="MARKET",
        reference_price=50.0,
        stop_price=45.0,
        target_price=65.0,
        decision_status="APPROVE",
        audit_reference="audit-forged",
        created_at=datetime.now(timezone.utc).isoformat(),
        account_mode="PAPER",
    )
    fields.update(overrides)
    return ExecutionIntent._create_approved(_THE_APPROVAL_TOKEN, **fields)


def base_proposal(**overrides):
    proposal = {
        "ticker": "ABC",
        "instrument_type": "stock",
        "action": "BUY",
        "current_price": 50.0,
        "intended_entry": 50.0,
        "candidate_score": 88,
        "catalyst": "Earnings beat with raised guidance",
        "catalyst_timing": "Reported this morning, guidance raise effective immediately",
        "bull_case": "Margin expansion continues into next two quarters",
        "bear_case": "Multiple compression if growth decelerates",
        "thesis_invalidation": "Guidance is walked back or margins compress q/q",
        "stop_price": 45.0,
        "target_price": 62.5,
        "proposed_dollar_amount": 45.0,
        "proposed_allocation_pct": 15.0,
        "sector": "Technology",
        "confidence": 0.7,
        "holding_period": "2-6 weeks",
        "reason_to_buy_now": "Catalyst is fresh and thesis is falsifiable today",
        "reason_to_wait": "None - waiting risks missing the re-rating",
        "data_timestamp": "2026-09-06T09:35:00Z",
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


# ---------------------------------------------------------------------
# Live-endpoint / write-permission guard
# ---------------------------------------------------------------------


def test_live_rest_path_rejected():
    transport = FakeHttpTransport()
    broker = make_paper_broker(transport)
    for live_path in ("/api/v1.0/accounts/authorized_trd_accs", "/api/v1.0/trading/trade/place-order"):
        with pytest.raises(LiveAccountRejectedError):
            broker._guard_path(live_path)
    assert transport.calls == []


def test_write_disabled_by_default():
    transport = FakeHttpTransport()
    broker = MoomooPaperBroker(access_token_provider=lambda: "fake-access-token", http_transport=transport, simulated_account_id=DEFAULT_ACCOUNT_ID)
    assert broker.write_enabled is False


def test_write_disabled_env_var_default_false(monkeypatch):
    monkeypatch.delenv(ENV_WRITE_ENABLED, raising=False)
    transport = FakeHttpTransport()
    broker = MoomooPaperBroker(access_token_provider=lambda: "x", http_transport=transport, simulated_account_id=DEFAULT_ACCOUNT_ID)
    assert broker.write_enabled is False


def test_write_disabled_prevents_all_write_http_calls():
    transport = FakeHttpTransport()
    queue_full_read_setup(transport, positions=[])
    broker = make_paper_broker(transport, write_enabled=False)

    order = broker.submit_execution_intent(forged_intent(action="BUY", dollar_amount=150.0))

    assert order.status == "REJECTED"
    assert REJECT_WRITE_DISABLED in order.rejection_reason
    write_calls = [c for c in transport.calls if c[0] == "POST" and c[1] == OPEN_ORDERS_PATH_TEMPLATE.format(acc_id=DEFAULT_ACCOUNT_ID)]
    assert write_calls == []


# ---------------------------------------------------------------------
# Type gate -- only a real ExecutionIntent may reach order construction
# ---------------------------------------------------------------------


def test_raw_ai_text_rejected():
    transport = FakeHttpTransport()
    broker = make_paper_broker(transport)
    with pytest.raises(TypeError):
        broker.submit_execution_intent("APPROVED, buy 100 shares of ABC")
    assert transport.calls == []


def test_trade_proposal_rejected():
    transport = FakeHttpTransport()
    broker = make_paper_broker(transport)
    proposal = TradeProposal.from_dict(base_proposal())
    with pytest.raises(TypeError):
        broker.submit_execution_intent(proposal)
    assert transport.calls == []


def test_dict_rejected():
    transport = FakeHttpTransport()
    broker = make_paper_broker(transport)
    with pytest.raises(TypeError):
        broker.submit_execution_intent({"action": "BUY", "ticker": "ABC"})
    assert transport.calls == []


def test_fake_approval_dict_rejected():
    transport = FakeHttpTransport()
    broker = make_paper_broker(transport)
    with pytest.raises(TypeError):
        broker.submit_execution_intent({"execution_allowed": True, "ticker": "ABC", "action": "BUY"})
    assert transport.calls == []


def test_execution_allowed_false_rejected():
    transport = FakeHttpTransport()
    broker = make_paper_broker(transport)
    intent = forged_intent()
    object.__setattr__(intent, "execution_allowed", False)
    order = broker.submit_execution_intent(intent)
    assert order.status == "REJECTED"
    assert transport.calls == []


def test_live_mode_rejected():
    transport = FakeHttpTransport()
    broker = make_paper_broker(transport)
    order = broker.submit_execution_intent(forged_intent(account_mode="LIVE"))
    assert order.status == "REJECTED"
    assert "PAPER/SIMULATED" in order.rejection_reason
    assert transport.calls == []


def test_option_instrument_rejected():
    transport = FakeHttpTransport()
    broker = make_paper_broker(transport)
    order = broker.submit_execution_intent(forged_intent(instrument_type="option"))
    assert order.status == "REJECTED"
    assert transport.calls == []


def test_fractional_share_instrument_rejected():
    transport = FakeHttpTransport()
    broker = make_paper_broker(transport)
    order = broker.submit_execution_intent(forged_intent(instrument_type="fractional_share"))
    assert order.status == "REJECTED"
    assert "fractional" in order.rejection_reason.lower()
    assert transport.calls == []


def test_hold_and_do_nothing_never_reach_broker():
    # HOLD/DO_NOTHING never produce an ExecutionIntent at all (see
    # execution/intent.py) -- there is nothing to submit. Confirmed via
    # the full pipeline instead of forging an intent for a non-executable action.
    proposal = base_proposal(action="HOLD")
    from models.trade_proposal import evaluate_trade_proposal

    decision = evaluate_trade_proposal(proposal, **account_state())
    assert decision["decision"] == "NO_ACTION"
    result = create_execution_intent(proposal, **account_state())
    assert result.created is False
    assert result.intent is None


# ---------------------------------------------------------------------
# Staleness
# ---------------------------------------------------------------------


def test_stale_intent_rejected():
    transport = FakeHttpTransport()
    broker = make_paper_broker(transport)
    old_created_at = (datetime.now(timezone.utc) - timedelta(seconds=600)).isoformat()
    order = broker.submit_execution_intent(forged_intent(created_at=old_created_at))
    assert order.status == "REJECTED"
    assert "old" in order.rejection_reason
    assert transport.calls == []


def test_stale_quote_rejected():
    transport = FakeHttpTransport()
    stale_time_ms = int((datetime.now(timezone.utc) - timedelta(minutes=5)).timestamp() * 1000)
    queue_full_read_setup(transport, positions=[], quote_time_ms=stale_time_ms)
    broker = make_paper_broker(transport)
    order = broker.submit_execution_intent(forged_intent())
    assert order.status == "REJECTED"
    assert "stale" in order.rejection_reason.lower() or "old" in order.rejection_reason.lower() or "freshness" in order.rejection_reason.lower()


# ---------------------------------------------------------------------
# BUY / ADD construction
# ---------------------------------------------------------------------


def test_approved_paper_buy_creates_correct_mock_request():
    transport = FakeHttpTransport()
    queue_full_read_setup(transport, cash="1000.00", positions=[], ticker="ABC", price="50.00")
    queue_place_order_accepted(transport, order_id="o-buy-1")
    queue_order_lookup(transport, history_orders=[_order_record("o-buy-1", side=1, status=4, qty="3", cum_qty="3", price="50.00")])

    broker = make_paper_broker(transport, write_enabled=True)
    intent = forged_intent(action="BUY", dollar_amount=150.0, audit_reference="audit-buy-1")
    order = broker.submit_execution_intent(intent)

    place_calls = [c for c in transport.calls if c[0] == "POST" and c[1] == OPEN_ORDERS_PATH_TEMPLATE.format(acc_id=DEFAULT_ACCOUNT_ID)]
    assert len(place_calls) == 1
    assert place_calls[0][2] == {
        "market": US_MARKET_ID,
        "symbol": "ABC",
        "order_type": ORDER_TYPE_MARKET,
        "order_side": ORDER_SIDE_BUY,
        "qty": "3",
        "text": "beanstock:audit-buy-1",
    }
    assert order.status == "FILLED"
    assert order.order_id == "o-buy-1"


def test_valid_add_creates_correct_mock_request():
    transport = FakeHttpTransport()
    queue_full_read_setup(
        transport,
        cash="1000.00",
        positions=[{"symbol": "ABC", "qty": "2", "cost_price": "48.00", "mv": "100.00", "profit": "4.00"}],
        ticker="ABC",
        price="50.00",
    )
    queue_place_order_accepted(transport, order_id="o-add-1")
    queue_order_lookup(transport, history_orders=[_order_record("o-add-1", side=1, status=4, qty="2", cum_qty="2", price="50.00")])

    broker = make_paper_broker(transport, write_enabled=True)
    intent = forged_intent(action="ADD", dollar_amount=100.0, audit_reference="audit-add-1")
    order = broker.submit_execution_intent(intent)

    place_calls = [c for c in transport.calls if c[0] == "POST" and c[1] == OPEN_ORDERS_PATH_TEMPLATE.format(acc_id=DEFAULT_ACCOUNT_ID)]
    assert place_calls[0][2]["qty"] == "2"
    assert place_calls[0][2]["order_side"] == ORDER_SIDE_BUY
    assert order.status == "FILLED"


def test_add_without_existing_position_rejected():
    transport = FakeHttpTransport()
    queue_full_read_setup(transport, positions=[])
    broker = make_paper_broker(transport, write_enabled=True)
    order = broker.submit_execution_intent(forged_intent(action="ADD", dollar_amount=100.0))
    assert order.status == "REJECTED"
    assert "ADD requires an existing position" in order.rejection_reason


def test_buy_with_existing_position_rejected():
    transport = FakeHttpTransport()
    queue_full_read_setup(transport, positions=[{"symbol": "ABC", "qty": "2", "cost_price": "48.00", "mv": "100.00", "profit": "4.00"}])
    broker = make_paper_broker(transport, write_enabled=True)
    order = broker.submit_execution_intent(forged_intent(action="BUY", dollar_amount=100.0))
    assert order.status == "REJECTED"
    assert "BUY requires no existing position" in order.rejection_reason


def test_insufficient_cash_rejected():
    transport = FakeHttpTransport()
    queue_full_read_setup(transport, cash="100.00", positions=[])
    broker = make_paper_broker(transport, write_enabled=True)
    order = broker.submit_execution_intent(forged_intent(action="BUY", dollar_amount=150.0))
    assert order.status == "REJECTED"
    assert "Insufficient cash" in order.rejection_reason


def test_buy_rounds_to_zero_whole_shares_rejected():
    transport = FakeHttpTransport()
    queue_full_read_setup(transport, cash="1000.00", positions=[], price="50.00")
    broker = make_paper_broker(transport, write_enabled=True)
    order = broker.submit_execution_intent(forged_intent(action="BUY", dollar_amount=45.0))
    assert order.status == "REJECTED"
    assert "0 whole shares" in order.rejection_reason


# ---------------------------------------------------------------------
# REDUCE / EXIT
# ---------------------------------------------------------------------


def test_reduce_cannot_exceed_holdings():
    transport = FakeHttpTransport()
    queue_full_read_setup(transport, positions=[{"symbol": "ABC", "qty": "2", "cost_price": "45.00", "mv": "100.00", "profit": "10.00"}], price="50.00")
    broker = make_paper_broker(transport, write_enabled=True)
    # Would sell 3 shares (150/50) against a held quantity of 2.
    order = broker.submit_execution_intent(forged_intent(action="REDUCE", dollar_amount=150.0))
    assert order.status == "REJECTED"
    assert "short position" in order.rejection_reason


def test_reduce_within_holdings_creates_correct_mock_request():
    transport = FakeHttpTransport()
    queue_full_read_setup(transport, positions=[{"symbol": "ABC", "qty": "5", "cost_price": "45.00", "mv": "250.00", "profit": "25.00"}], price="50.00")
    queue_place_order_accepted(transport, order_id="o-reduce-1")
    queue_order_lookup(transport, history_orders=[_order_record("o-reduce-1", side=2, status=4, qty="2", cum_qty="2", price="50.00")])

    broker = make_paper_broker(transport, write_enabled=True)
    order = broker.submit_execution_intent(forged_intent(action="REDUCE", dollar_amount=100.0, audit_reference="audit-reduce-1"))

    place_calls = [c for c in transport.calls if c[0] == "POST" and c[1] == OPEN_ORDERS_PATH_TEMPLATE.format(acc_id=DEFAULT_ACCOUNT_ID)]
    assert place_calls[0][2]["qty"] == "2"
    assert place_calls[0][2]["order_side"] == ORDER_SIDE_SELL
    assert order.status == "FILLED"


def test_exit_cannot_exceed_holdings():
    transport = FakeHttpTransport()
    queue_full_read_setup(transport, positions=[{"symbol": "ABC", "qty": "7", "cost_price": "45.00", "mv": "350.00", "profit": "35.00"}], price="50.00")
    queue_place_order_accepted(transport, order_id="o-exit-1")
    queue_order_lookup(transport, history_orders=[_order_record("o-exit-1", side=2, status=4, qty="7", cum_qty="7", price="50.00")])

    broker = make_paper_broker(transport, write_enabled=True)
    order = broker.submit_execution_intent(forged_intent(action="EXIT", dollar_amount=None, audit_reference="audit-exit-1"))

    place_calls = [c for c in transport.calls if c[0] == "POST" and c[1] == OPEN_ORDERS_PATH_TEMPLATE.format(acc_id=DEFAULT_ACCOUNT_ID)]
    assert place_calls[0][2]["qty"] == "7"  # exactly the held quantity, never more
    assert order.status == "FILLED"
    assert order.filled_quantity == Decimal("7")


def test_exit_without_position_rejected():
    transport = FakeHttpTransport()
    queue_full_read_setup(transport, positions=[])
    broker = make_paper_broker(transport, write_enabled=True)
    order = broker.submit_execution_intent(forged_intent(action="EXIT"))
    assert order.status == "REJECTED"
    assert "EXIT requires an existing position" in order.rejection_reason


# ---------------------------------------------------------------------
# SAFE_MODE / daily loss / weekly drawdown gates (broker's own copy)
# ---------------------------------------------------------------------


def test_safe_mode_buy_rejected():
    transport = FakeHttpTransport()
    broker = make_paper_broker(transport, write_enabled=True)
    broker.safe_mode = True
    order = broker.submit_execution_intent(forged_intent(action="BUY", dollar_amount=150.0))
    assert order.status == "REJECTED"
    assert "SAFE_MODE" in order.rejection_reason
    assert transport.calls == []  # rejected before any read was needed


def test_safe_mode_exit_allowed_when_risk_reducing():
    transport = FakeHttpTransport()
    queue_full_read_setup(transport, positions=[{"symbol": "ABC", "qty": "4", "cost_price": "45.00", "mv": "200.00", "profit": "20.00"}], price="50.00")
    broker = make_paper_broker(transport, write_enabled=False)  # write disabled -- expect it to get all the way there anyway
    broker.safe_mode = True
    order = broker.submit_execution_intent(forged_intent(action="EXIT"))
    assert "SAFE_MODE" not in (order.rejection_reason or "")
    assert REJECT_WRITE_DISABLED in order.rejection_reason  # blocked only by the write gate, not safe_mode


def test_daily_loss_buy_rejected():
    transport = FakeHttpTransport()
    broker = make_paper_broker(transport, write_enabled=True)
    broker.daily_loss_breached = True
    order = broker.submit_execution_intent(forged_intent(action="BUY", dollar_amount=150.0))
    assert order.status == "REJECTED"
    assert "Daily loss" in order.rejection_reason
    assert transport.calls == []


def test_weekly_drawdown_add_rejected():
    transport = FakeHttpTransport()
    broker = make_paper_broker(transport, write_enabled=True)
    broker.weekly_drawdown_breached = True
    order = broker.submit_execution_intent(forged_intent(action="ADD", dollar_amount=100.0))
    assert order.status == "REJECTED"
    assert "Weekly drawdown" in order.rejection_reason
    assert transport.calls == []


# ---------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------


def test_duplicate_intent_blocked():
    transport = FakeHttpTransport()
    queue_full_read_setup(transport, cash="1000.00", positions=[], price="50.00")
    queue_place_order_accepted(transport, order_id="o-dup-1")
    queue_order_lookup(transport, history_orders=[_order_record("o-dup-1", side=1, status=4, qty="3", cum_qty="3", price="50.00")])

    broker = make_paper_broker(transport, write_enabled=True)
    intent = forged_intent(action="BUY", dollar_amount=150.0, audit_reference="audit-dup")

    first = broker.submit_execution_intent(intent)
    assert first.status == "FILLED"

    second = broker.submit_execution_intent(intent)
    assert second.status == "REJECTED"
    assert REJECT_DUPLICATE in second.rejection_reason

    # The second submission never touched HTTP at all.
    place_calls = [c for c in transport.calls if c[0] == "POST" and c[1] == OPEN_ORDERS_PATH_TEMPLATE.format(acc_id=DEFAULT_ACCOUNT_ID)]
    assert len(place_calls) == 1


def test_write_disabled_rejection_does_not_block_future_retry():
    # A write-disabled rejection must NOT permanently consume the
    # audit_reference -- the same approved decision must still be
    # submittable once BEANSTOCK_PAPER_WRITE_ENABLED is later turned on.
    transport = FakeHttpTransport()
    queue_full_read_setup(transport, cash="1000.00", positions=[], price="50.00")
    broker = make_paper_broker(transport, write_enabled=False)
    intent = forged_intent(action="BUY", dollar_amount=150.0, audit_reference="audit-retry")

    first = broker.submit_execution_intent(intent)
    assert first.status == "REJECTED"
    assert REJECT_WRITE_DISABLED in first.rejection_reason

    second = broker.submit_execution_intent(intent)
    assert REJECT_DUPLICATE not in (second.rejection_reason or "")
    assert REJECT_WRITE_DISABLED in second.rejection_reason


# ---------------------------------------------------------------------
# HTTP failure modes (write_enabled=True to actually reach the HTTP layer)
# ---------------------------------------------------------------------


def test_malformed_place_order_response_fails_closed():
    transport = FakeHttpTransport()
    queue_full_read_setup(transport, cash="1000.00", positions=[], price="50.00")
    transport.queue_post(OPEN_ORDERS_PATH_TEMPLATE.format(acc_id=DEFAULT_ACCOUNT_ID), json_response(200, {"ret_code": 0, "data": {}}))  # missing order_id
    broker = make_paper_broker(transport, write_enabled=True)
    with pytest.raises(MalformedResponseError):
        broker.submit_execution_intent(forged_intent(action="BUY", dollar_amount=150.0))


def test_unauthorized_place_order_response_fails_closed():
    transport = FakeHttpTransport()
    queue_full_read_setup(transport, cash="1000.00", positions=[], price="50.00")
    transport.queue_post(OPEN_ORDERS_PATH_TEMPLATE.format(acc_id=DEFAULT_ACCOUNT_ID), json_response(401, {"error": "invalid_token"}))
    broker = make_paper_broker(transport, write_enabled=True)
    with pytest.raises(MoomooAuthenticationError):
        broker.submit_execution_intent(forged_intent(action="BUY", dollar_amount=150.0))


def test_place_order_timeout_fails_closed():
    transport = FakeHttpTransport()
    queue_full_read_setup(transport, cash="1000.00", positions=[], price="50.00")
    transport.queue_post(OPEN_ORDERS_PATH_TEMPLATE.format(acc_id=DEFAULT_ACCOUNT_ID), TransportTimeout("timed out"))
    broker = make_paper_broker(transport, write_enabled=True)
    with pytest.raises(MoomooTimeoutError):
        broker.submit_execution_intent(forged_intent(action="BUY", dollar_amount=150.0))


def test_rate_limited_place_order_fails_closed():
    transport = FakeHttpTransport()
    queue_full_read_setup(transport, cash="1000.00", positions=[], price="50.00")
    transport.queue_post(OPEN_ORDERS_PATH_TEMPLATE.format(acc_id=DEFAULT_ACCOUNT_ID), json_response(429, {"error": "rate_limited"}))
    broker = make_paper_broker(transport, write_enabled=True)
    with pytest.raises(MoomooRateLimitError):
        broker.submit_execution_intent(forged_intent(action="BUY", dollar_amount=150.0))


def test_server_error_place_order_fails_closed():
    transport = FakeHttpTransport()
    queue_full_read_setup(transport, cash="1000.00", positions=[], price="50.00")
    transport.queue_post(OPEN_ORDERS_PATH_TEMPLATE.format(acc_id=DEFAULT_ACCOUNT_ID), json_response(500, {"error": "internal"}))
    broker = make_paper_broker(transport, write_enabled=True)
    with pytest.raises(MoomooServerError):
        broker.submit_execution_intent(forged_intent(action="BUY", dollar_amount=150.0))


def test_partial_fill_status_normalized():
    transport = FakeHttpTransport()
    queue_full_read_setup(transport, cash="1000.00", positions=[], price="50.00")
    queue_place_order_accepted(transport, order_id="o-partial-1")
    # status=3 -> Partially Filled, mapped onto Beanstock's FILLED
    # (Order model has no PARTIAL state -- see broker/moomoo_readonly.py).
    queue_order_lookup(transport, open_orders=[_order_record("o-partial-1", side=1, status=3, qty="3", cum_qty="1", price="50.00")])
    broker = make_paper_broker(transport, write_enabled=True)
    order = broker.submit_execution_intent(forged_intent(action="BUY", dollar_amount=150.0))
    assert order.status == "FILLED"
    assert order.filled_quantity == Decimal("1")  # only 1 of 3 requested actually filled so far


def test_canceled_order_status_normalized():
    transport = FakeHttpTransport()
    queue_full_read_setup(transport, cash="1000.00", positions=[], price="50.00")
    queue_place_order_accepted(transport, order_id="o-canceled-1")
    queue_order_lookup(transport, open_orders=[_order_record("o-canceled-1", side=1, status=5, qty="3", cum_qty="0", price="50.00")])
    broker = make_paper_broker(transport, write_enabled=True)
    order = broker.submit_execution_intent(forged_intent(action="BUY", dollar_amount=150.0))
    assert order.status == "CANCELED"


def test_place_order_still_pending_when_not_found_in_order_lookup():
    transport = FakeHttpTransport()
    queue_full_read_setup(transport, cash="1000.00", positions=[], price="50.00")
    queue_place_order_accepted(transport, order_id="o-pending-1")
    queue_order_lookup(transport)  # empty -- order not found yet in either list
    broker = make_paper_broker(transport, write_enabled=True)
    order = broker.submit_execution_intent(forged_intent(action="BUY", dollar_amount=150.0))
    assert order.status == "PENDING"
    assert order.order_id == "o-pending-1"


# ---------------------------------------------------------------------
# cancel_order write gate
# ---------------------------------------------------------------------


def test_cancel_order_write_disabled_raises():
    transport = FakeHttpTransport()
    queue_full_read_setup(transport, cash="1000.00", positions=[], price="50.00")
    queue_place_order_accepted(transport)
    queue_order_lookup(transport)  # PENDING (not found)
    broker = make_paper_broker(transport, write_enabled=True)
    order = broker.submit_execution_intent(forged_intent(action="BUY", dollar_amount=150.0))
    assert order.status == "PENDING"

    broker._write_enabled = False  # simulate write later disabled again
    with pytest.raises(PaperWriteDisabledError):
        broker.cancel_order(order.order_id)


def test_close_position_write_disabled_returns_rejected():
    transport = FakeHttpTransport()
    queue_full_read_setup(transport, positions=[{"symbol": "ABC", "qty": "4", "cost_price": "45.00", "mv": "200.00", "profit": "20.00"}], price="50.00")
    broker = make_paper_broker(transport, write_enabled=False)
    order = broker.close_position("ABC")
    assert order.status == "REJECTED"
    assert REJECT_WRITE_DISABLED in order.rejection_reason


def test_close_position_creates_correct_mock_request_when_enabled():
    transport = FakeHttpTransport()
    queue_full_read_setup(transport, positions=[{"symbol": "ABC", "qty": "4", "cost_price": "45.00", "mv": "200.00", "profit": "20.00"}], price="50.00")
    queue_place_order_accepted(transport, order_id="o-close-1")
    queue_order_lookup(transport, history_orders=[_order_record("o-close-1", side=2, status=4, qty="4", cum_qty="4", price="50.00")])
    broker = make_paper_broker(transport, write_enabled=True)
    order = broker.close_position("ABC")
    place_calls = [c for c in transport.calls if c[0] == "POST" and c[1] == OPEN_ORDERS_PATH_TEMPLATE.format(acc_id=DEFAULT_ACCOUNT_ID)]
    assert place_calls[0][2]["qty"] == "4"
    assert place_calls[0][2]["order_side"] == ORDER_SIDE_SELL
    assert order.status == "FILLED"


def test_max_buy_quantity_helper():
    transport = FakeHttpTransport()
    queue_account_resolution(transport)
    transport.queue_get(
        MAX_BUY_SELL_PATH_TEMPLATE.format(acc_id=DEFAULT_ACCOUNT_ID),
        json_response(200, envelope({"max_cash_buy_qty_round_lot": "42"})),
    )
    broker = make_paper_broker(transport)
    assert broker.max_buy_quantity("ABC") == Decimal("42")


# ---------------------------------------------------------------------
# Item 10 -- full mock integration test
# ---------------------------------------------------------------------


def test_full_mock_integration_300_equity_abc_50_buy_and_exit():
    """$300 starting simulated equity, ABC quoted at $50, run the full
    real pipeline: TradeProposal -> schema validation -> risk engine
    -> DecisionResult -> ExecutionIntent -> BrokerGateway -> MoomooPaperBroker.

    Part A: a fresh BUY at the small-account 15% hard cap (0.15*$300 =
    $45) is APPROVED by the risk engine -- but $45 / $50 = 0.9 shares,
    and MoomooPaperBroker only ever trades whole shares (fractional
    support is unverified against the real Place Order contract). This
    is a genuine structural fact about a $300 account and a $50 stock:
    no whole-share BUY can ever fit under the 15% cap here. The broker
    correctly refuses to round up and buy a share it wasn't authorized
    to buy -- proving the fail-closed fractional-share rule catches a
    real case the upstream pipeline has no concept of. Confirms no HTTP
    write call happens (BEANSTOCK_PAPER_WRITE_ENABLED defaults False).

    Part B: EXIT bypasses the entry risk engine's position-sizing gate
    entirely (see execution/intent.py), so it's what actually reaches a
    real (mocked) place-order call in this scenario -- closing an
    existing 3-share ABC position, with write_enabled=True this time,
    verifying the accepted-order response is correctly normalized into
    a FILLED Order and logged to the audit trail.
    """
    # --- Part A ---
    transport_a = FakeHttpTransport()
    queue_full_read_setup(transport_a, cash="300.00", positions=[], ticker="ABC", price="50.00")
    broker_a = make_paper_broker(transport_a, write_enabled=False)

    buy_proposal = base_proposal(
        ticker="ABC",
        action="BUY",
        current_price=50.0,
        intended_entry=50.0,
        stop_price=45.0,
        target_price=62.5,
        proposed_dollar_amount=45.0,
        proposed_allocation_pct=15.0,
    )
    buy_result = create_execution_intent(buy_proposal, **account_state())
    assert buy_result.created is True, buy_result.reasons
    assert buy_result.decision_status == "APPROVE"

    gateway_a = BrokerGateway()
    gateway_result_a, order_a = gateway_a.submit(buy_result.intent, broker_a)
    assert gateway_result_a.allowed is True, gateway_result_a.reasons
    assert order_a is not None
    assert order_a.status == "REJECTED"
    assert "0 whole shares" in order_a.rejection_reason

    write_calls_a = [c for c in transport_a.calls if c[0] == "POST" and c[1] == OPEN_ORDERS_PATH_TEMPLATE.format(acc_id=DEFAULT_ACCOUNT_ID)]
    assert write_calls_a == []

    # --- Part B ---
    transport_b = FakeHttpTransport()
    queue_full_read_setup(
        transport_b,
        cash="150.00",
        positions=[{"symbol": "ABC", "qty": "3", "cost_price": "50.00", "mv": "150.00", "profit": "0.00"}],
        ticker="ABC",
        price="50.00",
    )
    queue_place_order_accepted(transport_b, order_id="moomoo-order-777")
    queue_order_lookup(transport_b, history_orders=[_order_record("moomoo-order-777", symbol="ABC", side=2, status=4, qty="3", cum_qty="3", price="50.00")])
    broker_b = make_paper_broker(transport_b, write_enabled=True)

    exit_proposal = base_proposal(ticker="ABC", action="EXIT", current_price=50.0, intended_entry=50.0)
    exit_result = create_execution_intent(exit_proposal, has_existing_position=True, **account_state(available_cash=150.0, current_positions=1, trades_this_week=1))
    assert exit_result.created is True, exit_result.reasons
    assert exit_result.decision_status == "EXIT_ALLOWED"

    gateway_b = BrokerGateway()
    gateway_result_b, order_b = gateway_b.submit(exit_result.intent, broker_b)
    assert gateway_result_b.allowed is True, gateway_result_b.reasons
    assert order_b is not None
    assert order_b.status == "FILLED"
    assert order_b.order_id == "moomoo-order-777"
    assert order_b.filled_quantity == Decimal("3")
    assert order_b.fill_price == Decimal("50.00")

    place_calls_b = [c for c in transport_b.calls if c[0] == "POST" and c[1] == OPEN_ORDERS_PATH_TEMPLATE.format(acc_id=DEFAULT_ACCOUNT_ID)]
    assert len(place_calls_b) == 1
    assert place_calls_b[0][2]["qty"] == "3"
    assert place_calls_b[0][2]["order_side"] == ORDER_SIDE_SELL

    audit_log_b = broker_b.get_audit_log()
    assert len(audit_log_b) == 1
    assert audit_log_b[0]["status"] == "FILLED"
    assert audit_log_b[0]["order_id"] == "moomoo-order-777"
