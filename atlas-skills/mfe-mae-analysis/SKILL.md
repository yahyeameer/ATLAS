---
name: mfe-mae-analysis
description: "Analyse maximum favourable and adverse excursion of recorded ATLAS trades to find exit problems and loss clusters."
version: 0.1.0
author: ATLAS
license: UNLICENSED
platforms: [linux]
metadata:
  hermes:
    tags: [ATLAS, Forex, Exits, Journal]
    related_skills: [backtest-analysis, risk-review]
---

# MFE / MAE Analysis

## Objective

Explain how trades behave between entry and exit: how far winners and losers ran for and against the position (in R), where losses cluster, and which exit rules from PRD §18 are worth testing next. The output is evidence for a T1 exit experiment, not a change to live exits.

## When to use

- A card asks why a run's expectancy is low, why the win rate is what it is, or which exit variant to test.
- A performance review needs loss-cluster analysis.

Don't use for: pass/fail judgements (use `backtest-analysis`) or running new backtests.

## Required inputs

- A `run_id` with recorded MFE and MAE (every ATLAS backtest and walk-forward run records them).
- Optionally a focus: a session, symbol or direction.

## Procedure

1. Group excursions: `mfe_mae(run_id, group_by="exit_reason")`, then by `session` and `direction`.
2. Read the stop-outs:
   - `losers_reaching_1r` is the share of losing trades that were at least +1 R in profit first. Above about 0.25 suggests a breakeven or partial-close test; below 0.10 means those rules would mostly cut winners.
   - A median loser MAE well past 1 R means slippage or gaps through the stop; check `cost_r` in `query_trades`.
3. Read the winners: a target group whose MFE p75 sits well above the 2 R target suggests a trailing-exit test (ATR trail after +1.5 R, per §18).
4. Find clusters: `loss_clusters(run_id)` for the longest losing streak and the worst sessions, weekdays, hours and symbols by total R. Pull examples with `query_trades(run_id, session=..., outcome="loss")`.
5. Separate structure from noise: a cluster needs enough trades (30 or more) and should show up in more than one year (`performance_summary(run_id, by="year")`) before it counts.
6. Rank at most three exit or filter ideas by expected effect and name the metric each would move.

## Tools

- atlas-journal: `mfe_mae`, `loss_clusters`, `query_trades`
- atlas-performance: `performance_summary`

## Output format

A table per grouping (trades, expectancy R, MFE median and p75, MAE median, losers reaching +1 R), the worst clusters, and a ranked list of proposed T1 experiments. Complete the card with `experiment_id` set to the analysed run id, `artifacts: [<run_id>]`, and the run's `trades` and `expectancy_r`.

## Safety constraints

- Never request holdout data or runs that include it.
- Output everything in R after costs, never currency or total return alone.
- Exit changes are proposals for T1 experiments; you never change live trade management.
- Report every query that shaped a conclusion, including groupings that showed nothing.

## Validation requirements

- Each proposed experiment names the numbers that motivated it and the group size behind them.
- Clusters under 30 trades are labelled as anecdotes, not findings.
- Numbers match the tool results; no MFE or MAE figure is computed by hand.
