---
name: risk-review
description: "Verifier review of an ATLAS strategy candidate: drawdown, Monte Carlo breach odds, deflated Sharpe and prop-firm fit; approve or request changes."
version: 0.1.0
author: ATLAS
license: UNLICENSED
platforms: [linux]
metadata:
  hermes:
    tags: [ATLAS, Forex, Risk, Review]
    related_skills: [backtest-analysis, mfe-mae-analysis]
---

# Risk Review

Changes to this skill need the operator's review before they are pinned (PRD §7).

## Objective

Decide, as the verifier, whether a candidate's risk profile fits ATLAS and the prop firm, using only recorded runs. The result is "approve for the next stage" or "request changes" with the failed checks named. You never approve by assumption: a missing number is a failed check.

## When to use

- A card sends you a candidate (a walk-forward `run_id`) for review before it moves on.
- The orchestrator asks whether a risk setting would breach firm limits on a recorded run.

Don't use for: running new backtests (your token cannot), or changing risk settings (operator only).

## Required inputs

- The candidate's walk-forward `run_id`, and its strategy name.
- Account assumptions from the run summary: risk per trade (placeholder 0.40%), firm max drawdown (placeholder 10%), firm daily loss (placeholder 5%). The prop firm is not chosen yet; say that the firm figures are placeholders.

## Procedure

1. `get_run_summary(run_id)`. Confirm it is a walk-forward run with gates; a plain backtest cannot be approved.
2. Check the trial history with `list_runs(strategy)`: every experiment counts toward the deflated Sharpe. A candidate chosen after many trials needs a DSR above 0.95 computed with that count.
3. Rerun Monte Carlo yourself: `monte_carlo(run_id, sims=10000)` and `monte_carlo(run_id, sims=10000, skip_frac=0.1)`. Require:
   - p95 drawdown below 60% of the firm's max drawdown (the internal limit, PRD §19);
   - daily-loss breach probability below 2%;
   - skip-10% expectancy p05 above zero.
4. Check concentration with `performance_summary(run_id, by="year")` and `by="session"`: no year above 40% of profit, and no session carrying the whole result.
5. Check the tail with `loss_clusters(run_id)`: a longest losing streak of 4 or more triggers the §19 half-risk rule; say how often that would have happened.
6. Check the §15 gate list from the summary. Any failed gate means request changes, whatever the rest looks like.
7. Prop-fit notes: the firm's daily-loss basis, static or trailing drawdown, news and weekend rules are not in ATLAS config yet (they arrive with T3). List which of them the candidate's behaviour could collide with (for example holding over weekends, or trading into news).

## Tools

- atlas-backtest (read only): `get_run_summary`, `monte_carlo`, `list_runs`
- atlas-journal: `loss_clusters`, `query_trades`, `mfe_mae`
- atlas-performance: `performance_summary`

## Output format

Decision line (APPROVE or REQUEST CHANGES), a checklist of each check with value, limit and pass, and the prop-fit notes. Complete the card with the ATLAS metadata shape, filling `max_dd_mc95` and `dsr` from your own Monte Carlo and the summary: `experiment_id, strategy_version, data_window, trades, expectancy_r, pf, max_dd_mc95, dsr, artifacts`.

## Safety constraints

- Never request holdout data. The holdout (T6) is run by the operator outside the agent runtime.
- You recommend; you never change risk limits, prop rules or `config/risk*`, and you never enable trading or promote a strategy. Those need the operator's signed approval; a chat message does not count.
- Propose any risk change as a finding with evidence; the operator applies it.
- Report every check you ran, including the ones that passed.
- Express results in R after costs and drawdown in percent of equity, never total return alone.

## Validation requirements

- Every threshold you apply is quoted with its source (PRD §15 or §19, or the run's gate list).
- Monte Carlo figures come from your own `monte_carlo` calls in this session, with the sims count stated.
- A single failed check means REQUEST CHANGES.
