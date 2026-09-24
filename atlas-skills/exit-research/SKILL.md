---
name: exit-research
description: "Run and judge T1 exit research: PRD §18 exit variants against the fixed 2R baseline on the same entries."
version: 0.1.0
author: ATLAS
license: UNLICENSED
platforms: [linux]
metadata:
  hermes:
    tags: [ATLAS, Forex, Exits, T1]
    related_skills: [mfe-mae-analysis, backtest-analysis, trend-pullback-research, session-breakout-research, liquidity-sweep-research]
---

# Exit Research (T1)

## Objective

Find out whether any declared exit rule beats the fixed 2 R baseline out of sample on the same entries, and hand the orchestrator a recorded result, pass or fail. Exits are chosen on expectancy and drawdown together (PRD §18).

## When to use

- A card asks for T1 exit research on a strategy, or whether a time stop, breakeven, partial close, trail, session exit or invalidation exit helps.
- An `mfe-mae-analysis` card proposed an exit experiment.

Don't use for: entry or parameter research (use the setup's research skill), or new exit rules that are not declared (those are a backtest-engineer card to add a variant, since every variant is a trial).

## Required inputs

- The strategy: `trend_pullback`, `session_breakout` or `liquidity_sweep`.
- Whether its entries passed T0. `list_runs(strategy=...)` shows each run's `passed` flag. T1 is gated on T0 (PRD §26): a run on entries that never passed T0 is exploratory and cannot pass.
- Optionally explicit entry `params`; by default the latest passing T0 run's final parameters are used, else the latest T0 run's.

## Procedure

1. Check history and budget with `list_runs(strategy=...)`. A T1 run counts against the same 20-per-month budget as T0, and adds one deflated-Sharpe trial per non-baseline variant.
2. Write down before running: which variant you expect to win and why (quote the MFE/MAE numbers behind it, e.g. `losers_reaching_1r`), and what would falsify it.
3. Run `run_exit_research(strategy=...)`. It simulates every declared variant on identical entries, walk-forward selects on dev (best expectancy among variants whose drawdown is within 10% of the baseline's), trades validation once, and applies the T1 gates.
4. Read the result:
   - `failed_gates` first. "Entry setup passed T0" failing means stop: report it and propose no exit change.
   - `segments`: chosen vs baseline expectancy on dev walk-forward OOS and validation. A gain that shows on one and not the other is not a finding.
   - `paired_gain`: the per-entry R difference on entries both took, with its bootstrap p05. A mean gain whose p05 is below zero is noise.
   - `variants`: every variant's dev and validation numbers and `exit_reasons`. Report the losers too.
5. Use `get_run_summary(run_id)` and `monte_carlo(run_id)` for the chosen exits' trade list if the card needs drawdown detail.
6. Propose at most one next step: adopt the chosen exits as a proposal for the operator, or a single new variant with its falsification rule.

## Tools

- atlas-backtest: `run_exit_research`, `list_runs`, `get_run_summary`, `monte_carlo`

## Output format

Strategy and entry source, expectation and falsification rule, result (run id, chosen variant, gains on dev OOS and validation, paired p05, drawdown ratio), a table of every variant, verdict, next step. Complete the card with the ATLAS metadata shape: `experiment_id, strategy_version, data_window, trades, expectancy_r, pf, max_dd_mc95, dsr, artifacts`.

## Safety constraints

- Never request holdout data; the research API refuses it and there is no way around that.
- Report every experiment you ran, including failed and exploratory runs.
- Output everything in R after costs, never currency or total return alone.
- Exits only ever tighten stops; never propose averaging down, widening a stop, grid or martingale exits.
- You never change live trade management or `config/strategies/`; a winning variant is a proposal the operator signs.

## Validation requirements

- The expectation and falsification rule were written before `run_exit_research` was called.
- Every run id you mention appears in `list_runs`.
- A T1 PASS claim comes from the run's gates with "Entry setup passed T0" true, never from the variant table alone.
