---
name: session-breakout-research
description: "Design and run session-breakout experiments (Asian range broken around the London open) on dev and validation data."
version: 0.1.0
author: ATLAS
license: UNLICENSED
platforms: [linux]
metadata:
  hermes:
    tags: [ATLAS, Forex, Strategy, Breakout, Sessions]
    related_skills: [backtest-analysis, mfe-mae-analysis, trend-pullback-research, forex-market-analysis]
---

# Session Breakout Research

## Objective

Test whether the first break of the Asian range around the London open continues far enough to pay 2 R after costs, and hand the orchestrator a recorded, falsifiable result, whether it passes or fails.

## When to use

- A card asks for a session-breakout hypothesis, parameter test or robustness check.
- A card asks whether momentum or retest entries work better.

Don't use for: other setups, exit-rule changes (T1), or code changes (create a backtest-engineer card).

## Required inputs

- The hypothesis on the card, or the configured one: "The first break of the Asian range around the London open continues far enough to pay 2R after costs."
- The data window: `dev`, `validation`, or a custom window inside them.
- Symbols: EURUSD and GBPUSD are configured.

## Procedure

1. Check the budget and history: `list_runs(strategy="session_breakout")`. 20 experiments per strategy per month, failures included.
2. Write the hypothesis, the single change being tested, and the falsification rule before running anything.
3. Choose parameters. Defaults: `mode` momentum, entries 07:00 to 10:00 London time, Asian range between 1.0 and 4.0 H1 ATR, `sl_buffer_atr` 0.5, `sl_min_atr` 1.0, `sl_max_atr` 1.5. The declared grid is `mode` in {momentum, retest} and `range_max_h1atr` in {3.0, 5.0}.
4. Know what the setup already enforces: at most one long and one short trigger per symbol per day, entries only in the London window (the session-open delay does not apply because the setup is session based), spread at most 20% of the stop distance, no entries after Friday 18:00 UTC.
5. Run it with `run_backtest(strategy="session_breakout", window="dev", params={...})` for a quick look, or `run_walk_forward(strategy="session_breakout")` for the full candidate evaluation with every gate. Stress `spread_mult` up to 3.0: breakouts fill in fast markets, so cost sensitivity matters more here than for pullbacks.
6. Analyse with `backtest-analysis`. Check the session breakdown and the day-of-week clusters (`loss_clusters` if you have journal access): a breakout edge that exists only on one weekday is likely noise.
7. Propose at most one next experiment, with its own falsification rule.

## Tools

- atlas-backtest: `run_backtest`, `run_walk_forward`, `get_run_summary`, `monte_carlo`, `list_runs`

## Output format

Hypothesis, change tested, falsification rule, result (with run ids), verdict, next experiment. Complete the card with the ATLAS metadata shape: `experiment_id, strategy_version, data_window, trades, expectancy_r, pf, max_dd_mc95, dsr, artifacts`.

## Safety constraints

- Never request holdout data. Windows ending after the validation period are refused; don't try variants.
- Report every experiment you ran, including failures and quick looks.
- Output expectancy in R after costs, never total return alone.
- Never edit `config/strategies/` or promote a strategy; propose, and the operator decides.

## Validation requirements

- The hypothesis and falsification rule were written before the first run of this card.
- Every run id you mention appears in `list_runs`.
- A PASS claim comes from `run_walk_forward` gates, including expectancy at 2× spread.
