# Market-Open Workflow

You are running Beanstock's market-open workflow in PAPER / SIMULATED mode.

1. Read `CLAUDE.md`, `memory/TRADING-STRATEGY.md`, today's `memory/RESEARCH-LOG.md` entry, and the tail of `memory/TRADE-LOG.md`.
2. If today's research entry is missing, do not trade. Run research first.
3. Pull fresh paper-account state and quotes from the configured broker adapter.
4. Revalidate every planned trade:
   - candidate score >= 75
   - catalyst still valid
   - liquidity/spread acceptable
   - positions after trade <= 6
   - new trades this week <= 3
   - initial position <= 15% while equity < $2,000
   - absolute company exposure <= 20%
   - sector exposure <= 30%
   - position cost <= available cash
   - reward:risk >= 2:1
5. Skip and log any failed gate.
6. For approved PAPER trades, submit only through the configured simulated broker path. Verify the account is simulated immediately before submission.
7. Establish broker-supported downside protection when technically available. If a fractional position cannot use the desired protective order type, log that limitation and use the monitoring workflow rather than pretending a stop exists.
8. Append every executed paper trade to `memory/TRADE-LOG.md`: ticker, size, fill, catalyst, thesis, invalidation, target, R:R, sector, and resulting allocation.
9. Persist changes to Git.

Never place a live-money order from this workflow unless the project owner explicitly changes the project out of PAPER mode.
