"""Tests for PaperWriteController (execution/paper_write_controller.py).

No test in this file makes a real network call (FakeHttpTransport only),
and BEANSTOCK_PAPER_WRITE_ENABLED is never set in the real environment --
every test needing the "write enabled" path passes write_enabled=True
explicitly to MoomooPaperBroker's/PaperWriteController's own constructor
overrides.
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest

from broker.http_transport import HttpResponse, HttpTransport, TransportTimeout
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
from broker.moomoo_paper import ENV_WRITE_ENABLED, MoomooPaperBroker, ORDER_SIDE_BUY
from execution.intent import ExecutionIntent, create_execution_intent, _THE_APPROVAL_TOKEN
from execution.paper_write_controller import (
    ALLOWED_READ_PATH_PREFIXES,
    ALLOWED_WRITE_PATH_PREFIXES,
    DEFAULT_FIRST_TEST_MAX_NOTIONAL,
    REJECT_DUPLICATE,
    STATE_ARMED,
    STATE_DISARMED,
    PaperWriteController,
    PathFirewallViolation,
    guard_read_path,
    guard_write_path,
)
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
        "order_id": order_id, "symbol": symbol, "side": side, "status": status,
        "qty": qty, "cum_qty": cum_qty, "price": price, "create_time": create_time,
    }


def queue_account_resolution(transport, account_id=DEFAULT_ACCOUNT_ID):
    transport.queue_get(ACCOUNTS_PATH, json_response(200, accounts_envelope(us_account_ids=[account_id])))


def queue_cash(transport, account_id=DEFAULT_ACCOUNT_ID, cash="1000.00"):
    transport.queue_get(CASH_INFO_PATH_TEMPLATE.format(acc_id=account_id), json_response(200, envelope({"balance": cash, "total_asset": cash})))


def queue_positions(transport, account_id=DEFAULT_ACCOUNT_ID, positions=None):
    transport.queue_get(POSITIONS_PATH_TEMPLATE.format(acc_id=account_id), json_response(200, envelope({"positions": positions or []})))


def queue_quote(transport, ticker="ABC", price="50.00", data_time_ms=None, market_prefix="US"):
    if data_time_ms is None:
        data_time_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    transport.queue_post(QUOTE_PATH, json_response(200, envelope({"quote_list": [{"code": f"{market_prefix}.{ticker}", "last_price": price, "data_time": data_time_ms}]})))


def queue_full_read_setup(transport, *, account_id=DEFAULT_ACCOUNT_ID, cash="1000.00", positions=None, ticker="ABC", price="50.00", quote_time_ms=None):
    queue_account_resolution(transport, account_id)
    queue_cash(transport, account_id, cash)
    queue_positions(transport, account_id, positions)
    queue_quote(transport, ticker, price, quote_time_ms)


def queue_place_order_accepted(transport, account_id=DEFAULT_ACCOUNT_ID, order_id="moomoo-order-1"):
    transport.queue_post(OPEN_ORDERS_PATH_TEMPLATE.format(acc_id=account_id), json_response(200, {"ret_code": 0, "data": {"order_id": order_id}}))


def queue_order_lookup(transport, account_id=DEFAULT_ACCOUNT_ID, open_orders=None, history_orders=None):
    transport.queue_get(OPEN_ORDERS_PATH_TEMPLATE.format(acc_id=account_id), json_response(200, envelope({"orders": open_orders or []})))
    transport.queue_get(HISTORY_ORDERS_PATH_TEMPLATE.format(acc_id=account_id), json_response(200, envelope({"orders": history_orders or [], "pagination": {"has_more": False, "next_key": ""}})))


def make_paper_broker(transport, *, account_id_override=DEFAULT_ACCOUNT_ID, write_enabled=False, token=None):
    return MoomooPaperBroker(
        access_token_provider=token or (lambda: "fake-access-token"),
        http_transport=transport,
        simulated_account_id=account_id_override,
        write_enabled=write_enabled,
    )


def forged_intent(**overrides):
    fields = dict(
        ticker="ABC", action="BUY", instrument_type="stock", quantity=3.0, dollar_amount=150.0,
        intended_order_type="MARKET", reference_price=50.0, stop_price=45.0, target_price=65.0,
        decision_status="APPROVE", audit_reference="audit-forged",
        created_at=datetime.now(timezone.utc).isoformat(), account_mode="PAPER",
    )
    fields.update(overrides)
    return ExecutionIntent._create_approved(_THE_APPROVAL_TOKEN, **fields)


def base_proposal(**overrides):
    proposal = {
        "ticker": "XYZ", "instrument_type": "stock", "action": "BUY",
        "current_price": 10.0, "intended_entry": 10.0, "candidate_score": 88,
        "catalyst": "Earnings beat with raised guidance",
        "catalyst_timing": "Reported this morning, guidance raise effective immediately",
        "bull_case": "Margin expansion continues into next two quarters",
        "bear_case": "Multiple compression if growth decelerates",
        "thesis_invalidation": "Guidance is walked back or margins compress q/q",
        "stop_price": 9.0, "target_price": 12.5, "proposed_dollar_amount": 45.0,
        "proposed_allocation_pct": 15.0, "sector": "Technology", "confidence": 0.7,
        "holding_period": "2-6 weeks", "reason_to_buy_now": "Catalyst is fresh",
        "reason_to_wait": "None", "data_timestamp": "2026-09-06T09:35:00Z", "reward_risk": 2.5,
    }
    proposal.update(overrides)
    return proposal


def account_state(**overrides):
    state = dict(
        account_equity=300.0, available_cash=300.0, current_positions=0, trades_this_week=0,
        current_company_exposure_pct=0.0, current_sector_exposure_pct=0.0, blocked_sectors=[],
        account_mode="PAPER",
    )
    state.update(overrides)
    return state


# ---------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------


def test_controller_defaults_disarmed():
    controller = PaperWriteController()
    assert controller.state == STATE_DISARMED


def test_write_flag_defaults_false(monkeypatch):
    monkeypatch.delenv(ENV_WRITE_ENABLED, raising=False)
    controller = PaperWriteController()
    assert controller.write_enabled is False


# ---------------------------------------------------------------------
# Write-flag x armed-state combinations
# ---------------------------------------------------------------------


def test_flag_false_armed_blocks_write():
    transport = FakeHttpTransport()
    queue_full_read_setup(transport, positions=[])
    broker = make_paper_broker(transport, write_enabled=False)
    gateway = BrokerGateway()
    controller = PaperWriteController(write_enabled=False)
    controller.arm()

    result, order = controller.submit(forged_intent(), gateway, broker)
    assert result.allowed is False
    assert order is None
    assert transport.calls == []


def test_flag_true_disarmed_blocks_write():
    transport = FakeHttpTransport()
    queue_full_read_setup(transport, positions=[])
    broker = make_paper_broker(transport, write_enabled=True)
    gateway = BrokerGateway()
    controller = PaperWriteController(write_enabled=True)  # never armed

    result, order = controller.submit(forged_intent(), gateway, broker)
    assert result.allowed is False
    assert controller.state == STATE_DISARMED
    assert order is None
    assert transport.calls == []


def test_flag_true_armed_valid_paper_permits_write():
    transport = FakeHttpTransport()
    queue_full_read_setup(transport, cash="1000.00", positions=[], price="50.00")
    queue_place_order_accepted(transport, order_id="o-armed-1")
    queue_order_lookup(transport, history_orders=[_order_record("o-armed-1", side=1, status=4, qty="3", cum_qty="3", price="50.00")])

    broker = make_paper_broker(transport, write_enabled=True)
    gateway = BrokerGateway()
    controller = PaperWriteController(write_enabled=True)
    controller.arm()

    result, order = controller.submit(forged_intent(audit_reference="audit-armed-1"), gateway, broker)
    assert result.allowed is True
    assert order is not None
    assert order.status == "FILLED"
    place_calls = [c for c in transport.calls if c[0] == "POST" and c[1] == OPEN_ORDERS_PATH_TEMPLATE.format(acc_id=DEFAULT_ACCOUNT_ID)]
    assert len(place_calls) == 1


# ---------------------------------------------------------------------
# LIVE mode / path firewall
# ---------------------------------------------------------------------


def test_live_mode_cannot_pass_even_when_armed_and_enabled():
    transport = FakeHttpTransport()
    broker = make_paper_broker(transport, write_enabled=True)
    gateway = BrokerGateway()
    controller = PaperWriteController(write_enabled=True)
    controller.arm()

    result, order = controller.submit(forged_intent(account_mode="LIVE"), gateway, broker)
    assert result.allowed is False
    assert "LIVE" in " ".join(result.reasons)
    assert order is None
    assert transport.calls == []


def test_live_rest_path_blocked():
    for live_path in ("/api/v1.0/trade/place-order", "/api/v1.0/accounts/authorized_trd_accs"):
        with pytest.raises(PathFirewallViolation):
            guard_write_path(live_path)
        with pytest.raises(PathFirewallViolation):
            guard_read_path(live_path)


def test_unknown_path_blocked():
    with pytest.raises(PathFirewallViolation):
        guard_write_path("/api/v1.0/something-undocumented/orders")
    with pytest.raises(PathFirewallViolation):
        guard_read_path("/api/v2.0/sim-trade/accounts")  # different version -- unknown family


def test_read_and_write_firewall_allow_documented_sim_trade():
    guard_read_path("/api/v1.0/sim-trade/accounts")
    guard_read_path("/api/v1.0/quote/stock-quote")
    guard_write_path("/api/v1.0/sim-trade/acc-1/orders")


# ---------------------------------------------------------------------
# SAFE_MODE / daily loss / weekly drawdown (controller's own copy)
# ---------------------------------------------------------------------


def test_safe_mode_buy_blocked():
    transport = FakeHttpTransport()
    broker = make_paper_broker(transport, write_enabled=True)
    gateway = BrokerGateway()
    controller = PaperWriteController(write_enabled=True)
    controller.arm()
    controller.safe_mode = True

    result, order = controller.submit(forged_intent(action="BUY"), gateway, broker)
    assert result.allowed is False
    assert "SAFE_MODE" in " ".join(result.reasons)
    assert transport.calls == []


def test_safe_mode_add_blocked():
    transport = FakeHttpTransport()
    broker = make_paper_broker(transport, write_enabled=True)
    gateway = BrokerGateway()
    controller = PaperWriteController(write_enabled=True)
    controller.arm()
    controller.safe_mode = True

    result, order = controller.submit(forged_intent(action="ADD", dollar_amount=100.0), gateway, broker)
    assert result.allowed is False
    assert "SAFE_MODE" in " ".join(result.reasons)


def test_safe_mode_reduce_permitted_when_valid():
    transport = FakeHttpTransport()
    queue_full_read_setup(transport, positions=[{"symbol": "ABC", "qty": "5", "cost_price": "45.00", "mv": "250.00", "profit": "25.00"}], price="50.00")
    queue_place_order_accepted(transport, order_id="o-safe-reduce")
    queue_order_lookup(transport, history_orders=[_order_record("o-safe-reduce", side=2, status=4, qty="2", cum_qty="2", price="50.00")])

    broker = make_paper_broker(transport, write_enabled=True)
    gateway = BrokerGateway()
    controller = PaperWriteController(write_enabled=True)
    controller.arm()
    controller.safe_mode = True

    result, order = controller.submit(forged_intent(action="REDUCE", dollar_amount=100.0, audit_reference="audit-safe-reduce"), gateway, broker)
    assert result.allowed is True
    assert order.status == "FILLED"


def test_safe_mode_exit_permitted_when_valid():
    transport = FakeHttpTransport()
    queue_full_read_setup(transport, positions=[{"symbol": "ABC", "qty": "4", "cost_price": "45.00", "mv": "200.00", "profit": "20.00"}], price="50.00")
    queue_place_order_accepted(transport, order_id="o-safe-exit")
    queue_order_lookup(transport, history_orders=[_order_record("o-safe-exit", side=2, status=4, qty="4", cum_qty="4", price="50.00")])

    broker = make_paper_broker(transport, write_enabled=True)
    gateway = BrokerGateway()
    controller = PaperWriteController(write_enabled=True)
    controller.arm()
    controller.safe_mode = True

    result, order = controller.submit(forged_intent(action="EXIT", dollar_amount=None, audit_reference="audit-safe-exit"), gateway, broker)
    assert result.allowed is True
    assert order.status == "FILLED"


def test_daily_loss_buy_blocked():
    transport = FakeHttpTransport()
    broker = make_paper_broker(transport, write_enabled=True)
    gateway = BrokerGateway()
    controller = PaperWriteController(write_enabled=True)
    controller.arm()
    controller.daily_loss_breached = True

    result, order = controller.submit(forged_intent(action="BUY"), gateway, broker)
    assert result.allowed is False
    assert "Daily loss" in " ".join(result.reasons)


def test_daily_loss_exit_permitted():
    transport = FakeHttpTransport()
    queue_full_read_setup(transport, positions=[{"symbol": "ABC", "qty": "4", "cost_price": "45.00", "mv": "200.00", "profit": "20.00"}], price="50.00")
    queue_place_order_accepted(transport, order_id="o-daily-exit")
    queue_order_lookup(transport, history_orders=[_order_record("o-daily-exit", side=2, status=4, qty="4", cum_qty="4", price="50.00")])

    broker = make_paper_broker(transport, write_enabled=True)
    gateway = BrokerGateway()
    controller = PaperWriteController(write_enabled=True)
    controller.arm()
    controller.daily_loss_breached = True

    result, order = controller.submit(forged_intent(action="EXIT", dollar_amount=None, audit_reference="audit-daily-exit"), gateway, broker)
    assert result.allowed is True
    assert order.status == "FILLED"


def test_weekly_drawdown_add_blocked():
    transport = FakeHttpTransport()
    broker = make_paper_broker(transport, write_enabled=True)
    gateway = BrokerGateway()
    controller = PaperWriteController(write_enabled=True)
    controller.arm()
    controller.weekly_drawdown_breached = True

    result, order = controller.submit(forged_intent(action="ADD", dollar_amount=100.0), gateway, broker)
    assert result.allowed is False
    assert "Weekly drawdown" in " ".join(result.reasons)


def test_weekly_drawdown_reduce_permitted():
    transport = FakeHttpTransport()
    queue_full_read_setup(transport, positions=[{"symbol": "ABC", "qty": "5", "cost_price": "45.00", "mv": "250.00", "profit": "25.00"}], price="50.00")
    queue_place_order_accepted(transport, order_id="o-weekly-reduce")
    queue_order_lookup(transport, history_orders=[_order_record("o-weekly-reduce", side=2, status=4, qty="2", cum_qty="2", price="50.00")])

    broker = make_paper_broker(transport, write_enabled=True)
    gateway = BrokerGateway()
    controller = PaperWriteController(write_enabled=True)
    controller.arm()
    controller.weekly_drawdown_breached = True

    result, order = controller.submit(forged_intent(action="REDUCE", dollar_amount=100.0, audit_reference="audit-weekly-reduce"), gateway, broker)
    assert result.allowed is True
    assert order.status == "FILLED"


# ---------------------------------------------------------------------
# Staleness
# ---------------------------------------------------------------------


def test_stale_quote_blocked():
    transport = FakeHttpTransport()
    stale_time_ms = int((datetime.now(timezone.utc) - timedelta(minutes=5)).timestamp() * 1000)
    queue_full_read_setup(transport, positions=[], quote_time_ms=stale_time_ms)
    broker = make_paper_broker(transport, write_enabled=True)
    gateway = BrokerGateway()
    controller = PaperWriteController(write_enabled=True)
    controller.arm()

    result, order = controller.submit(forged_intent(), gateway, broker)
    assert result.allowed is False
    assert order is None


def test_stale_intent_blocked():
    transport = FakeHttpTransport()
    broker = make_paper_broker(transport, write_enabled=True)
    gateway = BrokerGateway()
    controller = PaperWriteController(write_enabled=True)
    controller.arm()

    old_created_at = (datetime.now(timezone.utc) - timedelta(seconds=600)).isoformat()
    result, order = controller.submit(forged_intent(created_at=old_created_at), gateway, broker)
    assert result.allowed is False
    assert order is None
    assert transport.calls == []


# ---------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------


def test_duplicate_audit_reference_blocked():
    transport = FakeHttpTransport()
    queue_full_read_setup(transport, cash="1000.00", positions=[], price="50.00")
    queue_place_order_accepted(transport, order_id="o-dup-1")
    queue_order_lookup(transport, history_orders=[_order_record("o-dup-1", side=1, status=4, qty="3", cum_qty="3", price="50.00")])

    broker = make_paper_broker(transport, write_enabled=True)
    gateway = BrokerGateway()
    controller = PaperWriteController(write_enabled=True)
    controller.arm()

    intent = forged_intent(audit_reference="audit-dup-ctrl")
    first_result, first_order = controller.submit(intent, gateway, broker)
    assert first_result.allowed is True

    second_result, second_order = controller.submit(intent, gateway, broker)
    assert second_result.allowed is False
    assert REJECT_DUPLICATE in " ".join(second_result.reasons)
    assert second_order is None


# ---------------------------------------------------------------------
# FIRST_ORDER_TEST_MODE
# ---------------------------------------------------------------------


def test_first_order_auto_disarms():
    transport = FakeHttpTransport()
    queue_full_read_setup(transport, cash="1000.00", positions=[], ticker="ABC", price="20.00")
    queue_place_order_accepted(transport, order_id="o-first-1")
    queue_order_lookup(transport, history_orders=[_order_record("o-first-1", side=1, status=4, qty="1", cum_qty="1", price="20.00")])

    broker = make_paper_broker(transport, write_enabled=True)
    gateway = BrokerGateway()
    controller = PaperWriteController(write_enabled=True, first_order_test_mode=True)
    controller.arm()
    assert controller.state == STATE_ARMED

    result, order = controller.submit(forged_intent(dollar_amount=20.0, reference_price=20.0, audit_reference="audit-first-1"), gateway, broker)
    assert result.allowed is True
    assert order.status == "FILLED"
    assert controller.state == STATE_DISARMED  # auto-disarmed after the attempt


def test_second_order_after_auto_disarm_blocked():
    transport = FakeHttpTransport()
    queue_full_read_setup(transport, cash="1000.00", positions=[], ticker="ABC", price="20.00")
    queue_place_order_accepted(transport, order_id="o-first-2")
    queue_order_lookup(transport, history_orders=[_order_record("o-first-2", side=1, status=4, qty="1", cum_qty="1", price="20.00")])

    broker = make_paper_broker(transport, write_enabled=True)
    gateway = BrokerGateway()
    controller = PaperWriteController(write_enabled=True, first_order_test_mode=True)
    controller.arm()
    first_result, _ = controller.submit(forged_intent(dollar_amount=20.0, reference_price=20.0, audit_reference="audit-first-2"), gateway, broker)
    assert first_result.allowed is True

    place_calls_before = len([c for c in transport.calls if c[0] == "POST"])

    # A different (non-duplicate) intent, no re-arm -- must still be blocked.
    second_result, second_order = controller.submit(forged_intent(dollar_amount=20.0, reference_price=20.0, audit_reference="audit-first-2-b"), gateway, broker)
    assert second_result.allowed is False
    assert second_order is None
    place_calls_after = len([c for c in transport.calls if c[0] == "POST"])
    assert place_calls_after == place_calls_before  # no new HTTP write was attempted


def test_rearming_after_first_order_permits_a_new_attempt():
    transport = FakeHttpTransport()
    queue_full_read_setup(transport, cash="1000.00", positions=[], ticker="ABC", price="20.00")
    queue_place_order_accepted(transport, order_id="o-rearm-1")
    queue_order_lookup(transport, history_orders=[_order_record("o-rearm-1", side=1, status=4, qty="1", cum_qty="1", price="20.00")])

    broker = make_paper_broker(transport, write_enabled=True)
    gateway = BrokerGateway()
    controller = PaperWriteController(write_enabled=True, first_order_test_mode=True)
    controller.arm()
    controller.submit(forged_intent(dollar_amount=20.0, reference_price=20.0, audit_reference="audit-rearm-1"), gateway, broker)
    assert controller.state == STATE_DISARMED

    # Re-arm for a genuinely new order.
    queue_place_order_accepted(transport, order_id="o-rearm-2")
    queue_order_lookup(transport, history_orders=[_order_record("o-rearm-2", side=1, status=4, qty="1", cum_qty="1", price="20.00")])
    controller.arm()
    result, order = controller.submit(forged_intent(dollar_amount=20.0, reference_price=20.0, audit_reference="audit-rearm-2"), gateway, broker)
    assert result.allowed is True
    assert order.status == "FILLED"
    assert controller.state == STATE_DISARMED  # auto-disarmed again


def test_timeout_fails_closed():
    transport = FakeHttpTransport()
    queue_full_read_setup(transport, cash="1000.00", positions=[], ticker="ABC", price="20.00")
    transport.queue_post(OPEN_ORDERS_PATH_TEMPLATE.format(acc_id=DEFAULT_ACCOUNT_ID), TransportTimeout("timed out"))

    broker = make_paper_broker(transport, write_enabled=True)
    gateway = BrokerGateway()
    controller = PaperWriteController(write_enabled=True, first_order_test_mode=True)
    controller.arm()

    with pytest.raises(Exception):
        controller.submit(forged_intent(dollar_amount=20.0, reference_price=20.0, audit_reference="audit-timeout-1"), gateway, broker)

    # Fail closed: auto-disarmed by the uncertain result.
    assert controller.state == STATE_DISARMED


def test_no_retry_after_timeout_without_rearming():
    transport = FakeHttpTransport()
    queue_full_read_setup(transport, cash="1000.00", positions=[], ticker="ABC", price="20.00")
    transport.queue_post(OPEN_ORDERS_PATH_TEMPLATE.format(acc_id=DEFAULT_ACCOUNT_ID), TransportTimeout("timed out"))

    broker = make_paper_broker(transport, write_enabled=True)
    gateway = BrokerGateway()
    controller = PaperWriteController(write_enabled=True, first_order_test_mode=True)
    controller.arm()

    intent = forged_intent(dollar_amount=20.0, reference_price=20.0, audit_reference="audit-timeout-2")
    with pytest.raises(Exception):
        controller.submit(intent, gateway, broker)

    # Same audit_reference, no re-arm -- blocked both by DISARMED state
    # and by duplicate tracking (the uncertain attempt was consumed).
    result, order = controller.submit(intent, gateway, broker)
    assert result.allowed is False
    assert order is None


# ---------------------------------------------------------------------
# FIRST_TEST_MAX_NOTIONAL
# ---------------------------------------------------------------------


def test_order_above_first_test_cap_blocked():
    transport = FakeHttpTransport()
    queue_full_read_setup(transport, cash="1000.00", positions=[], ticker="ABC", price="20.00")
    broker = make_paper_broker(transport, write_enabled=True)
    gateway = BrokerGateway()
    controller = PaperWriteController(write_enabled=True, first_order_test_mode=True)
    controller.arm()

    result, order = controller.submit(forged_intent(dollar_amount=30.0, reference_price=20.0), gateway, broker)  # > $25 cap
    assert result.allowed is False
    assert "FIRST_TEST_MAX_NOTIONAL" in " ".join(result.reasons)
    assert order is None
    write_calls = [c for c in transport.calls if c[0] == "POST" and c[1] == OPEN_ORDERS_PATH_TEMPLATE.format(acc_id=DEFAULT_ACCOUNT_ID)]
    assert write_calls == []  # no write attempted (the quote lookup itself is a POST, but not to the order path)
    assert controller.state == STATE_ARMED  # a pre-broker-call rejection does not consume the single attempt


def test_whole_share_rounding_above_notional_blocked_not_rounded_up():
    transport = FakeHttpTransport()
    # $25 cap, quote $20 -> 1 share costs $20 (fits); but at quote $26,
    # even 1 whole share ($26) exceeds the $25 cap -- must reject, never
    # round up to "make it fit" and never buy at a size that wasn't authorized.
    queue_full_read_setup(transport, cash="1000.00", positions=[], ticker="ABC", price="26.00")
    broker = make_paper_broker(transport, write_enabled=True)
    gateway = BrokerGateway()
    controller = PaperWriteController(write_enabled=True, first_order_test_mode=True)
    controller.arm()

    result, order = controller.submit(forged_intent(dollar_amount=25.0, reference_price=26.0), gateway, broker)
    assert result.allowed is False
    assert "exceeds the $25" in " ".join(result.reasons) or "cap" in " ".join(result.reasons).lower()
    assert order is None


def test_default_first_test_max_notional_is_25_dollars():
    assert DEFAULT_FIRST_TEST_MAX_NOTIONAL == Decimal("25")
    controller = PaperWriteController()
    assert controller.first_test_max_notional == Decimal("25")


# ---------------------------------------------------------------------
# Fractional shares -- blocked unless verified
# ---------------------------------------------------------------------


def test_fractional_instrument_type_blocked():
    transport = FakeHttpTransport()
    broker = make_paper_broker(transport, write_enabled=True)
    gateway = BrokerGateway()
    controller = PaperWriteController(write_enabled=True)
    controller.arm()

    result, order = controller.submit(forged_intent(instrument_type="fractional_share"), gateway, broker)
    assert result.allowed is False
    assert "fractional" in " ".join(result.reasons).lower()
    assert order is None
    assert transport.calls == []


# ---------------------------------------------------------------------
# Type gate -- nothing but a real ExecutionIntent can arm/pass
# ---------------------------------------------------------------------


def test_raw_ai_text_cannot_arm_controller():
    transport = FakeHttpTransport()
    broker = make_paper_broker(transport, write_enabled=True)
    gateway = BrokerGateway()
    controller = PaperWriteController(write_enabled=True)
    controller.arm()

    result, order = controller.submit("APPROVED, buy 100 shares of ABC", gateway, broker)
    assert result.allowed is False
    assert order is None
    assert transport.calls == []


def test_trade_proposal_cannot_arm_controller():
    transport = FakeHttpTransport()
    broker = make_paper_broker(transport, write_enabled=True)
    gateway = BrokerGateway()
    controller = PaperWriteController(write_enabled=True)
    controller.arm()

    proposal = TradeProposal.from_dict(base_proposal())
    result, order = controller.submit(proposal, gateway, broker)
    assert result.allowed is False
    assert order is None
    assert transport.calls == []


def test_arbitrary_dict_cannot_arm_controller():
    transport = FakeHttpTransport()
    broker = make_paper_broker(transport, write_enabled=True)
    gateway = BrokerGateway()
    controller = PaperWriteController(write_enabled=True)
    controller.arm()

    result, order = controller.submit({"execution_allowed": True, "action": "BUY", "ticker": "ABC"}, gateway, broker)
    assert result.allowed is False
    assert order is None
    assert transport.calls == []


# ---------------------------------------------------------------------
# Item 11 -- full mock integration test
# ---------------------------------------------------------------------


def test_full_integration_A_valid_armed_paper_flow_permits_write():
    transport = FakeHttpTransport()
    queue_full_read_setup(transport, cash="300.00", positions=[], ticker="XYZ", price="10.00")
    queue_place_order_accepted(transport, order_id="moomoo-order-A")
    queue_order_lookup(transport, history_orders=[_order_record("moomoo-order-A", symbol="XYZ", side=1, status=4, qty="4", cum_qty="4", price="10.00")])

    broker = make_paper_broker(transport, write_enabled=True)
    gateway = BrokerGateway()
    controller = PaperWriteController(write_enabled=True)
    controller.arm()

    proposal = base_proposal()  # $45 at $10/share -> 4 whole shares, well within the 15% cap
    result = create_execution_intent(proposal, **account_state())
    assert result.created is True, result.reasons

    controller_result, order = controller.submit(result.intent, gateway, broker)
    assert controller_result.allowed is True, controller_result.reasons
    assert order.status == "FILLED"

    place_calls = [c for c in transport.calls if c[0] == "POST" and c[1] == OPEN_ORDERS_PATH_TEMPLATE.format(acc_id=DEFAULT_ACCOUNT_ID)]
    assert len(place_calls) == 1
    assert place_calls[0][2]["qty"] == "4"
    assert place_calls[0][2]["order_side"] == ORDER_SIDE_BUY


def test_full_integration_B_disarmed_controller_blocks_broker_write():
    transport = FakeHttpTransport()
    queue_full_read_setup(transport, cash="300.00", positions=[], ticker="XYZ", price="10.00")

    broker = make_paper_broker(transport, write_enabled=True)
    gateway = BrokerGateway()
    controller = PaperWriteController(write_enabled=True)  # never armed

    proposal = base_proposal()
    result = create_execution_intent(proposal, **account_state())
    assert result.created is True, result.reasons

    controller_result, order = controller.submit(result.intent, gateway, broker)
    assert controller_result.allowed is False
    assert order is None
    write_calls = [c for c in transport.calls if c[0] == "POST" and c[1] == OPEN_ORDERS_PATH_TEMPLATE.format(acc_id=DEFAULT_ACCOUNT_ID)]
    assert write_calls == []


def test_full_integration_C_second_order_blocked_after_first_test_auto_disarm():
    transport = FakeHttpTransport()
    queue_full_read_setup(transport, cash="300.00", positions=[], ticker="XYZ", price="10.00")
    queue_place_order_accepted(transport, order_id="moomoo-order-C1")
    queue_order_lookup(transport, history_orders=[_order_record("moomoo-order-C1", symbol="XYZ", side=1, status=4, qty="4", cum_qty="4", price="10.00")])

    broker = make_paper_broker(transport, write_enabled=True)
    gateway = BrokerGateway()
    controller = PaperWriteController(write_enabled=True, first_order_test_mode=True, first_test_max_notional=Decimal("50"))
    controller.arm()

    proposal = base_proposal()
    first_decision = create_execution_intent(proposal, **account_state())
    assert first_decision.created is True, first_decision.reasons

    first_controller_result, first_order = controller.submit(first_decision.intent, gateway, broker)
    assert first_controller_result.allowed is True
    assert first_order.status == "FILLED"
    assert controller.state == STATE_DISARMED

    # A second, distinct approved decision -- still blocked, no re-arm.
    second_decision = create_execution_intent(base_proposal(ticker="XYZ", proposed_dollar_amount=20.0, proposed_allocation_pct=6.7), **account_state())
    assert second_decision.created is True, second_decision.reasons

    place_calls_before = len([c for c in transport.calls if c[0] == "POST"])
    second_controller_result, second_order = controller.submit(second_decision.intent, gateway, broker)
    assert second_controller_result.allowed is False
    assert second_order is None
    place_calls_after = len([c for c in transport.calls if c[0] == "POST"])
    assert place_calls_after == place_calls_before
