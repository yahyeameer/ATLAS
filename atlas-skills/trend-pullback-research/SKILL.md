---
name: trend-pullback-research
description: "Design and run trend-pullback experiments (H1 trend, M15 pullback to the fast EMA) on dev and validation data."
version: 0.1.0
author: ATLAS
license: UNLICENSED
platforms: [linux]
metadata:
  hermes:
    tags: [ATLAS, Forex, Strategy, Trend]
    related_skills: [backtest-analysis, mfe-mae-analysis, session-breakout-research, liquidity-sweep-research]
---

# Trend Pullback Research

## Objective

Test whether M15 pullbacks into the fast EMA resume the H1 trend often enough to pay 2 R after costs, and hand the orchestrator a recorded, falsifiable result, whether it passes or fails.

## When to use

- A card asks for a trend-pullback hypothesis, parameter test or robustness check.
- A previous trend-pullback run failed a gate and the card asks why or what to try next.

Don't use for: other setups, exit-rule changes (T1 work), or code changes to the setup (create a backtest-engineer card).

## Required inputs

- The hypothesis on the card, or the configured one: "M15 pullbacks into the fast EMA resume the H1 trend often enough to pay 2R after costs."
- The data window: `dev` (walk-forward), `validation` (selection, used sparingly), or a custom window inside them.
- Symbols: EURUSD and GBPUSD are configured for this setup.

## Procedure

1. Check the budget and history: `list_runs(strategy="trend_pullback")`. The budget is 20 experiments per strategy per month, and every run counts, failures included. If fewer than 3 remain, ask the orchestrator before spending them.
2. Write down before running: the hypothesis, the one thing this experiment changes, and the result that would falsify it (for example "expectancy below 0.10 R on dev OOS").
3. Choose parameters. Defaults: `adx_min` 20, `pullback_bars` 6, `sl_buffer_atr` 0.2, `sl_min_atr` 1.0, `sl_max_atr` 1.5. The declared grid is `adx_min` in {20, 25} and `pullback_bars` in {4, 8}. Change one parameter per experiment, and stay near the grid; wide searches inflate the trial count and the deflated Sharpe punishes them.
4. Run it:
   - Quick look on dev: `run_backtest(strategy="trend_pullback", window="dev", params={...})`.
   - Full candidate evaluation: `run_walk_forward(strategy="trend_pullback")`, which runs the declared grid on dev walk-forward, validation once, and every §15 gate.
   - Stress costs with `spread_mult` up to 3.0; never below the configured 1.5.
5. Analyse with the `backtest-analysis` procedure. For a failure, name the gate and check whether the problem is regime (ADX filter too loose), entry timing (pullback depth) or exits (use `mfe-mae-analysis`).
6. Propose at most one next experiment, with its own falsification criterion.

## Tools

- atlas-backtest: `run_backtest`, `run_walk_forward`, `get_run_summary`, `monte_carlo`, `list_runs`

## Output format

Hypothesis, change tested, falsification rule, result (with run ids), verdict, next experiment. Complete the card with the ATLAS metadata shape: `experiment_id, strategy_version, data_window, trades, expectancy_r, pf, max_dd_mc95, dsr, artifacts`.

## Safety constraints

- Never request holdout data. Windows ending after the validation period are refused; don't try variants.
- Report every experiment you ran, including failures and quick looks.
- Output expectancy in R after costs, never total return alone.
- One position per setup per symbol; never pyramiding, averaging down, grid or martingale variants.
- Never edit `config/strategies/` or promote a strategy; propose, and the operator decides.

## Validation requirements

- The hypothesis and falsification rule were written before the first run of this card.
- Every run id you mention appears in `list_runs`.
- A PASS claim comes from `run_walk_forward` gates, not from a single `run_backtest`.
