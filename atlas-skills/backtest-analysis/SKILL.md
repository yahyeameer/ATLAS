---
name: backtest-analysis
description: "Read an ATLAS backtest or walk-forward run and judge it against the PRD §15 gates, in R after costs."
version: 0.1.0
author: ATLAS
license: UNLICENSED
platforms: [linux]
metadata:
  hermes:
    tags: [ATLAS, Forex, Backtest, Validation]
    related_skills: [mfe-mae-analysis, risk-review, trend-pullback-research, session-breakout-research, liquidity-sweep-research]
---

# Backtest Analysis

## Objective

Turn a recorded ATLAS run into a verdict a reviewer can check: which PRD §15 gates it passes, which it fails, and what the numbers say about why. Every figure is in R after costs and comes from a tool result, never from memory or estimation.

## When to use

- A card asks you to evaluate, summarize or compare backtest or walk-forward runs.
- A strategy card finished a `run_walk_forward` and the parent needs a synthesis.
- The orchestrator asks whether a candidate is ready for risk review.

Don't use for: designing a new experiment (use the setup's research skill), or exit analysis (use `mfe-mae-analysis`).

## Required inputs

- One or more `run_id`s, or a strategy name to look up with `list_runs`.
- The question on the card: pass/fail, comparison, or diagnosis.

## Procedure

1. Get the facts. `get_run_summary(run_id)` for each run; `list_runs(strategy)` to see how many experiments the strategy has used this month and what failed before. If you only have the orchestrator's read-only tools, use `performance_summary` and the journal tools instead.
2. Check sample size first. Fewer than 300 out-of-sample trades (dev walk-forward plus validation) means the run cannot pass, whatever the expectancy. Say so and stop there.
3. Walk the gates in the run's `gates` list, in this order, quoting value, rule and scope for each:
   - Expectancy after costs ≥ 0.10 R and profit factor ≥ 1.25, on both dev OOS and validation.
   - Expectancy still positive at 2× spread.
   - Monte Carlo p95 drawdown under 60% of the firm's max drawdown; daily-loss breach probability under 2%.
   - Deflated Sharpe > 0.95 given the number of trials recorded.
   - Walk-forward efficiency ≥ 0.5.
   - No single year above 40% of profit; skip-10% Monte Carlo p05 expectancy positive.
   - Expectancy above the random-entry p95; worst ±20% parameter neighbour still positive.
4. For a plain backtest run (kind `backtest`, no gates), report expectancy, PF, trades and costs, and say which gates it cannot speak to (walk-forward, DSR, neighbours).
5. Break the result down with `performance_summary(run_id, by="year")` and `by="session"`. A result carried by one year or one session is fragile; say which.
6. Compare dev OOS against validation. Validation expectancy below half of dev is a warning even when both pass.
7. Write the verdict: PASS only if every gate passes; otherwise FAIL with the failed gates listed first.

## Tools

- atlas-backtest: `get_run_summary`, `list_runs`, `monte_carlo` (read scope)
- atlas-performance: `performance_summary`
- atlas-journal: `query_trades`, `loss_clusters` for drill-down

## Output format

A short report: verdict line, a gate table (gate, scope, value, rule, pass), the breakdowns that matter, and one paragraph of interpretation. Complete the card with:

```yaml
experiment_id: <run_id>
strategy_version: <from summary>
data_window: <from summary>
trades: <OOS trades>
expectancy_r: <dev OOS or validation, say which>
pf: <profit factor>
max_dd_mc95: <percent of equity>
dsr: <deflated Sharpe or null for plain backtests>
artifacts: [<run_id>]
```

## Safety constraints

- Never request holdout data. There is no holdout window or tool; a refusal from the API is final.
- Report every experiment you ran or read, including failures.
- Output expectancy in R after costs, never total return alone.
- You judge candidates; you never promote one, change a gate threshold or edit `config/strategies/`.

## Validation requirements

- Every number in the report appears in a tool result from this session; name the tool for each table.
- The verdict agrees with the gate list: one failed gate means FAIL.
- The trial count used for the deflated Sharpe matches `list_runs` for the strategy.
