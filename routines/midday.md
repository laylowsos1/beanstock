# Midday Workflow

You are running Beanstock's midday monitoring workflow in PAPER / SIMULATED mode.

1. Read `CLAUDE.md`, `memory/TRADING-STRATEGY.md`, today's research, and the tail of the trade log.
2. Pull current simulated positions, quotes, and open protective orders from the configured broker adapter.
3. Reconcile live simulated positions against the trade log. If they differ, trust broker state and document the discrepancy.
4. For each position:
   - review unrealized P&L
   - review catalyst/thesis status
   - review material midday news
   - review support/resistance and sector trend
   - flag concentration risk
5. Exit a simulated position when its thesis is invalidated or its defined loss rule is reached. Do not average down just because price fell.
6. For winners:
   - at +15% review whether ~7% trailing protection is technically appropriate
   - at +20% review whether ~5% trailing protection is technically appropriate
   - never move protection farther away merely to avoid an exit
7. If two consecutive trades in a sector have failed, block new entries in that sector until weekly review.
8. Append any action or meaningful thesis change to the trade/research logs.
9. Persist changes to Git.

If the broker cannot support a desired protective order on a fractional position, record that clearly and rely on scheduled monitoring/alerts rather than claiming an order exists.
