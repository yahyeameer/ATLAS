---
name: liquidity-sweep-research
description: "Design and run liquidity-sweep reversal experiments (prior-day high or low swept, close back inside) on EURUSD dev and validation data."
version: 0.1.0
author: ATLAS
license: UNLICENSED
platforms: [linux]
metadata:
  hermes:
    tags: [ATLAS, Forex, Strategy, Reversal]
    related_skills: [backtest-analysis, mfe-mae-analysis, trend-pullback-research]
---

# Liquidity Sweep Research

## Objective

Test whether a sweep of the prior-day high or low that closes back inside reverses far enough to pay 2 R after costs, and hand the orchestrator a recorded, falsifiable result, whether it passes or fails.

## When to use

- A card asks for a liquidity-sweep hypothesis, parameter test or robustness check.

Don't use for: other setups, other symbols (see below), exit research, or code changes.

## Required inputs

- The hypothesis on the card, or the configured one: "A sweep of the prior-day high or low that closes back inside reverses far enough to pay 2R after costs."
- The data window: `dev`, `validation`, or a custom window inside them.
- Symbol: EURUSD only. PRD §16 allows GBPUSD and XAUUSD only after a separate validation, and the API refuses other symbols for this setup. Extending it is an operator decision; ask with `kanban_block`.

## Procedure

1. Check the budget and history: `list_runs(strategy="liquidity_sweep")`. 20 experiments per strategy per month, failures included.
2. Write the hypothesis, the single change being tested, and the falsification rule before running anything.
3. Choose parameters. Defaults: entries 07:00 to 16:00 London time, `min_sweep_atr` 0.0, `sl_buffer_atr` 0.2, `sl_min_atr` 1.0, `sl_max_atr` 1.5. The declared grid is `min_sweep_atr` in {0.0, 0.2} and `entry_end` in {12:00, 16:00}.
4. Run it with `run_backtest(strategy="liquidity_sweep", window="dev", params={...})` for a quick look, or `run_walk_forward(strategy="liquidity_sweep")` for the full evaluation with every gate.
5. Watch the trade count. Sweeps of the prior-day extreme are rarer than pullbacks, so the 300 out-of-sample trade minimum is the gate most likely to fail on one symbol. Report it plainly rather than loosening filters to manufacture trades.
6. Analyse with `backtest-analysis`. Reversal setups often show losers that first went well in favour; if so, hand exit ideas to a T1 card instead of changing exits here.
7. Propose at most one next experiment, with its own falsification rule.

## Tools

- atlas-backtest: `run_backtest`, `run_walk_forward`, `get_run_summary`, `monte_carlo`, `list_runs`

## Output format

Hypothesis, change tested, falsification rule, result (with run ids), verdict, next experiment. Complete the card with the ATLAS metadata shape: `experiment_id, strategy_version, data_window, trades, expectancy_r, pf, max_dd_mc95, dsr, artifacts`.

## Safety constraints

- Never request holdout data. Windows ending after the validation period are refused; don't try variants.
- Report every experiment you ran, including failures and quick looks.
- Output expectancy in R after costs, never total return alone.
- EURUSD only until the operator approves another symbol.
- Never edit `config/strategies/` or promote a strategy; propose, and the operator decides.

## Validation requirements

- The hypothesis and falsification rule were written before the first run of this card.
- Every run id you mention appears in `list_runs`.
- A PASS claim comes from `run_walk_forward` gates, including the 300-trade minimum.
