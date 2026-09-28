# Phase T1: exit research

PRD §26: MFE/MAE logging and exit variants, run via Kanban. Exit gate:
**an exit variant beats the fixed baseline out of sample.**

The PRD gates T1 on T0 finding an edge. T0 round 1 found none, and the owner
chose to build T1 ahead of that, as with T4. So everything here is
strategy-agnostic: the exit rules work on any setup's entries, and the gate
refuses to pass a setup that never passed T0. Once a setup passes T0, T1 is
one command away.

## What was built

| Piece | Where |
| --- | --- |
| Managed-exit simulator with every §18 rule, same fill model as T0 | `atlas_research/exits.py` |
| Eight exit variants, declared before any result | `atlas_research/configs/t1.yaml` |
| T1 runner: same entries for every variant, dev walk-forward, validation once, gates, registry, report | `atlas_research/t1.py`, `atlas-research t1 run` |
| Research API route and MCP tool, so Kanban cards can run it | `backtest/exit_research`, `atlas-backtest.run_exit_research` |
| Skill for strategy-researcher and backtest-engineer | `atlas-skills/exit-research/` |
| Tests | `tests/research/test_exits.py`, `tests/research/test_t1_end_to_end.py`, `tests/mcp/test_service.py` |

MFE and MAE were already logged per trade by the T0 simulator and served by
`atlas-journal.mfe_mae` (H2). T1 adds `bars_held`, `stop_moves` and
`partial_r` to each trade, and a baseline MFE/MAE summary to every report.

## Exit rules

| §18 rule | Variant | How it is simulated |
| --- | --- | --- |
| Fixed SL/TP | `fixed_2r` (baseline) | Stop from the setup, target 2R from the actual fill. Identical to the T0 simulator; a test holds them together trade for trade. |
| Time stop | `time_stop` | At the 12th M15 close, close at market if MFE never reached +0.5R. |
| Breakeven | `breakeven` | Once MFE reaches +1R at an M15 close, stop to entry plus commission and stop slippage, so a breakeven exit nets zero. |
| Partial close | `partial_trail` | Broker-side take-profit for 50% at +1R; the rest trails 2×ATR from +1R, no fixed target. |
| ATR trail | `atr_trail` | After +1.5R, stop at the best exit-side price minus 2×ATR(M15). No fixed target. |
| Structure trail | `structure_trail` | After +1R, stop just beyond the last confirmed M15 swing (lowest of 5 bars, known 2 bars later). No fixed target. |
| Session exit | `session_exit` | Flat from 16:45 New York, before the rollover. Friday 20:00 UTC flatten applies to every variant, as in T0. |
| Invalidation | `invalidation` | Close at market when the closed-H1 trend flips against the position. A counter-trend entry, like a sweep reversal, isn't closed by the trend it was already fading. |

Management follows §18. Initial stop, target and partial are broker-side and
trigger inside the M1 bar. Everything else decides at an M15 close on closed
bars only, and a market close fills at the next M1 open. Stops only tighten,
move at most once per M15 bar (the tightest of breakeven and the trails
wins), and never to a price the market is already through. A stop and a
partial or target in the same minute count as the stop. Stops hit after a
move are labelled `managed_stop`, so they're kept apart from initial stops.

## How a run works

1. **Entries.** One set of signals for the strategy, from explicit `params`,
   else the latest T0 run that passed, else the latest T0 run, else the
   setup defaults. The report says which. Every variant trades these same
   signals; only the one-position rule can make the trade lists differ.
2. **Walk-forward on dev (2019–2023).** Each 12-month train window picks the
   variant with the best expectancy among those whose realised drawdown in R
   is within 10% of the baseline's (§18: expectancy and drawdown together).
   The pick trades the next 3 months, and so does the baseline.
3. **Validation (Jan 2024–Jun 2025).** The variant picked on all of dev
   trades validation once, beside the baseline.
4. **Gates**, then a registry entry (`kind: exit_research`) whether it
   passed or not. Each non-baseline variant's dev Sharpe is a trial, so it
   raises the bar for the strategy's deflated Sharpe in later T0 and T1 runs.

## Gates

| Gate | Threshold | Scope | Source |
| --- | --- | --- | --- |
| Entry setup passed T0 | a passing T0 run on record | registry | §26 |
| Expectancy gain vs fixed baseline | > 0 R | dev walk-forward OOS | §26 |
| Expectancy gain vs fixed baseline | > 0 R | validation | §26 |
| MC p95 drawdown, chosen ÷ baseline | ≤ 1.10 | all OOS | §18, ATLAS threshold |
| Expectancy gain at 2× spread | > 0 R | all OOS | §15, ATLAS threshold |
| Paired gain per shared entry, bootstrap p05 | > 0 R | all OOS | ATLAS default |
| Chosen expectancy after costs | ≥ +0.10 R | all OOS | §15 |
| Deflated Sharpe, exit trials counted | > 0.95 | all OOS | §15 |

The PRD's gate is only "beats fixed baseline OOS". On a random walk, a trailing
exit sometimes beats the baseline on both dev and validation by luck, so a
difference in means alone would pass noise. The paired bootstrap compares the
two exits entry by entry. The last two gates make sure the improved strategy
still meets §15 with the extra trials counted.

## Checked on synthetic data

| Data | Result |
| --- | --- |
| Random walk, T0 pass faked in the registry | Fails every gate except "passed T0" |
| Random walk with 15-minute momentum (0.35), T0 pass faked | Passes; `atr_trail` chosen, +0.85 R vs +0.13 R for the baseline over dev |
| Same momentum data, no T0 pass | Fails only "Entry setup passed T0" |

These are `tests/research/test_t1_end_to_end.py`. Synthetic runs never touch
the real registry: the CLI refuses a synthetic data root with the default
registry path.

## Running it

```bash
atlas-research t1 run trend_pullback                    # entries from the latest T0 run
atlas-research t1 run session_breakout --params '{"mode": "retest"}'
atlas-research t1 run --all
```

Output goes to `research/runs/<experiment_id>/`: `report.md`, `summary.json`,
the chosen exits' OOS trades (`oos_trades.csv`, readable through
`atlas-journal`) and the baseline's (`baseline_oos_trades.csv`). From a
Kanban card, `run_exit_research(strategy=...)` does the same through the
research API, under the `backtest:run` scope and the monthly budget.

## Not covered yet

- **Not run on real data.** All three T0 setups failed round 1, so a real T1
  run could only fail its first gate. Run it once a setup passes T0 (round 2
  on H1 bars is in progress).
- **M15 management only.** If T0 round 2 moves decisions to H1, the time stop
  and trails still manage on M15 closes; add an H1 option then if needed.
- **Invalidation uses the H1 EMA 20/50 trend**, which rarely flips within a
  2R trade's life. Faster definitions would be new, declared variants.
- **No stop/freeze-level checks.** Broker minimum stop distances arrive with
  the MT5 contract specs in T4.
- **Live trade management** in the engine is T4's; it should call the same
  rules so backtest and live match (§22 replay ≥ 95% in T5).
