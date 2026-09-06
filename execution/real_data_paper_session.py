"""Beanstock REAL-DATA PAPER MODE.

    REAL moomoo read-only data (MoomooReadOnlyBroker)
        -> market/account/quote data
        -> Claude research -> TradeProposal
        -> deterministic Risk Engine -> ExecutionIntent
        -> BrokerGateway -> PaperWriteController
        -> LOCAL FakePaperBroker
        -> paper positions / P&L / trade log

The point: trade against REAL market prices while execution stays
entirely local and simulated. No moomoo write endpoint is ever called
by this module -- the execution target is broker.fake_paper.FakePaperBroker,
which makes no network call of any kind. This module's own guard
(RealDataPaperSession.__init__) additionally requires the read-data
source to literally be a MoomooReadOnlyBroker instance and the
execution target to literally be a FakePaperBroker instance -- there is
no constructor argument, override, or code path that lets either be
swapped for a write-capable broker.

Runtime mode
--------------
BEANSTOCK_EXECUTION_MODE must equal EXECUTION_MODE_LOCAL_PAPER_REAL_DATA
("LOCAL_PAPER_REAL_DATA") for a RealDataPaperSession to even construct --
checked once at construction (env var, or an explicit override for
tests), fail closed otherwise. In this mode:

  - real moomoo READS are allowed (MoomooReadOnlyBroker only)
  - local FakePaperBroker execution is allowed
  - moomoo WRITE requests are forbidden -- structurally: this module
    never imports or constructs broker.moomoo_paper.MoomooPaperBroker,
    and MoomooReadOnlyBroker itself has no write method (submit_
    execution_intent/cancel_order/close_position on it unconditionally
    raise ReadOnlyBrokerError)
  - live account paths are forbidden -- MoomooReadOnlyBroker's own
    path-prefix allowlist (broker/MOOMOO_API_CONTRACT.md) still applies
    to every real read this module makes

Execution still goes through execution.paper_write_controller.PaperWriteController
for its business-safety gates (ARMED state, SAFE_MODE, daily-loss/
weekly-drawdown, intent/quote staleness, duplicate audit_reference,
FIRST_ORDER_TEST_MODE's notional cap) -- constructed here with
enforce_network_write_gate=False, since FakePaperBroker has no real
write to gate or firewall in the first place (see that class's
docstring). BEANSTOCK_PAPER_WRITE_ENABLED is therefore irrelevant to
whether local paper trades execute in this mode, by design -- that flag
only ever meant "may a REAL moomoo write happen," which never happens
here regardless of its value.

Real quotes, never invented
------------------------------
refresh_quote() is the ONLY way a price ever reaches FakePaperBroker in
this module. It fetches a real quote via MoomooReadOnlyBroker.get_quote()
and MoomooReadOnlyBroker.get_quote_timestamp(), and feeds them into
FakePaperBroker.set_quote() with the REAL timestamp preserved -- never
datetime.now(). If the real broker cannot supply a valid, timestamped
quote, this raises RealQuoteUnavailableError rather than falling back
to any invented, cached, or estimated price. Because the real timestamp
is what BrokerGateway/PaperWriteController check for staleness,
Beanstock's existing quote-freshness gates apply to real market data
exactly the way they already applied to fake test data -- nothing about
that logic was changed or loosened.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Optional, Union
import os

from broker.base import Order
from broker.fake_paper import FakePaperBroker
from broker.gateway import BrokerGateway
from broker.moomoo_readonly import MoomooReadOnlyBroker
from execution.intent import create_execution_intent
from execution.paper_write_controller import PaperWriteController

EXECUTION_MODE_LOCAL_PAPER_REAL_DATA = "LOCAL_PAPER_REAL_DATA"
ENV_EXECUTION_MODE = "BEANSTOCK_EXECUTION_MODE"

DEFAULT_BENCHMARK_TICKER = "SPY"


class RealDataPaperModeError(Exception):
    """Raised when BEANSTOCK_EXECUTION_MODE is not
    EXECUTION_MODE_LOCAL_PAPER_REAL_DATA at RealDataPaperSession
    construction time. Fail closed -- this mode never activates itself
    implicitly."""


class RealQuoteUnavailableError(Exception):
    """Raised whenever a real quote cannot be obtained (missing,
    invalid, or no timestamp). Never caught internally to substitute a
    fallback price -- every caller sees this failure directly."""


def _to_decimal(value) -> Optional[Decimal]:
    if value is None or isinstance(value, bool):
        return None
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return d if d.is_finite() else None


def _extract_ticker(proposal) -> Optional[str]:
    if isinstance(proposal, dict):
        ticker = proposal.get("ticker")
    else:
        ticker = getattr(proposal, "ticker", None)
    if not (isinstance(ticker, str) and ticker.strip()):
        return None
    return ticker.strip().upper()


@dataclass
class SimulatedTradeRecord:
    """One row per ExecutionIntent submission attempt (item 11) --
    filled, rejected, or otherwise, so this doubles as an audit trail.
    Every price field here traces back to a real MoomooReadOnlyBroker
    quote at the time it was captured -- never invented.
    """

    ticker: str
    action: str
    real_quote_used: Optional[Decimal]
    real_quote_timestamp: Optional[str]
    simulated_entry_price: Optional[Decimal]
    simulated_quantity: Optional[Decimal]
    candidate_score: Optional[float]
    catalyst: Optional[str]
    stop_price: Optional[Decimal]
    target_price: Optional[Decimal]
    reward_risk: Optional[float]
    audit_reference: Optional[str]
    order_id: Optional[str]
    status: str
    rejection_reason: Optional[str]
    opened_at: str

    # Optional -- carried from the proposal's own "sector" field so
    # callers (e.g. runner/daily_session.py) can attribute a held
    # position's market value to a sector without FakePaperBroker's own
    # Position needing to know about sectors at all.
    sector: Optional[str] = None

    # Updated over time by update_open_trade_tracking() -- always from a
    # fresh real quote, never carried over/estimated.
    latest_real_quote: Optional[Decimal] = None
    latest_quote_timestamp: Optional[str] = None
    unrealized_pnl: Optional[Decimal] = None
    realized_pnl: Optional[Decimal] = None
    max_favorable_excursion: Optional[Decimal] = None
    max_adverse_excursion: Optional[Decimal] = None
    closed: bool = False


@dataclass
class DailyPortfolioSnapshot:
    """Item 12. Every field is derived from FakePaperBroker's own live
    state plus (for the benchmark) a real SPY quote -- nothing here is
    persisted across process restarts; max_drawdown_pct and daily_pnl
    are computed from equity observations taken within this session's
    own lifetime (see RealDataPaperSession docstring for that scope
    limitation).
    """

    timestamp: str
    starting_equity: Decimal
    current_equity: Decimal
    cash: Decimal
    positions: list
    daily_pnl: Decimal
    total_pnl: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    win_rate: Optional[float]
    max_drawdown_pct: Decimal
    benchmark_ticker: str
    benchmark_quote: Optional[Decimal]
    benchmark_quote_timestamp: Optional[str]
    benchmark_return_pct: Optional[float]
    account_return_pct: float
    benchmark_note: str


class RealDataPaperSession:
    def __init__(
        self,
        *,
        real_data_broker: MoomooReadOnlyBroker,
        paper_broker: FakePaperBroker,
        execution_mode: Optional[str] = None,
        write_enabled: Optional[bool] = None,
        first_order_test_mode: bool = False,
        max_intent_age_seconds: float = 300.0,
        max_quote_age_seconds: float = 60.0,
        benchmark_ticker: str = DEFAULT_BENCHMARK_TICKER,
    ):
        mode = execution_mode if execution_mode is not None else os.environ.get(ENV_EXECUTION_MODE)
        if mode != EXECUTION_MODE_LOCAL_PAPER_REAL_DATA:
            raise RealDataPaperModeError(
                f"{ENV_EXECUTION_MODE} must be {EXECUTION_MODE_LOCAL_PAPER_REAL_DATA!r} "
                f"to construct a RealDataPaperSession; got {mode!r}."
            )

        if not isinstance(real_data_broker, MoomooReadOnlyBroker):
            raise TypeError(
                "real_data_broker must be a broker.moomoo_readonly.MoomooReadOnlyBroker -- "
                "no other broker may ever supply real quotes to this session."
            )
        if not isinstance(paper_broker, FakePaperBroker):
            raise TypeError(
                "paper_broker must be a broker.fake_paper.FakePaperBroker -- "
                "RealDataPaperSession never executes against a real or moomoo-backed broker."
            )

        self._real = real_data_broker
        self._paper = paper_broker
        self.benchmark_ticker = benchmark_ticker.strip().upper()

        self.gateway = BrokerGateway(
            max_intent_age_seconds=max_intent_age_seconds,
            max_quote_age_seconds=max_quote_age_seconds,
        )
        self.controller = PaperWriteController(
            write_enabled=write_enabled,
            enforce_network_write_gate=False,  # FakePaperBroker has no real write to gate
            first_order_test_mode=first_order_test_mode,
            max_intent_age_seconds=max_intent_age_seconds,
            max_quote_age_seconds=max_quote_age_seconds,
        )

        self._trades: list = []
        self._equity_curve: list = []
        self._starting_equity: Optional[Decimal] = None
        self._benchmark_start_price: Optional[Decimal] = None
        self._benchmark_start_timestamp: Optional[str] = None

    @property
    def starting_equity(self) -> Optional[Decimal]:
        """None until the first evaluate_and_submit()/daily_snapshot()
        call establishes it (from FakePaperBroker's own live equity)."""
        return self._starting_equity

    # -----------------------------------------------------------------
    # Real quotes -> local execution
    # -----------------------------------------------------------------

    def refresh_quote(self, ticker: str) -> Decimal:
        """The ONLY path a price takes into this session's FakePaperBroker.
        Raises RealQuoteUnavailableError rather than ever inventing,
        reusing a stale cached value, or falling back to any estimate.
        """
        normalized = (ticker or "").strip().upper()
        if not normalized:
            raise RealQuoteUnavailableError("Missing or invalid ticker; cannot refresh a real quote.")

        price = self._real.get_quote(normalized)
        if price is None or not price.is_finite() or price <= 0:
            raise RealQuoteUnavailableError(
                f"No valid real quote available for {normalized!r}; refusing to invent a price."
            )
        timestamp = self._real.get_quote_timestamp(normalized)
        if timestamp is None:
            raise RealQuoteUnavailableError(
                f"Real quote for {normalized!r} has no timestamp; refusing to use it without one."
            )

        self._paper.set_quote(normalized, price, timestamp)
        return price

    # -----------------------------------------------------------------
    # Full pipeline: TradeProposal -> ... -> FakePaperBroker
    # -----------------------------------------------------------------

    def evaluate_and_submit(self, proposal: Union[dict, object], **account_state_kwargs):
        """Runs TradeProposal -> schema validation -> risk engine ->
        ExecutionIntent -> BrokerGateway -> PaperWriteController ->
        FakePaperBroker, using a freshly refreshed REAL quote for the
        proposal's ticker. Returns (ExecutionIntentResult, ControllerResult,
        Order | None).

        Raises RealQuoteUnavailableError (propagated from refresh_quote())
        if a real quote cannot be obtained -- the pipeline never runs on
        an invented price. Does NOT catch a stale-quote rejection --
        that is a normal ControllerResult(allowed=False) outcome, not an
        exception, exactly like every other business-rule rejection in
        this pipeline.
        """
        ticker = _extract_ticker(proposal)
        if ticker is None:
            raise RealQuoteUnavailableError("Missing or invalid ticker on the proposal; cannot refresh a real quote.")

        quote = self.refresh_quote(ticker)
        quote_timestamp = self._paper.get_quote_timestamp(ticker)

        result = create_execution_intent(proposal, **account_state_kwargs)
        if not result.created:
            return result, None, None

        controller_result, order = self.controller.submit(result.intent, self.gateway, self._paper)

        record = self._record_trade(
            ticker=ticker,
            action=result.intent.action,
            quote=quote,
            quote_timestamp=quote_timestamp,
            decision=result.decision,
            audit_reference=result.intent.audit_reference,
            order=order,
            controller_result=controller_result,
        )

        if self._starting_equity is None:
            self._starting_equity = self._paper.get_account().equity

        return result, controller_result, order

    def _record_trade(self, *, ticker, action, quote, quote_timestamp, decision, audit_reference, order, controller_result) -> SimulatedTradeRecord:
        normalized = (decision or {}).get("normalized_proposal") or {}
        record = SimulatedTradeRecord(
            ticker=ticker,
            action=action,
            real_quote_used=quote,
            real_quote_timestamp=quote_timestamp.isoformat() if quote_timestamp else None,
            simulated_entry_price=order.fill_price if order else None,
            simulated_quantity=order.filled_quantity if order else None,
            candidate_score=normalized.get("candidate_score"),
            catalyst=normalized.get("catalyst"),
            stop_price=_to_decimal(normalized.get("stop_price")),
            target_price=_to_decimal(normalized.get("target_price")),
            reward_risk=normalized.get("reward_risk"),
            audit_reference=audit_reference,
            order_id=order.order_id if order else None,
            status=order.status if order else "REJECTED",
            rejection_reason=(order.rejection_reason if order else "; ".join(controller_result.reasons)) or None,
            opened_at=datetime.now(timezone.utc).isoformat(),
            sector=normalized.get("sector"),
        )
        if order is not None and order.status == "FILLED" and order.realized_pnl is not None:
            record.realized_pnl = order.realized_pnl
            record.closed = action in ("REDUCE", "EXIT")
        self._trades.append(record)
        return record

    def get_trade_log(self) -> list:
        return list(self._trades)

    # -----------------------------------------------------------------
    # Tracking: subsequent real prices / MFE / MAE (item 11)
    # -----------------------------------------------------------------

    def update_open_trade_tracking(self) -> list:
        """For every ticker currently held (per FakePaperBroker's own
        positions), refresh its real quote and update the most recent
        open trade record's latest price / unrealized P&L / max
        favorable & adverse excursion. A refresh failure for one ticker
        never invents a price for it -- that ticker's tracking is simply
        left unchanged for this call, and the failure is included in the
        returned list so callers can see it.
        """
        failures = []
        held_tickers = {p.ticker for p in self._paper.get_positions()}
        for ticker in held_tickers:
            try:
                quote = self.refresh_quote(ticker)
            except RealQuoteUnavailableError as exc:
                failures.append((ticker, str(exc)))
                continue
            quote_ts = self._paper.get_quote_timestamp(ticker)
            position = self._paper.get_position(ticker)
            if position is None:
                continue
            latest_open_record = self._latest_open_record(ticker)
            if latest_open_record is None:
                continue
            latest_open_record.latest_real_quote = quote
            latest_open_record.latest_quote_timestamp = quote_ts.isoformat() if quote_ts else None
            latest_open_record.unrealized_pnl = position.unrealized_pnl
            if latest_open_record.max_favorable_excursion is None or position.unrealized_pnl > latest_open_record.max_favorable_excursion:
                latest_open_record.max_favorable_excursion = position.unrealized_pnl
            if latest_open_record.max_adverse_excursion is None or position.unrealized_pnl < latest_open_record.max_adverse_excursion:
                latest_open_record.max_adverse_excursion = position.unrealized_pnl
        return failures

    def _latest_open_record(self, ticker: str) -> Optional[SimulatedTradeRecord]:
        for record in reversed(self._trades):
            if record.ticker == ticker and record.action in ("BUY", "ADD") and record.status == "FILLED":
                return record
        return None

    # -----------------------------------------------------------------
    # Daily portfolio snapshot (item 12)
    # -----------------------------------------------------------------

    def daily_snapshot(self) -> DailyPortfolioSnapshot:
        account = self._paper.get_account()
        positions = self._paper.get_positions()

        if self._starting_equity is None:
            self._starting_equity = account.equity

        previous_equity = self._equity_curve[-1] if self._equity_curve else self._starting_equity
        self._equity_curve.append(account.equity)

        peak = self._equity_curve[0]
        max_drawdown_pct = Decimal("0")
        for value in self._equity_curve:
            peak = max(peak, value)
            if peak > 0:
                drawdown = (peak - value) / peak * 100
                max_drawdown_pct = max(max_drawdown_pct, drawdown)

        realized_pnl = sum((t.realized_pnl for t in self._trades if t.realized_pnl is not None), Decimal("0"))
        unrealized_pnl = sum((p.unrealized_pnl for p in positions), Decimal("0"))

        closed_trades = [t for t in self._trades if t.action in ("REDUCE", "EXIT") and t.status == "FILLED" and t.realized_pnl is not None]
        win_rate = None
        if closed_trades:
            wins = sum(1 for t in closed_trades if t.realized_pnl > 0)
            win_rate = wins / len(closed_trades)

        benchmark_note = ""
        benchmark_quote = None
        benchmark_ts = None
        benchmark_return_pct = None
        try:
            benchmark_quote = self._real.get_quote(self.benchmark_ticker)
            benchmark_ts = self._real.get_quote_timestamp(self.benchmark_ticker)
            if benchmark_quote is not None and benchmark_quote.is_finite() and benchmark_quote > 0:
                if self._benchmark_start_price is None:
                    self._benchmark_start_price = benchmark_quote
                    self._benchmark_start_timestamp = benchmark_ts.isoformat() if benchmark_ts else None
                    benchmark_note = f"Benchmark baseline set to {self.benchmark_ticker}={benchmark_quote} this snapshot."
                else:
                    benchmark_return_pct = float((benchmark_quote / self._benchmark_start_price - 1) * 100)
            else:
                benchmark_note = f"No valid real quote for {self.benchmark_ticker}; benchmark comparison unavailable."
                benchmark_quote = None
        except Exception as exc:
            benchmark_note = f"Benchmark quote fetch failed ({type(exc).__name__}); no price was invented."
            benchmark_quote = None
            benchmark_ts = None

        account_return_pct = float((account.equity / self._starting_equity - 1) * 100) if self._starting_equity else 0.0

        return DailyPortfolioSnapshot(
            timestamp=datetime.now(timezone.utc).isoformat(),
            starting_equity=self._starting_equity,
            current_equity=account.equity,
            cash=account.cash,
            positions=positions,
            daily_pnl=account.equity - previous_equity,
            total_pnl=account.equity - self._starting_equity,
            realized_pnl=realized_pnl,
            unrealized_pnl=unrealized_pnl,
            win_rate=win_rate,
            max_drawdown_pct=max_drawdown_pct,
            benchmark_ticker=self.benchmark_ticker,
            benchmark_quote=benchmark_quote,
            benchmark_quote_timestamp=benchmark_ts.isoformat() if benchmark_ts else None,
            benchmark_return_pct=benchmark_return_pct,
            account_return_pct=account_return_pct,
            benchmark_note=benchmark_note,
        )
