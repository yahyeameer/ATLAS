---
name: data-quality
description: "Check ATLAS bid/ask market data for gaps, crossed quotes, bad OHLC, spread spikes and timezone or DST shifts, and report the affected ranges."
version: 0.1.0
author: ATLAS
license: UNLICENSED
platforms: [linux]
metadata:
  hermes:
    tags: [ATLAS, Forex, Data, Quality]
    related_skills: [forex-market-analysis, backtest-analysis]
---

# Data Quality

## Objective

Make sure the M1 bid/ask data the backtester reads is fit to trust, and report problems precisely enough that a pipeline fix or an exclusion can be made. A backtest on bad data is worse than no backtest, because it looks like evidence.

## When to use

- New data was downloaded or imported (Dukascopy, MT5 export).
- A backtest result looks implausible (a spike of wins or losses on one day, costs far from expectation).
- A card asks for a data-quality report for a symbol and period.

Don't use for: market commentary (use `forex-market-analysis`).

## Required inputs

- Symbols and the period to check (dev and validation only; the holdout is never checked by agents).
- For engineering roles, the task's git worktree with the ATLAS repo.

## Procedure

1. Run the per-year report in the worktree: `atlas-research data quality --symbols EURUSD GBPUSD`. It reports coverage, duplicate timestamps, gaps over 5 minutes inside market hours, crossed quotes (ask below bid), OHLC violations and spread percentiles in pips. The loader refuses holdout rows, so the report covers research data only.
2. Cross-check with the API: `get_spread_stats(symbol, window)` by session, and `get_bars(symbol, "M15", window)` around any suspicious date.
3. Check time handling: data is UTC. Around the March and October/November DST changes, the New York rollover spread spike should move by an hour in UTC. A spike that doesn't move means a timezone bug.
4. Classify each finding: gap (feed outage or holiday), spike (bad tick), crossed quote, DST shift, or duplicate. Give the exact UTC ranges.
5. Decide per finding: fix in the pipeline (create or do the card in the worktree), exclude the range (document it), or accept (holidays, known thin markets).
6. After a pipeline fix, rerun the report and show the before and after counts.

## Tools

- terminal and file (engineering roles, in the task worktree): `atlas-research data quality`, `atlas-research data build`
- atlas-market: `get_spread_stats`, `get_bars`, `collect_market_state`

## Output format

A per-symbol table (year, bars, gaps, duplicates, crossed, bad OHLC, spread median and p95), then the findings list with ranges and decisions. Complete the card with `artifacts` listing the report files or commits and `data_window` set to the checked period.

## Safety constraints

- Never request, load or check holdout data; the tools refuse it and so do you.
- Never edit data by hand to make a backtest look better; fixes go through the pipeline code with tests.
- Work only in the task's git worktree; don't push to `main`.
- Report every finding, including ranges you decided to accept.

## Validation requirements

- Every finding has a UTC range and a count.
- A claimed fix shows the report before and after.
- The tests for any pipeline code you changed pass before handoff.
