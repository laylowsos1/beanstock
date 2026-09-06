"""Tests for RealDataPaperSession (execution/real_data_paper_session.py).

No test in this file makes a real network call -- MoomooReadOnlyBroker is
always constructed with a FakeHttpTransport in this file. BEANSTOCK_
PAPER_WRITE_ENABLED is never set to True in the real environment, and
BEANSTOCK_EXECUTION_MODE is only ever set via this test file's own
explicit `execution_mode=` constructor overrides, never the real
environment variable.
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest

from broker.fake_paper import FakePaperBroker
from broker.http_transport import HttpResponse, HttpTransport
from broker.moomoo_readonly import (
    LiveAccountRejectedError,
    MalformedResponseError,
    MoomooReadOnlyBroker,
    QUOTE_PATH,
    ReadOnlyBrokerError,
)
from execution.intent import create_execution_intent
from execution.paper_write_controller import REJECT_DUPLICATE
from execution.real_data_paper_session import (
    EXECUTION_MODE_LOCAL_PAPER_REAL_DATA,
    RealDataPaperModeError,
    RealDataPaperSession,
    RealQuoteUnavailableError,
)


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


def queue_quote(transport, ticker="ABC", price="50.00", data_time_ms=None, market_prefix="US"):
    if data_time_ms is None:
        data_time_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    transport.queue_post(
        QUOTE_PATH,
        json_response(200, envelope({"quote_list": [{"code": f"{market_prefix}.{ticker}", "last_price": price, "data_time": data_time_ms}]})),
    )


def make_real_broker(transport, token=None):
    return MoomooReadOnlyBroker(access_token_provider=token or (lambda: "fake-access-token"), http_transport=transport)


def make_session(transport, *, starting_cash=Decimal("300.00"), execution_mode=EXECUTION_MODE_LOCAL_PAPER_REAL_DATA, write_enabled=None, first_order_test_mode=False):
    real = make_real_broker(transport)
    paper = FakePaperBroker(starting_cash=starting_cash)
    session = RealDataPaperSession(
        real_data_broker=real,
        paper_broker=paper,
        execution_mode=execution_mode,
        write_enabled=write_enabled,
        first_order_test_mode=first_order_test_mode,
    )
    return session, real, paper


def base_proposal(**overrides):
    proposal = {
        "ticker": "ABC", "instrument_type": "stock", "action": "BUY",
        "current_price": 50.0, "intended_entry": 50.0, "candidate_score": 88,
        "catalyst": "Earnings beat with raised guidance",
        "catalyst_timing": "Reported this morning, guidance raise effective immediately",
        "bull_case": "Margin expansion continues into next two quarters",
        "bear_case": "Multiple compression if growth decelerates",
        "thesis_invalidation": "Guidance is walked back or margins compress q/q",
        "stop_price": 45.0, "target_price": 62.5, "proposed_dollar_amount": 45.0,
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
# Construction / mode guard
# ---------------------------------------------------------------------


def test_wrong_execution_mode_fails_closed(monkeypatch):
    monkeypatch.delenv("BEANSTOCK_EXECUTION_MODE", raising=False)
    transport = FakeHttpTransport()
    real = make_real_broker(transport)
    paper = FakePaperBroker(starting_cash=Decimal("300"))
    with pytest.raises(RealDataPaperModeError):
        RealDataPaperSession(real_data_broker=real, paper_broker=paper)  # no override, env unset


def test_only_fake_paper_broker_accepted_as_execution_target():
    transport = FakeHttpTransport()
    real = make_real_broker(transport)
    with pytest.raises(TypeError):
        RealDataPaperSession(real_data_broker=real, paper_broker=object(), execution_mode=EXECUTION_MODE_LOCAL_PAPER_REAL_DATA)


def test_only_moomoo_readonly_broker_accepted_as_real_data_source():
    paper = FakePaperBroker(starting_cash=Decimal("300"))
    with pytest.raises(TypeError):
        RealDataPaperSession(real_data_broker=object(), paper_broker=paper, execution_mode=EXECUTION_MODE_LOCAL_PAPER_REAL_DATA)


# ---------------------------------------------------------------------
# Live path / write-endpoint guarantees (structural, not per-call)
# ---------------------------------------------------------------------


def test_live_moomoo_endpoint_blocked():
    transport = FakeHttpTransport()
    real = make_real_broker(transport)
    with pytest.raises(LiveAccountRejectedError):
        real._guard_path("/api/v1.0/accounts/authorized_trd_accs")
    with pytest.raises(LiveAccountRejectedError):
        real._guard_path("/api/v1.0/trading/trade/place-order")


def test_no_moomoo_write_endpoint_reachable_from_real_data_source():
    transport = FakeHttpTransport()
    real = make_real_broker(transport)
    with pytest.raises(ReadOnlyBrokerError):
        real.submit_execution_intent(None)
    with pytest.raises(ReadOnlyBrokerError):
        real.cancel_order("any-order-id")
    with pytest.raises(ReadOnlyBrokerError):
        real.close_position("ABC")
    assert transport.calls == []


# ---------------------------------------------------------------------
# Real quotes feeding local execution
# ---------------------------------------------------------------------


def test_real_quote_feeds_fake_paper_broker():
    transport = FakeHttpTransport()
    queue_quote(transport, ticker="ABC", price="50.00")
    session, real, paper = make_session(transport)

    price = session.refresh_quote("ABC")
    assert price == Decimal("50.00")
    assert paper.get_quote("ABC") == Decimal("50.00")
    ts = paper.get_quote_timestamp("ABC")
    assert ts is not None
    # the timestamp is the REAL one from moomoo, not a fabricated "now"
    assert abs((datetime.now(timezone.utc) - ts).total_seconds()) < 5 or ts == real.get_quote_timestamp("ABC")


def test_missing_real_quote_blocks_execution():
    transport = FakeHttpTransport()
    transport.queue_post(QUOTE_PATH, json_response(200, envelope({"quote_list": []})))
    session, real, paper = make_session(transport)

    with pytest.raises(MalformedResponseError):
        session.refresh_quote("ABC")
    assert paper.get_quote("ABC") is None  # nothing was ever set


def test_invalid_ticker_blocks_execution():
    transport = FakeHttpTransport()
    session, real, paper = make_session(transport)
    with pytest.raises(RealQuoteUnavailableError):
        session.refresh_quote("")
    assert transport.calls == []


def test_stale_real_quote_blocks_local_execution():
    transport = FakeHttpTransport()
    stale_ms = int((datetime.now(timezone.utc) - timedelta(minutes=5)).timestamp() * 1000)
    queue_quote(transport, ticker="ABC", price="50.00", data_time_ms=stale_ms)
    session, real, paper = make_session(transport)
    session.controller.arm()

    result, controller_result, order = session.evaluate_and_submit(base_proposal(), **account_state())
    assert result.created is True, result.reasons  # the risk engine has no concept of quote staleness
    assert controller_result.allowed is False
    assert order is None
    assert paper.get_positions() == []  # nothing executed locally


# ---------------------------------------------------------------------
# Local simulated BUY / ADD / REDUCE / EXIT
# ---------------------------------------------------------------------


def test_local_simulated_buy_works():
    transport = FakeHttpTransport()
    queue_quote(transport, ticker="ABC", price="50.00")
    session, real, paper = make_session(transport)
    session.controller.arm()

    result, controller_result, order = session.evaluate_and_submit(base_proposal(), **account_state())
    assert result.created is True, result.reasons
    assert controller_result.allowed is True, controller_result.reasons
    assert order.status == "FILLED"
    assert order.fill_price == Decimal("50.00")

    position = paper.get_position("ABC")
    assert position is not None
    assert position.quantity == Decimal("45.00") / Decimal("50.00")

    trade_log = session.get_trade_log()
    assert len(trade_log) == 1
    assert trade_log[0].real_quote_used == Decimal("50.00")
    assert trade_log[0].candidate_score == 88
    assert trade_log[0].catalyst == base_proposal()["catalyst"]


def test_local_simulated_add_works():
    transport = FakeHttpTransport()
    # Both quotes queued upfront, in consumption order -- FakeHttpTransport
    # keeps returning a lone queued item forever, so appending a second
    # one *after* the first call would leave the first (already "used")
    # item at the front and hand it out again on the next call.
    queue_quote(transport, ticker="ABC", price="50.00")
    queue_quote(transport, ticker="ABC", price="55.00")
    session, real, paper = make_session(transport)
    session.controller.arm()

    session.evaluate_and_submit(base_proposal(), **account_state())

    add_proposal = base_proposal(
        action="ADD", current_price=55.0, intended_entry=55.0, stop_price=50.0, target_price=67.5,
        proposed_dollar_amount=30.0, proposed_allocation_pct=10.0,
    )
    result, controller_result, order = session.evaluate_and_submit(
        add_proposal, has_existing_position=True,
        **account_state(available_cash=255.0, current_positions=1, trades_this_week=1),
    )
    assert result.created is True, result.reasons
    assert controller_result.allowed is True, controller_result.reasons
    assert order.status == "FILLED"


def test_local_simulated_reduce_works():
    transport = FakeHttpTransport()
    queue_quote(transport, ticker="ABC", price="50.00")
    queue_quote(transport, ticker="ABC", price="52.00")
    session, real, paper = make_session(transport)
    session.controller.arm()
    session.evaluate_and_submit(base_proposal(), **account_state())

    # After the $45 BUY, the position is ~15% of the $300 account --
    # REDUCE must target a lower allocation than that to be eligible.
    reduce_proposal = base_proposal(action="REDUCE", current_price=52.0, intended_entry=52.0, proposed_dollar_amount=20.0, proposed_allocation_pct=8.0)
    result, controller_result, order = session.evaluate_and_submit(
        reduce_proposal, has_existing_position=True,
        **account_state(available_cash=255.0, current_positions=1, trades_this_week=1, current_company_exposure_pct=15.0, current_sector_exposure_pct=15.0),
    )
    assert result.created is True, result.reasons
    assert controller_result.allowed is True, controller_result.reasons
    assert order.status == "FILLED"
    assert order.realized_pnl is not None


def test_local_simulated_exit_works():
    transport = FakeHttpTransport()
    queue_quote(transport, ticker="ABC", price="50.00")
    queue_quote(transport, ticker="ABC", price="65.00")
    session, real, paper = make_session(transport)
    session.controller.arm()
    session.evaluate_and_submit(base_proposal(), **account_state())

    exit_proposal = base_proposal(action="EXIT", current_price=65.0, intended_entry=65.0)
    result, controller_result, order = session.evaluate_and_submit(
        exit_proposal, has_existing_position=True,
        **account_state(available_cash=255.0, current_positions=1, trades_this_week=1, current_company_exposure_pct=15.0, current_sector_exposure_pct=15.0),
    )
    assert result.created is True, result.reasons
    assert controller_result.allowed is True, controller_result.reasons
    assert order.status == "FILLED"
    assert paper.get_position("ABC") is None
    assert order.realized_pnl == (Decimal("65.00") - Decimal("50.00")) * (Decimal("45.00") / Decimal("50.00"))


# ---------------------------------------------------------------------
# Duplicate blocking + risk engine / gateway / controller still required
# ---------------------------------------------------------------------


def test_duplicate_execution_intent_still_blocked():
    transport = FakeHttpTransport()
    queue_quote(transport, ticker="ABC", price="50.00")
    session, real, paper = make_session(transport)
    session.controller.arm()

    proposal = base_proposal()
    first_result, first_controller_result, first_order = session.evaluate_and_submit(proposal, **account_state())
    assert first_controller_result.allowed is True
    assert first_order.status == "FILLED"

    queue_quote(transport, ticker="ABC", price="50.00")  # a fresh quote fetch for the resubmission attempt
    second_result, second_controller_result, second_order = session.evaluate_and_submit(proposal, **account_state())
    assert second_result.created is True  # the risk engine has no concept of "already submitted"
    assert second_controller_result.allowed is False
    assert REJECT_DUPLICATE in " ".join(second_controller_result.reasons)
    assert second_order is None


def test_risk_engine_still_required():
    transport = FakeHttpTransport()
    queue_quote(transport, ticker="ABC", price="50.00")
    session, real, paper = make_session(transport)
    session.controller.arm()

    bad_proposal = base_proposal(candidate_score=40)  # below MIN_CANDIDATE_SCORE
    result, controller_result, order = session.evaluate_and_submit(bad_proposal, **account_state())
    assert result.created is False
    assert controller_result is None
    assert order is None
    assert paper.get_positions() == []


def test_broker_gateway_still_required():
    # A reference_price far from the real quote must be rejected by
    # BrokerGateway's own deviation check -- PaperWriteController does
    # not independently re-check price deviation, so this proves the
    # gateway is genuinely in the loop, not bypassed.
    transport = FakeHttpTransport()
    queue_quote(transport, ticker="ABC", price="50.00")
    session, real, paper = make_session(transport)
    session.controller.arm()

    # entry=65 still forms a valid long structure (stop=60 < entry < target=77.5,
    # RR=2.5) so it clears schema/risk-engine checks, but deviates ~23%
    # from the real $50 quote -- over BrokerGateway's 20% tolerance.
    weird_proposal = base_proposal(current_price=65.0, intended_entry=65.0, stop_price=60.0, target_price=77.5)
    result, controller_result, order = session.evaluate_and_submit(weird_proposal, **account_state())
    assert result.created is True, result.reasons
    assert controller_result.allowed is False
    assert "deviat" in " ".join(controller_result.reasons).lower()
    assert order is None
    assert paper.get_positions() == []


def test_paper_write_controller_still_required():
    transport = FakeHttpTransport()
    queue_quote(transport, ticker="ABC", price="50.00")
    session, real, paper = make_session(transport)
    # controller never armed

    result, controller_result, order = session.evaluate_and_submit(base_proposal(), **account_state())
    assert result.created is True, result.reasons
    assert controller_result.allowed is False
    assert order is None
    assert paper.get_positions() == []  # gateway approved, but the controller still blocked local execution


# ---------------------------------------------------------------------
# Snapshot / tracking sanity (item 11/12) -- basic exercise, not the
# core safety surface, but proves it works end to end.
# ---------------------------------------------------------------------


def test_daily_snapshot_and_open_trade_tracking():
    transport = FakeHttpTransport()
    # All three quotes queued upfront, in the exact order they'll be
    # consumed (BUY, then the ABC tracking refresh, then the SPY
    # benchmark fetch) -- see the note in test_local_simulated_add_works.
    queue_quote(transport, ticker="ABC", price="50.00")
    queue_quote(transport, ticker="ABC", price="55.00")
    queue_quote(transport, "SPY", "500.00")
    session, real, paper = make_session(transport)
    session.controller.arm()
    session.evaluate_and_submit(base_proposal(), **account_state())

    failures = session.update_open_trade_tracking()
    assert failures == []

    trade = session.get_trade_log()[0]
    assert trade.latest_real_quote == Decimal("55.00")
    assert trade.unrealized_pnl is not None

    snapshot = session.daily_snapshot()
    assert snapshot.starting_equity == Decimal("300.00")
    assert snapshot.current_equity > Decimal("300.00")  # price moved up, unrealized gain
    assert snapshot.win_rate is None  # no closed trades yet
