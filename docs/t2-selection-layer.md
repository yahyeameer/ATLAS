# Phase T2: selection layer

PRD §17 and §26: ML baseline, Jev, calibration and the EV gate. Exit gate:
**the best arm beats rules-only, and its Brier score beats the base rate.**

**Built ahead of its gate.** §26 puts T2 after T0 finds a setup with an
edge, and T0 round 1 found none. The owner chose to build T2 anyway, as with
T4. So T2 is built to work with any setup and has only been run on synthetic
data. Nothing in it assumes which setup passes. Running it on a real setup
waits for T0.

## What is built

| Piece | Where | Notes |
| --- | --- | --- |
| Decision state | `atlas_engine/decisions/state.py` | Built from the shared M15 feature frame and used by both research and live. It holds 15 numbers, each a distance in ATR or R, signed so positive favours the trade, plus a session label, a regime label and the setup name. It has no dates, prices or symbols (§17 leakage rule). |
| Regime labels | same | Uses six fixed labels: `trend`/`range` (H1 ADX ≥ 25) × `low`/`normal`/`high` vol (ATR percentile < 0.33 / > 0.80). |
| EV gate | `atlas_engine/decisions/ev.py` | `EV_R = p·R − (1−p) − C_R ≥ EV_min` (0.15 R). A missing, NaN, out-of-range or non-numeric p is a skip. |
| Calibration | `atlas_engine/calibration/` | An isotonic (PAV) fit, checked against scikit-learn, with an optional rolling window (§17: last 500 trades). It serialises to JSON for `calibration_models`. Also here: Brier, Brier skill, and a reliability table. Uses numpy only. |
| Jev adapter | `atlas_engine/adapters/jev/` | Enforces the §17 contract: a leakage guard on every request, pinned question wording (`atlas-jev-q1`), and no `jev-latest` in live. Timeouts (500 ms), errors, schema failures and out-of-range values become `Skip`. Answers are cached by request hash. **No network client or credentials**, only an injected transport and `ReplayTransport` for recorded answers. |
| Meta-labelling dataset | `atlas_research/selection/dataset.py` | Every rules signal is simulated on its own, so each one gets a label: target hit before stop. Adds `recent_signal_r20`, the mean R of the setup's last 20 signals already closed at decision time. |
| Arms | `atlas_research/selection/arms.py` | `GBMArm` is scikit-learn `HistGradientBoostingClassifier`, depth 3, seeded. Isotonic calibration is fitted on the later 25% of each training window. `JevArm` asks the adapter and calibrates its answers the same way. |
| Keep/kill harness | `atlas_research/selection/keep_kill.py` | Runs the arms on identical candidates. Each arm's accepted signals are re-simulated under the one-position rule. |
| T2 run + report | `atlas_research/t2.py`, `selection/report.py`, `configs/t2.yaml` | `atlas-research t2 run <strategy>` |

`session_label` moved from `atlas_research/metrics.py` into the shared
`atlas_engine/features/sessions.py`, so engine code can use it. `metrics.session_label`
still works.

## How a run works

1. **Candidates.** The setup's parameters come from `t2.yaml` if set there,
   else the final parameters of that strategy's latest T0 experiment, else the
   setup defaults. Signals over dev + validation go through the same edge
   filters and the same 1.5× spread fills as T0. Each gets its state, its own
   outcome, and a cost estimate in R: commission, both slippages and one
   night of swap. Spread is already in the fills.
2. **Walk-forward on dev.** This uses T0's folds: train 12 months, test 3. An
   arm trains only on candidates whose exit fell inside the training window,
   because a label isn't known until the trade closes. Folds with fewer than
   150 training candidates are skipped for every arm.
3. **Validation.** Each arm is fitted on all of dev and scored once on validation.
4. **Keep/kill** (§17) uses dev walk-forward plus validation together. An arm is kept only if all of these hold:
   - it has at least 300 trades;
   - its expectancy is above rules-only, with bootstrap P(arm > rules) ≥ 0.95;
   - its Brier score beats the training-window base rate.
   Jev must also beat the GBM arm on the same bootstrap test. The phase gate
   passes when the best arm is kept.
5. **Registry.** Each run is recorded, pass or fail, under
   `<strategy>+selection` in `research/experiments.jsonl`. Every arm counts as
   one trial for that entry's deflated Sharpe. T0's trial count is not touched.

Data loads through the same holdout-guarded loader as T0, and the segments
end where the holdout starts.

```bash
pip install -e '.[selection]'
atlas-research t2 run trend_pullback                     # real data, once T0 has one
atlas-research data synthetic --data-root /tmp/syn       # dry run
atlas-research t2 run trend_pullback --data-root /tmp/syn --registry /tmp/syn/exp.jsonl --out /tmp/syn/runs
```

## Experiments run while building (all synthetic, none in the real registry)

| Run | Candidates | Rules-only | Rules + GBM | Verdict |
| --- | --- | --- | --- | --- |
| trend_pullback, random walk 2019–2022, EURUSD | 809 | 494 trades, −0.047 R | 48 trades, +0.275 R, kept 8%, P(beats rules) 0.93, Brier skill −0.04 | Not kept (too few trades, no Brier skill). The +0.275 R is noise that the criteria rejected. |
| Harness test, planted edge (target-first 0.48 when HTF-aligned, 0.20 otherwise) | 6,000 | < 0 R | > +0.3 R, keeps 30–70% | Kept |
| Harness test, no edge (constant 0.32) | 6,000 | < 0 R | — | Killed on Brier |
| Harness test, Jev that sees the same edge as GBM | 3,000 | — | — | Jev killed: it doesn't beat GBM |

## Not done

- **A real run.** This waits for a T0 setup that passes. Running T2 on the
  three failed round-1 setups would spend selection trials on setups §26 has
  already sent back to research.
- **Jev itself.** There is no vendor client, endpoint or key in the repo. The
  engine host's deployment supplies a transport. The first Jev run should
  record its answers so `ReplayTransport` can replay them reproducibly.
- **Engine wiring.** T4 calls `state_frame` → arm/calibrator → `EVGate.decide`
  after the rules fire and before `RiskEngine`. The weekly calibration refit
  and the Brier/reliability dashboard panel belong to T4/H3, and so does the
  `calibration-analysis` skill (H2 deferred it to T2).
- **News distance.** §16 lists it as a regime input, but there is no news
  calendar yet.
- **2× spread stress on the selected trades.** T0 gates it. T2 reports only the base stress.
