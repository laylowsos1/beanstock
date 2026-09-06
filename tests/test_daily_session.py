"""Tests for DailySessionRunner (runner/daily_session.py).

No test in this file makes a real network call -- MoomooReadOnlyBroker
is always constructed with a FakeHttpTransport. BEANSTOCK_PAPER_WRITE_
ENABLED is never set to True in the real environment; BEANSTOCK_
EXECUTION_MODE is only ever set via explicit `execution_mode=` overrides.
Memory files are always temp files under pytest's tmp_path -- no test
touches this project's real memory/*.md files.
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest

from broker.fake_paper import FakePaperBroker
from broker.http_transport import HttpResponse, HttpTransport
from broker.moomoo_readonly import LiveAccountRejectedError, MARKET_STATE_PATH, MoomooReadOnlyBroker, QUOTE_PATH, ReadOnlyBrokerError
from execution.real_data_paper_session import EXECUTION_MODE_LOCAL_PAPER_REAL_DATA
from runner.daily_session import DailySessionRunner, DEFAULT_MEMORY_FILENAMES


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
        json_response(200, envelope({"quote_list": [{"code": f"{market_prefix}.{ticker}", "last_price": price, "prev_close_price": price, "data_time": data_time_ms}]})),
    )


def queue_market_state(transport, ticker="SPY", state="MARKET_OPEN"):
    transport.queue_post(MARKET_STATE_PATH, json_response(200, envelope({"market_state_list": [{"code": f"US.{ticker}", "market_state": state}]})))


def queue_market_context_defaults(transport, ticker="ABC", price="50.00", data_time_ms=None):
    """Every DailySessionRunner.run() call reaches gather_market_context(),
    which hits QUOTE_PATH many times (benchmark, VIX attempt, sector
    ETFs, held positions) and MARKET_STATE_PATH once. FakeHttpTransport
    only keys by path, so ONE queued response per path is reused
    ("sticky") for every call to it -- fine here since these tests don't
    care about per-ticker distinction in the market-context data.
    """
    queue_quote(transport, ticker=ticker, price=price, data_time_ms=data_time_ms)
    queue_market_state(transport)


def make_real_broker(transport, token=None):
    return MoomooReadOnlyBroker(access_token_provider=token or (lambda: "fake-access-token"), http_transport=transport)


def make_memory_files(tmp_path: Path) -> dict:
    paths = {}
    for name in DEFAULT_MEMORY_FILENAMES:
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"# {name}\n", encoding="utf-8")
    return {name: tmp_path / name for name in DEFAULT_MEMORY_FILENAMES}


def make_runner(transport, tmp_path, *, starting_cash=Decimal("300.00"), execution_mode=EXECUTION_MODE_LOCAL_PAPER_REAL_DATA, write_enabled=None):
    real = make_real_broker(transport)
    paper = FakePaperBroker(starting_cash=starting_cash)
    return DailySessionRunner(
        real_data_broker=real,
        paper_broker=paper,
        execution_mode=execution_mode,
        write_enabled=write_enabled,
        memory_paths=make_memory_files(tmp_path),
    )


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


# ---------------------------------------------------------------------
# Startup checks
# ---------------------------------------------------------------------


def test_startup_check_passes_with_valid_configuration(tmp_path):
    transport = FakeHttpTransport()
    runner = make_runner(transport, tmp_path)
    result = runner.run_startup_checks()
    assert result.passed is True, result.reasons
    assert result.checks["live_endpoints_blocked"][0] is True
    assert result.checks["moomoo_writes_unavailable"][0] is True
    assert result.checks["memory_files_readable"][0] is True


def test_startup_check_fails_closed_on_wrong_execution_mode(tmp_path, monkeypatch):
    monkeypatch.delenv("BEANSTOCK_EXECUTION_MODE", raising=False)
    transport = FakeHttpTransport()
    runner = make_runner(transport, tmp_path, execution_mode=None)  # no override, env unset
    result = runner.run_startup_checks()
    assert result.passed is False
    assert runner.session is None


def test_startup_check_fails_closed_on_missing_memory_file(tmp_path):
    transport = FakeHttpTransport()
    real = make_real_broker(transport)
    paper = FakePaperBroker(starting_cash=Decimal("300"))
    memory_paths = make_memory_files(tmp_path)
    missing = memory_paths["memory/TRADING-STRATEGY.md"]
    missing.unlink()  # simulate an unreadable/missing strategy file
    runner = DailySessionRunner(
        real_data_broker=real, paper_broker=paper,
        execution_mode=EXECUTION_MODE_LOCAL_PAPER_REAL_DATA, memory_paths=memory_paths,
    )
    result = runner.run_startup_checks()
    assert result.passed is False
    assert result.checks["memory_files_readable"][0] is False


def test_live_endpoint_blocked_directly():
    transport = FakeHttpTransport()
    real = make_real_broker(transport)
    with pytest.raises(LiveAccountRejectedError):
        real._guard_path("/api/v1.0/accounts/authorized_trd_accs")


def test_moomoo_write_impossible_directly():
    transport = FakeHttpTransport()
    real = make_real_broker(transport)
    with pytest.raises(ReadOnlyBrokerError):
        real.cancel_order("anything")
    with pytest.raises(ReadOnlyBrokerError):
        real.submit_execution_intent(None)
    with pytest.raises(ReadOnlyBrokerError):
        real.close_position("ABC")
    assert transport.calls == []


# ---------------------------------------------------------------------
# Candidate routing
# ---------------------------------------------------------------------


def test_stale_market_data_blocks_trade(tmp_path):
    transport = FakeHttpTransport()
    stale_ms = int((datetime.now(timezone.utc) - timedelta(minutes=5)).timestamp() * 1000)
    queue_quote(transport, ticker="ABC", price="50.00", data_time_ms=stale_ms)
    queue_market_state(transport)
    runner = make_runner(transport, tmp_path)
    runner.run_startup_checks()
    runner.session.controller.arm()

    result = runner.run(candidate_proposals=[base_proposal()])
    assert result.final_action in ("NO_QUALIFYING_CANDIDATE",)
    assert result.trades_executed == []
    assert len(result.trades_rejected) == 1
    assert result.trades_rejected[0].stage == "gateway_or_controller_rejected"


def test_candidate_under_75_rejected(tmp_path):
    transport = FakeHttpTransport()
    queue_market_context_defaults(transport, ticker="ABC", price="50.00")
    runner = make_runner(transport, tmp_path)
    runner.run_startup_checks()
    runner.session.controller.arm()

    result = runner.run(candidate_proposals=[base_proposal(candidate_score=60)])
    assert result.trades_executed == []
    assert len(result.trades_rejected) == 1
    assert result.trades_rejected[0].stage == "schema_or_risk_rejected"
    assert "candidate_score" in " ".join(result.trades_rejected[0].reasons)


def test_valid_candidate_reaches_local_fake_paper_broker(tmp_path):
    transport = FakeHttpTransport()
    queue_market_context_defaults(transport, ticker="ABC", price="50.00")
    runner = make_runner(transport, tmp_path)
    runner.run_startup_checks()
    runner.session.controller.arm()

    result = runner.run(candidate_proposals=[base_proposal()])
    assert result.final_action == "TRADED"
    assert len(result.trades_executed) == 1
    order = result.trades_executed[0]
    assert order.status == "FILLED"
    position = runner._paper_broker.get_position("ABC")
    assert position is not None


def test_failed_risk_rule_blocks_execution(tmp_path):
    transport = FakeHttpTransport()
    queue_market_context_defaults(transport, ticker="ABC", price="50.00")
    runner = make_runner(transport, tmp_path)
    runner.run_startup_checks()
    runner.session.controller.arm()

    # reward:risk well below the 2.0 minimum
    bad_proposal = base_proposal(stop_price=48.0, target_price=51.0)
    result = runner.run(candidate_proposals=[bad_proposal])
    assert result.trades_executed == []
    assert result.trades_rejected[0].stage == "schema_or_risk_rejected"
    assert runner._paper_broker.get_positions() == []


def test_rejected_candidate_never_becomes_executed_trade(tmp_path):
    transport = FakeHttpTransport()
    queue_market_context_defaults(transport, ticker="ABC", price="50.00")
    runner = make_runner(transport, tmp_path)
    runner.run_startup_checks()
    # controller never armed -- gateway approves, controller blocks
    result = runner.run(candidate_proposals=[base_proposal()])
    assert result.trades_executed == []
    assert len(result.trades_rejected) == 1
    assert all(entry.startswith(f"## {result.session_id} — REJECTED") for entry in result.trade_log_entries)
    assert runner._paper_broker.get_positions() == []


# ---------------------------------------------------------------------
# DO NOTHING / snapshot / logging
# ---------------------------------------------------------------------


def test_do_nothing_session_records_correctly(tmp_path):
    transport = FakeHttpTransport()
    queue_market_context_defaults(transport)
    runner = make_runner(transport, tmp_path)
    runner.run_startup_checks()
    runner.session.controller.arm()

    result = runner.run(candidate_proposals=None)
    assert result.final_action == "DO_NOTHING"
    assert result.trades_executed == []
    assert result.trades_rejected == []
    assert result.snapshot is not None
    assert "DO_NOTHING" in result.research_log_entry


def test_executed_local_trade_records_correctly(tmp_path):
    transport = FakeHttpTransport()
    queue_market_context_defaults(transport, ticker="ABC", price="50.00")
    runner = make_runner(transport, tmp_path)
    runner.run_startup_checks()
    runner.session.controller.arm()

    result = runner.run(candidate_proposals=[base_proposal()])
    assert len(result.trade_log_entries) == 1
    entry = result.trade_log_entries[0]
    assert "EXECUTED" in entry
    assert "REJECTED" not in entry
    assert "ABC" in entry


def test_daily_snapshot_persists_correctly(tmp_path):
    transport = FakeHttpTransport()
    queue_market_context_defaults(transport)
    runner = make_runner(transport, tmp_path)
    runner.run_startup_checks()
    runner.session.controller.arm()
    result = runner.run(candidate_proposals=None)

    runner.persist_logs(result)

    snapshot_text = runner.memory_paths["memory/DAILY-SNAPSHOTS.md"].read_text(encoding="utf-8")
    assert f"## {result.session_id}" in snapshot_text
    assert "Starting equity" in snapshot_text

    research_text = runner.memory_paths["memory/RESEARCH-LOG.md"].read_text(encoding="utf-8")
    assert result.session_id in research_text


# ---------------------------------------------------------------------
# Duplicate-session protection
# ---------------------------------------------------------------------


def test_repeated_runner_invocation_cannot_duplicate_session(tmp_path):
    transport = FakeHttpTransport()
    queue_market_context_defaults(transport, ticker="ABC", price="50.00")
    memory_paths = make_memory_files(tmp_path)

    real1 = make_real_broker(transport)
    paper1 = FakePaperBroker(starting_cash=Decimal("300"))
    runner1 = DailySessionRunner(
        real_data_broker=real1, paper_broker=paper1,
        execution_mode=EXECUTION_MODE_LOCAL_PAPER_REAL_DATA, memory_paths=memory_paths,
    )
    runner1.run_startup_checks()
    runner1.session.controller.arm()
    first_result = runner1.run(session_id="2026-09-08", candidate_proposals=[base_proposal()])
    assert first_result.final_action == "TRADED"
    runner1.persist_logs(first_result)

    # A fresh runner instance (simulating a second, separate script
    # invocation) pointed at the SAME persisted memory files.
    transport2 = FakeHttpTransport()  # no quote queued -- a real run would need one; it must never get this far
    real2 = make_real_broker(transport2)
    paper2 = FakePaperBroker(starting_cash=Decimal("300"))
    runner2 = DailySessionRunner(
        real_data_broker=real2, paper_broker=paper2,
        execution_mode=EXECUTION_MODE_LOCAL_PAPER_REAL_DATA, memory_paths=memory_paths,
    )
    runner2.run_startup_checks()
    runner2.session.controller.arm()
    second_result = runner2.run(session_id="2026-09-08", candidate_proposals=[base_proposal()])

    assert second_result.already_ran is True
    assert second_result.final_action == "ALREADY_RAN"
    assert second_result.trades_executed == []
    assert transport2.calls == []  # never even attempted a real read for this duplicate session
    assert paper2.get_positions() == []
