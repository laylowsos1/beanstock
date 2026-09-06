"""Beanstock REAL-DATA PAPER DAILY RUNNER.

One repeatable orchestrator for Beanstock's daily paper-trading
lifecycle against current real moomoo data, execution always local-only
(broker.fake_paper.FakePaperBroker). This module wraps
execution.real_data_paper_session.RealDataPaperSession with the
lifecycle around it: a startup safety check, memory loading, local
account/portfolio state, real market-data gathering, candidate routing,
a mechanical (non-AI) stop-loss check, the daily snapshot, and
RESEARCH-LOG/TRADE-LOG entry rendering.

This module does NOT invent trade ideas, catalysts, or scores.
Candidate research is supplied by the caller as TradeProposal dicts --
exactly like the "Claude research" step in Beanstock's architecture
diagram sits upstream of TradeProposal. Calling run() with no supplied
candidates is a valid, safe DO NOTHING outcome, not an error.

Startup safety check (fail closed)
-------------------------------------
run_startup_checks() verifies, before anything else runs:
  - execution mode is LOCAL_PAPER_REAL_DATA and both the real-data and
    execution brokers are the correct types (delegated to
    RealDataPaperSession's own constructor, which already enforces this)
  - live-account endpoints are structurally rejected (a real call to
    _guard_path() against a real documented live path)
  - moomoo write methods are structurally unavailable (a real call to
    cancel_order(), which unconditionally raises ReadOnlyBrokerError)
  - BrokerGateway and PaperWriteController are constructed and active
  - every required strategy/memory file is present and readable
If any check fails, run() returns immediately with
final_action="STARTUP_CHECK_FAILED" and touches nothing else -- no
market data call, no pipeline call, no log rendering.

Duplicate-session protection
-------------------------------
There is no database in this project -- the only durable signal across
separate process invocations of this runner is whatever has already
been written to memory/DAILY-SNAPSHOTS.md. Before evaluating any
candidate, run() checks whether a "## {session_id}" heading already
exists there (session_id defaults to today's UTC date) and, if so,
returns immediately with already_ran=True, touching neither the real
broker nor FakePaperBroker. This protection is only as good as whether
the previous run's result was actually persisted via persist_logs() --
an unpersisted run() leaves no marker, by design (see persist_logs()'s
own docstring for why persistence is a separate, explicit step).

Git is never touched by this module. persist_logs() writes to the
memory/*.md files on disk; committing/pushing that remains a fully
separate, explicit step for the operator.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Optional

from broker.base import Order
from broker.fake_paper import FakePaperBroker
from broker.gateway import BrokerGateway
from broker.moomoo_readonly import LiveAccountRejectedError, MoomooBrokerError, MoomooReadOnlyBroker, ReadOnlyBrokerError
from execution.paper_write_controller import PaperWriteController
from execution.real_data_paper_session import (
    EXECUTION_MODE_LOCAL_PAPER_REAL_DATA,
    RealDataPaperModeError,
    RealDataPaperSession,
    RealQuoteUnavailableError,
)

DEFAULT_SECTOR_TICKERS = ("XLK", "XLF", "XLE", "XLV", "XLY", "XLI", "XLU", "XLB")

DEFAULT_MEMORY_FILENAMES = (
    "CLAUDE.md",
    "memory/TRADING-STRATEGY.md",
    "memory/PROJECT-CONTEXT.md",
    "memory/TRADE-LOG.md",
    "memory/RESEARCH-LOG.md",
    "memory/DAILY-SNAPSHOTS.md",
    "memory/WEEKLY-REVIEW.md",
)


def default_memory_paths(repo_root: Optional[str] = None) -> dict:
    root = Path(repo_root) if repo_root else Path(__file__).resolve().parent.parent
    return {name: root / name for name in DEFAULT_MEMORY_FILENAMES}


def _within_last_days(iso_timestamp: Optional[str], days: int, now: datetime) -> bool:
    if not iso_timestamp:
        return False
    try:
        moment = datetime.fromisoformat(iso_timestamp)
    except (TypeError, ValueError):
        return False
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return (now - moment) <= timedelta(days=days)


@dataclass
class StartupCheckResult:
    passed: bool
    checks: dict  # name -> (passed: bool, detail: str)
    reasons: list


@dataclass
class CandidateOutcome:
    ticker: Optional[str]
    action: Optional[str]
    score: Optional[float]
    created: bool  # did an ExecutionIntent get created (schema + risk engine passed)?
    controller_allowed: Optional[bool]  # None if never reached the controller
    order: Optional[Order]
    reasons: list
    stage: str  # "quote_unavailable" | "schema_or_risk_rejected" | "gateway_or_controller_rejected" | "executed"


@dataclass
class DailySessionResult:
    timestamp: str
    session_id: str
    startup_check: StartupCheckResult
    market_context: dict
    account_state: dict
    candidate_outcomes: list
    position_outcomes: list
    trades_executed: list
    trades_rejected: list
    top_candidate: Optional[str]
    top_score: Optional[float]
    final_action: str  # DO_NOTHING | NO_QUALIFYING_CANDIDATE | TRADED | STARTUP_CHECK_FAILED | ALREADY_RAN
    snapshot: Optional[object]  # execution.real_data_paper_session.DailyPortfolioSnapshot
    research_log_entry: str
    trade_log_entries: list
    real_moomoo_write_used: bool
    live_account_data_used: bool
    local_paper_execution_only: bool
    already_ran: bool = False


class DailySessionRunner:
    def __init__(
        self,
        *,
        real_data_broker: MoomooReadOnlyBroker,
        paper_broker: FakePaperBroker,
        execution_mode: Optional[str] = None,
        write_enabled: Optional[bool] = None,
        sector_tickers=DEFAULT_SECTOR_TICKERS,
        benchmark_ticker: str = "SPY",
        memory_paths: Optional[dict] = None,
        max_intent_age_seconds: float = 300.0,
        max_quote_age_seconds: float = 60.0,
        repo_root: Optional[str] = None,
    ):
        # Construction never touches the network or raises on a bad mode
        # itself -- that all happens inside run_startup_checks(), so a
        # bad configuration produces a clean StartupCheckResult instead
        # of an exception from __init__.
        self._real_data_broker = real_data_broker
        self._paper_broker = paper_broker
        self._execution_mode = execution_mode
        self._write_enabled = write_enabled
        self.sector_tickers = tuple(sector_tickers)
        self.benchmark_ticker = benchmark_ticker.strip().upper()
        self.memory_paths = memory_paths or default_memory_paths(repo_root)
        self.max_intent_age_seconds = max_intent_age_seconds
        self.max_quote_age_seconds = max_quote_age_seconds

        self.session: Optional[RealDataPaperSession] = None
        self._memory_texts: dict = {}

    # -----------------------------------------------------------------
    # 1. Startup safety check
    # -----------------------------------------------------------------

    def run_startup_checks(self) -> StartupCheckResult:
        checks = {}

        def record(name, ok, detail):
            checks[name] = (bool(ok), str(detail))

        if self.session is not None:
            # Idempotent: reuse the already-constructed session rather
            # than rebuild it -- rebuilding would silently discard any
            # arm()/safe_mode/etc. state the caller already set on its
            # controller. run() relies on this so calling it after an
            # explicit run_startup_checks() + arm() doesn't reset arming.
            session = self.session
            record("execution_mode_and_targets", True, "reusing already-constructed session")
        else:
            try:
                session = RealDataPaperSession(
                    real_data_broker=self._real_data_broker,
                    paper_broker=self._paper_broker,
                    execution_mode=self._execution_mode,
                    write_enabled=self._write_enabled,
                    max_intent_age_seconds=self.max_intent_age_seconds,
                    max_quote_age_seconds=self.max_quote_age_seconds,
                    benchmark_ticker=self.benchmark_ticker,
                )
            except (RealDataPaperModeError, TypeError) as exc:
                record("execution_mode_and_targets", False, exc)
                return self._finalize_startup(checks)
            record(
                "execution_mode_and_targets",
                True,
                f"mode={EXECUTION_MODE_LOCAL_PAPER_REAL_DATA}, real_data_broker={type(self._real_data_broker).__name__}, "
                f"paper_broker={type(self._paper_broker).__name__}",
            )
            self.session = session

        try:
            self._real_data_broker._guard_path("/api/v1.0/accounts/authorized_trd_accs")
            record("live_endpoints_blocked", False, "a live-account path was NOT rejected")
        except LiveAccountRejectedError:
            record("live_endpoints_blocked", True, "live-account path correctly rejected")
        except Exception as exc:
            record("live_endpoints_blocked", False, f"unexpected error: {type(exc).__name__}")

        try:
            self._real_data_broker.cancel_order("startup-check-noop")
            record("moomoo_writes_unavailable", False, "cancel_order() did not raise")
        except ReadOnlyBrokerError:
            record("moomoo_writes_unavailable", True, "write methods correctly raise ReadOnlyBrokerError")
        except Exception as exc:
            record("moomoo_writes_unavailable", False, f"unexpected error: {type(exc).__name__}")

        record("broker_gateway_active", isinstance(session.gateway, BrokerGateway), type(session.gateway).__name__)
        record(
            "paper_write_controller_active",
            isinstance(session.controller, PaperWriteController),
            f"state={session.controller.state}",
        )

        missing = []
        for name, path in self.memory_paths.items():
            try:
                self._memory_texts[name] = Path(path).read_text(encoding="utf-8")
            except OSError as exc:
                missing.append(f"{name} ({exc.__class__.__name__})")
        record("memory_files_readable", not missing, "all readable" if not missing else "missing/unreadable: " + ", ".join(missing))

        return self._finalize_startup(checks)

    def _finalize_startup(self, checks: dict) -> StartupCheckResult:
        passed = all(ok for ok, _ in checks.values())
        reasons = [f"{name}: {detail}" for name, (ok, detail) in checks.items() if not ok]
        return StartupCheckResult(passed=passed, checks=checks, reasons=reasons)

    # -----------------------------------------------------------------
    # 3. Account / portfolio state
    # -----------------------------------------------------------------

    def gather_account_state(self) -> dict:
        account = self._paper_broker.get_account()
        positions = self._paper_broker.get_positions()
        trade_log = self.session.get_trade_log() if self.session else []
        now = datetime.now(timezone.utc)

        trades_this_week = sum(1 for t in trade_log if _within_last_days(t.opened_at, 7, now))

        company_exposure_pct = {}
        sector_exposure_pct: dict = {}
        for pos in positions:
            pct = float(pos.market_value / account.equity * 100) if account.equity else 0.0
            company_exposure_pct[pos.ticker] = pct
            sector = self._sector_for_ticker(pos.ticker)
            if sector:
                sector_exposure_pct[sector] = sector_exposure_pct.get(sector, 0.0) + pct

        realized_pnl = sum((t.realized_pnl for t in trade_log if t.realized_pnl is not None), Decimal("0"))
        unrealized_pnl = sum((p.unrealized_pnl for p in positions), Decimal("0"))

        return {
            "starting_equity": self.session.starting_equity if self.session else None,
            "equity": account.equity,
            "cash": account.cash,
            "positions": positions,
            "realized_pnl": realized_pnl,
            "unrealized_pnl": unrealized_pnl,
            "trades_this_week": trades_this_week,
            "company_exposure_pct": company_exposure_pct,
            "sector_exposure_pct": sector_exposure_pct,
            # Not auto-derived in this build (would need dedicated
            # sector-level consecutive-failure tracking) -- callers may
            # override via run(account_state_overrides={"blocked_sectors": [...]})
            "blocked_sectors": [],
        }

    def _sector_for_ticker(self, ticker: str) -> Optional[str]:
        if self.session is None:
            return None
        for record in reversed(self.session.get_trade_log()):
            if record.ticker == ticker and record.action in ("BUY", "ADD") and record.status == "FILLED" and record.sector:
                return record.sector
        return None

    def _stop_for_ticker(self, ticker: str) -> Optional[Decimal]:
        if self.session is None:
            return None
        for record in reversed(self.session.get_trade_log()):
            if record.ticker == ticker and record.action in ("BUY", "ADD") and record.status == "FILLED" and record.stop_price is not None:
                return record.stop_price
        return None

    def _account_kwargs(self, account_state: dict, proposal=None) -> dict:
        """Company/sector exposure are looked up for the SPECIFIC ticker
        (and the proposal's own stated sector) being evaluated -- never
        a blanket 0.0, which would silently let an over-concentrated ADD
        through or wrongly block a legitimate REDUCE (REDUCE requires
        proposed_allocation_pct < current exposure; a wrong 0.0 there
        blocks every real REDUCE).
        """
        ticker = None
        sector = None
        if proposal is not None:
            ticker = proposal.get("ticker") if isinstance(proposal, dict) else getattr(proposal, "ticker", None)
            sector = proposal.get("sector") if isinstance(proposal, dict) else getattr(proposal, "sector", None)
            if isinstance(ticker, str):
                ticker = ticker.strip().upper()

        company_pct = account_state["company_exposure_pct"].get(ticker, 0.0) if ticker else 0.0
        sector_pct = account_state["sector_exposure_pct"].get(sector, 0.0) if sector else 0.0

        return dict(
            account_equity=float(account_state["equity"]),
            available_cash=float(account_state["cash"]),
            current_positions=len(account_state["positions"]),
            trades_this_week=account_state["trades_this_week"],
            current_company_exposure_pct=company_pct,
            current_sector_exposure_pct=sector_pct,
            blocked_sectors=account_state["blocked_sectors"],
            account_mode="PAPER",
        )

    # -----------------------------------------------------------------
    # 4. Real market data (never invented)
    # -----------------------------------------------------------------

    def gather_market_context(self) -> dict:
        context: dict = {"benchmark_ticker": self.benchmark_ticker}

        try:
            context["benchmark_quote"] = self._real_data_broker.get_quote(self.benchmark_ticker)
            context["benchmark_quote_timestamp"] = self._real_data_broker.get_quote_timestamp(self.benchmark_ticker)
            context["benchmark_change_pct"] = self._real_data_broker.get_daily_change_pct(self.benchmark_ticker)
        except MoomooBrokerError as exc:
            context["benchmark_quote"] = None
            context["benchmark_quote_timestamp"] = None
            context["benchmark_change_pct"] = None
            context["benchmark_error"] = f"{type(exc).__name__}: {exc}"

        try:
            context["market_state"] = self._real_data_broker.get_market_status()
        except MoomooBrokerError as exc:
            context["market_state"] = None
            context["market_state_error"] = f"{type(exc).__name__}: {exc}"

        context["vix_available"] = False
        context["vix_quote"] = None
        try:
            vix_price = self._real_data_broker.get_quote("VIX")
            if vix_price is not None:
                context["vix_quote"] = vix_price
                context["vix_available"] = True
        except MoomooBrokerError:
            pass  # genuinely unavailable -- never invented, never fatal

        sector_changes = {}
        for ticker in self.sector_tickers:
            try:
                sector_changes[ticker] = self._real_data_broker.get_daily_change_pct(ticker)
            except MoomooBrokerError:
                sector_changes[ticker] = None
        context["sector_changes_pct"] = sector_changes

        held_quotes = {}
        for pos in self._paper_broker.get_positions():
            try:
                held_quotes[pos.ticker] = self._real_data_broker.get_quote(pos.ticker)
            except MoomooBrokerError:
                held_quotes[pos.ticker] = None
        context["held_position_quotes"] = held_quotes

        return context

    # -----------------------------------------------------------------
    # 5/6. Candidate research routing -- TradeProposal -> ... -> FakePaperBroker
    # -----------------------------------------------------------------

    def evaluate_candidates(self, candidate_proposals, account_state: dict) -> list:
        return [self._evaluate_one(proposal, account_state) for proposal in (candidate_proposals or [])]

    def _evaluate_one(self, proposal, account_state: dict) -> CandidateOutcome:
        ticker = proposal.get("ticker") if isinstance(proposal, dict) else getattr(proposal, "ticker", None)
        action = proposal.get("action") if isinstance(proposal, dict) else getattr(proposal, "action", None)
        score = proposal.get("candidate_score") if isinstance(proposal, dict) else getattr(proposal, "candidate_score", None)
        account_kwargs = self._account_kwargs(account_state, proposal)

        try:
            result, controller_result, order = self.session.evaluate_and_submit(proposal, **account_kwargs)
        except RealQuoteUnavailableError as exc:
            return CandidateOutcome(ticker, action, score, False, None, None, [f"Real quote unavailable: {exc}"], "quote_unavailable")

        if not result.created:
            return CandidateOutcome(ticker, action, score, False, None, None, result.reasons, "schema_or_risk_rejected")

        if not controller_result.allowed:
            return CandidateOutcome(ticker, action, score, True, False, None, controller_result.reasons, "gateway_or_controller_rejected")

        return CandidateOutcome(ticker, action, score, True, True, order, [], "executed")

    # -----------------------------------------------------------------
    # 7. Position management -- mechanical stop check + caller-supplied
    #    thesis-driven reviews
    # -----------------------------------------------------------------

    def check_mechanical_stops(self) -> list:
        """Deterministic, non-AI: compares a fresh real quote against
        the stop recorded when each held position was opened. Never
        invents a stop -- a ticker with no recorded stop is skipped.
        """
        triggers = []
        for pos in self._paper_broker.get_positions():
            stop = self._stop_for_ticker(pos.ticker)
            if stop is None:
                continue
            try:
                quote = self._real_data_broker.get_quote(pos.ticker)
            except MoomooBrokerError:
                continue
            if quote is not None and quote <= stop:
                triggers.append((pos.ticker, quote))
        return triggers

    def build_mechanical_exit_proposal(self, ticker: str, current_price: Decimal) -> dict:
        price = float(current_price)
        return {
            "ticker": ticker,
            "instrument_type": "stock",
            "action": "EXIT",
            "current_price": price,
            "intended_entry": price,
            "candidate_score": 100,
            "catalyst": "Mechanical stop-loss trigger: real price at or below the recorded stop",
            "catalyst_timing": "Immediate",
            "bull_case": "N/A -- risk-reducing exit",
            "bear_case": "Thesis invalidated by price action",
            "thesis_invalidation": "Stop breached",
            "stop_price": round(price * 0.9, 4),
            "target_price": round(price * 1.1, 4),
            "proposed_dollar_amount": None,
            "proposed_allocation_pct": 0.0,
            "sector": self._sector_for_ticker(ticker) or "Unknown",
            "confidence": 1.0,
            "holding_period": "N/A",
            "reason_to_buy_now": "N/A",
            "reason_to_wait": "N/A",
            "data_timestamp": datetime.now(timezone.utc).isoformat(),
            "reward_risk": 0.0,
        }

    def manage_positions(self, position_review_proposals, account_state: dict) -> list:
        outcomes = []
        for ticker, quote in self.check_mechanical_stops():
            outcomes.append(self._evaluate_one(self.build_mechanical_exit_proposal(ticker, quote), account_state))
        for proposal in position_review_proposals or []:
            outcomes.append(self._evaluate_one(proposal, account_state))
        return outcomes

    # -----------------------------------------------------------------
    # Duplicate-session protection
    # -----------------------------------------------------------------

    def _session_already_recorded(self, session_id: str) -> bool:
        text = self._memory_texts.get("memory/DAILY-SNAPSHOTS.md", "")
        return f"## {session_id}" in text

    # -----------------------------------------------------------------
    # Full daily lifecycle
    # -----------------------------------------------------------------

    def run(
        self,
        *,
        session_id: Optional[str] = None,
        candidate_proposals: Optional[list] = None,
        position_review_proposals: Optional[list] = None,
        account_state_overrides: Optional[dict] = None,
        force_rerun: bool = False,
    ) -> DailySessionResult:
        now = datetime.now(timezone.utc)
        session_id = session_id or now.strftime("%Y-%m-%d")

        startup = self.run_startup_checks()
        if not startup.passed:
            return DailySessionResult(
                timestamp=now.isoformat(), session_id=session_id, startup_check=startup,
                market_context={}, account_state={}, candidate_outcomes=[], position_outcomes=[],
                trades_executed=[], trades_rejected=[], top_candidate=None, top_score=None,
                final_action="STARTUP_CHECK_FAILED", snapshot=None,
                research_log_entry="", trade_log_entries=[],
                real_moomoo_write_used=False, live_account_data_used=False, local_paper_execution_only=True,
                already_ran=False,
            )

        if not force_rerun and self._session_already_recorded(session_id):
            return DailySessionResult(
                timestamp=now.isoformat(), session_id=session_id, startup_check=startup,
                market_context={}, account_state={}, candidate_outcomes=[], position_outcomes=[],
                trades_executed=[], trades_rejected=[], top_candidate=None, top_score=None,
                final_action="ALREADY_RAN", snapshot=None,
                research_log_entry="", trade_log_entries=[],
                real_moomoo_write_used=False, live_account_data_used=False, local_paper_execution_only=True,
                already_ran=True,
            )

        account_state = self.gather_account_state()
        if account_state_overrides:
            account_state.update(account_state_overrides)
        market_context = self.gather_market_context()

        candidate_outcomes = self.evaluate_candidates(candidate_proposals, account_state)
        position_outcomes = self.manage_positions(position_review_proposals, account_state)
        all_outcomes = candidate_outcomes + position_outcomes

        trades_executed = [o.order for o in all_outcomes if o.stage == "executed"]
        trades_rejected = [o for o in all_outcomes if o.stage != "executed"]

        scored = [o for o in all_outcomes if o.score is not None]
        top = max(scored, key=lambda o: o.score) if scored else None
        top_candidate = top.ticker if top else None
        top_score = top.score if top else None

        if trades_executed:
            final_action = "TRADED"
        elif all_outcomes:
            final_action = "NO_QUALIFYING_CANDIDATE"
        else:
            final_action = "DO_NOTHING"

        snapshot = self.session.daily_snapshot()

        research_entry = self._render_research_log_entry(session_id, now, market_context, all_outcomes, final_action, account_state, snapshot)
        trade_entries = self._render_trade_log_entries(session_id, all_outcomes)

        return DailySessionResult(
            timestamp=now.isoformat(), session_id=session_id, startup_check=startup,
            market_context=market_context, account_state=account_state,
            candidate_outcomes=candidate_outcomes, position_outcomes=position_outcomes,
            trades_executed=trades_executed, trades_rejected=trades_rejected,
            top_candidate=top_candidate, top_score=top_score, final_action=final_action,
            snapshot=snapshot, research_log_entry=research_entry, trade_log_entries=trade_entries,
            real_moomoo_write_used=False, live_account_data_used=False, local_paper_execution_only=True,
            already_ran=False,
        )

    # -----------------------------------------------------------------
    # Log rendering (pure text -- no file I/O here)
    # -----------------------------------------------------------------

    def _render_research_log_entry(self, session_id, now, market_context, outcomes, final_action, account_state, snapshot) -> str:
        lines = [f"### {now.isoformat()} — Daily Runner Session ({session_id})", ""]
        lines.append("#### Session Facts")
        lines.append(f"- Execution mode: {EXECUTION_MODE_LOCAL_PAPER_REAL_DATA}")
        lines.append("- Real moomoo data used: YES")
        lines.append("- Real moomoo write used: NO")
        lines.append("- Live account data used: NO")
        lines.append(f"- Market state: {market_context.get('market_state')}")
        lines.append(f"- {market_context.get('benchmark_ticker')} benchmark: {market_context.get('benchmark_quote')}")
        lines.append("")
        lines.append("#### Account")
        lines.append(f"- Equity: ${account_state.get('equity')}")
        lines.append(f"- Cash: ${account_state.get('cash')}")
        lines.append(f"- Open positions: {len(account_state.get('positions', []))}")
        lines.append("")
        lines.append("#### Candidates / Position Reviews")
        if not outcomes:
            lines.append("- None supplied this session.")
        for o in outcomes:
            lines.append(f"- {o.ticker} ({o.action}) — score {o.score} — stage: {o.stage}" + (f" — {'; '.join(o.reasons)}" if o.reasons else ""))
        lines.append("")
        lines.append("#### Decision")
        lines.append(final_action)
        return "\n".join(lines)

    def _render_trade_log_entries(self, session_id, outcomes) -> list:
        entries = []
        for o in outcomes:
            if o.stage == "executed" and o.order is not None:
                entries.append(
                    "\n".join([
                        f"## {session_id} — EXECUTED — {o.ticker} {o.action}",
                        f"- Order ID: {o.order.order_id}",
                        f"- Status: {o.order.status}",
                        f"- Fill price: {o.order.fill_price}",
                        f"- Filled quantity: {o.order.filled_quantity}",
                        f"- Realized P&L: {o.order.realized_pnl}",
                        f"- Score: {o.score}",
                    ])
                )
            else:
                entries.append(
                    "\n".join([
                        f"## {session_id} — REJECTED CANDIDATE / NO-TRADE — {o.ticker} {o.action}",
                        "**This is not an executed trade.**",
                        f"- Stage: {o.stage}",
                        f"- Score: {o.score}",
                        f"- Reasons: {'; '.join(o.reasons) if o.reasons else 'n/a'}",
                    ])
                )
        return entries

    def _render_snapshot_entry(self, result: DailySessionResult) -> str:
        snap = result.snapshot
        if snap is None:
            return f"## {result.session_id}\n\nNo snapshot (session did not run: {result.final_action})."
        return "\n".join([
            f"## {result.session_id}",
            "",
            "| Metric | Value |",
            "|---|---|",
            f"| Starting equity | ${snap.starting_equity} |",
            f"| Ending equity | ${snap.current_equity} |",
            f"| Cash | ${snap.cash} |",
            f"| Realized P&L | ${snap.realized_pnl} |",
            f"| Unrealized P&L | ${snap.unrealized_pnl} |",
            f"| Daily P&L | ${snap.daily_pnl} |",
            f"| Total P&L | ${snap.total_pnl} |",
            f"| Positions | {len(snap.positions)} |",
            f"| Max drawdown | {snap.max_drawdown_pct}% |",
            f"| Benchmark | {snap.benchmark_ticker} |",
            f"| Benchmark quote | {snap.benchmark_quote} |",
            f"| Trades executed | {len(result.trades_executed)} |",
            f"| Trades rejected | {len(result.trades_rejected)} |",
            f"| Top candidate | {result.top_candidate} |",
            f"| Top score | {result.top_score} |",
            f"| Final action | {result.final_action} |",
        ])

    # -----------------------------------------------------------------
    # Explicit, separate persistence step -- never called by run()
    # -----------------------------------------------------------------

    def persist_logs(self, result: DailySessionResult) -> None:
        """Writes result's rendered entries to the memory/*.md files on
        disk. Never calls git -- committing/pushing remains a fully
        separate, explicit operator step. Refuses to persist a session
        that never actually ran (startup failure or already-ran).
        """
        if result.already_ran:
            raise RuntimeError("Refusing to persist logs for a session that was already recorded.")
        if result.final_action == "STARTUP_CHECK_FAILED":
            raise RuntimeError("Refusing to persist logs for a session whose startup check failed.")

        self._append(self.memory_paths["memory/RESEARCH-LOG.md"], result.research_log_entry)
        for entry in result.trade_log_entries:
            self._append(self.memory_paths["memory/TRADE-LOG.md"], entry)
        self._append(self.memory_paths["memory/DAILY-SNAPSHOTS.md"], self._render_snapshot_entry(result))
        # Keep the in-memory copy in sync so a subsequent run() in the
        # same process sees this session as already recorded too.
        self._memory_texts["memory/DAILY-SNAPSHOTS.md"] = self._memory_texts.get("memory/DAILY-SNAPSHOTS.md", "") + f"\n## {result.session_id}\n"

    def _append(self, path, text: str) -> None:
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n" + text.rstrip() + "\n")
