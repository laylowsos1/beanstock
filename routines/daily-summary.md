# Daily Summary Workflow

You are running Beanstock's end-of-day workflow in PAPER / SIMULATED mode.

1. Read the latest EOD snapshot from `memory/TRADE-LOG.md` and today's research/trades.
2. Pull final simulated account state, positions, and open orders.
3. Compute:
   - equity
   - cash and cash %
   - day P&L $ / %
   - cumulative P&L $ / %
   - trades today
   - trades this week
   - largest position
   - sector concentration
   - current drawdown from peak equity
4. Append an EOD snapshot to `memory/TRADE-LOG.md`.
5. Produce the daily dashboard:
   - current positions with BUY/ADD/HOLD/WATCH/REDUCE/EXIT classification
   - positions requiring attention
   - upcoming earnings/catalysts
   - top 3 new opportunities
   - market regime and major risks
   - final action: BUY / ADD / HOLD / REDUCE / EXIT / DO NOTHING
6. Persist the EOD snapshot to Git. Never fabricate unavailable values.
