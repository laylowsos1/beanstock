# Project Context

## Overview
- Project: Beanstock AI trading agent
- Current phase: build + paper/simulated validation
- Intended broker: moomoo where supported
- Starting real-money target after validation: $300
- Planned contribution: $300 every two weeks
- Primary strategy: catalyst-aware stock swing trading with fractional shares where useful
- Options: disabled initially; revisit only after account size and testing justify them

## Design Sources
This project combines two concepts:
1. A Claude research/portfolio-manager workflow that screens beaten-down or momentum names, scores catalysts, tracks market context, and produces a daily dashboard.
2. A stateless Claude Code agent architecture with scheduled pre-market, market-open, midday, daily-summary, and weekly-review runs; persistent memory stored in Git; hard risk gates before any order.

## Operating Principles
- Paper first.
- Separate strategy from broker implementation.
- Never fabricate live account state.
- Every trade must have documented research before execution.
- Preserve capital before chasing return.
- A no-trade day or week is acceptable.
- Use current data for every execution decision.

## Current Research/Monitoring Requirements
Every serious candidate should include:
- score 0-100
- catalyst + timing
- fundamentals/growth/valuation
- technical trend/support/resistance
- sector context
- bull/bear thesis
- thesis invalidation
- entry/stop/target
- reward:risk
- proposed allocation

Daily dashboard should include:
- equity / cash / P&L
- allocation and sector concentration
- position scores and actions
- held-position news
- upcoming earnings/catalysts
- market regime / VIX / breadth / relevant macro context
- top 3 opportunities
- positions requiring attention
- final action: BUY / ADD / HOLD / REDUCE / EXIT / DO NOTHING

## Safety
- Do not place live trades until explicitly switched out of paper mode.
- Do not store secrets in Git.
- Broker credentials belong only in supported secret/environment storage.
- Do not log account identifiers or secrets into research/trade memory.
