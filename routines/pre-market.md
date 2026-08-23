# Pre-Market Workflow

You are running Beanstock's pre-market workflow in PAPER / SIMULATED mode.

1. Read `CLAUDE.md`, `memory/TRADING-STRATEGY.md`, `memory/PROJECT-CONTEXT.md`, the tail of `memory/TRADE-LOG.md`, and the latest `memory/RESEARCH-LOG.md` entries.
2. Pull current paper-account state from the configured broker adapter. If broker data is unavailable, do not invent it; continue in research-only mode and flag the missing data.
3. Research:
   - S&P 500 futures / broad market direction
   - VIX
   - market breadth if available
   - economic calendar
   - top catalysts today
   - earnings calendar
   - sector momentum
   - material news on every held ticker
4. Run a broad stock screen. Prefer:
   - beaten-down quality names with identifiable rerating catalysts
   - momentum names supported by fundamentals
   - liquid stocks suitable for a small account / fractional positioning
5. Score serious candidates using the 100-point model in `CLAUDE.md`.
6. Write today's dated research entry with 2-3 best actionable ideas. Each must include catalyst, timing, entry zone, thesis invalidation/stop, target, R:R, proposed allocation, and why now.
7. Default decision is HOLD / DO NOTHING unless an edge is clear.
8. Persist all research changes to Git. Never store secrets or account identifiers.
