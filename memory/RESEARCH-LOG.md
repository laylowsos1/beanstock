# Research Log

Daily pre-market research entries are appended here.

## Template
### YYYY-MM-DD — Pre-market Research

#### Account
- Mode: PAPER / SIMULATED
- Equity: $X
- Cash: $X
- Buying power: $X

#### Market Context
- S&P 500 futures:
- VIX:
- Market breadth:
- Economic calendar:
- Major catalysts:
- Sector momentum:

#### Held Position News
- TICKER — material update / none

#### Trade Ideas
1. TICKER — score X/100
   - Catalyst:
   - Timing:
   - Entry:
   - Stop / invalidation:
   - Target:
   - R:R:
   - Allocation:
2. ...

#### Risks
- ...

#### Decision
TRADE / HOLD / DO NOTHING

---

### 2026-09-06T03:28:32Z — Pre-market Research (first REAL-DATA LOCAL PAPER session)

#### Session Facts
- Execution mode: LOCAL_PAPER_REAL_DATA
- Real moomoo data used: YES
- Real moomoo write used: NO
- Live account data used: NO
- Market state: AFTER_HOURS_END
- SPY benchmark baseline: $770.19

#### Account
- Mode: PAPER / SIMULATED
- Equity: $300.00
- Cash: $300.00
- Buying power: $300.00

#### Market Context
- S&P 500 futures: SPY $770.19 vs. prior close $773.17 (-0.39%); QQQ $718.96 vs. $717.67 (+0.18%)
- VIX: not available via current data access (not fabricated)
- Market breadth: not measured this session
- Economic calendar: no hot events returned for the query date
- Major catalysts: SOFI — Scotiabank Outperform initiation ($25 PT, 2026-09-03) + Notre Dame partnership / Kraken crypto-banking pact news (2026-09-05)
- Sector momentum: XLK +0.70%, XLI +0.41% relatively strong; XLV -1.05%, XLY -1.33% weakest (real day-over-day change)

#### Held Position News
- None (no open positions)

#### Trade Ideas
1. SOFI — score 66/100
   - Catalyst: Scotiabank Outperform initiation ($25 PT) + Notre Dame partnership / Kraken crypto-banking pact news
   - Timing: Both items published within ~24-48 hours of research (real moomoo news search)
   - Entry: $18.22 (real quote)
   - Stop / invalidation: $16.94 (-7%, default invalidation zone)
   - Target: $21.13 (+16%)
   - R:R: 2.28:1
   - Allocation: $45.00 / 15%

#### Risks
- Quote used for research was ~31.5 hours old at evaluation time (market closed, AFTER_HOURS_END) — an independent blocker even if score had passed
- Valuation rich (PE ~47x trailing / ~37x TTM); price ~44% off 52-week high with no confirmed technical reversal
- Sector (financials proxy) was down on the day

#### Decision
DO NOTHING

- Candidate: SOFI
- Candidate score: 66
- Candidate decision: REJECT
- Rejection reason: candidate score below Beanstock minimum of 75 (Rule 12)
- Additional independent blocker: stale quote (~31.5h old; would also have failed the freshness gate)
- Final action: DO NOTHING
- Starting simulated equity: $300.00
- Ending simulated equity: $300.00
- Simulated cash: $300.00
- Open positions: 0
- Trades executed: 0
- Trades rejected: 1
