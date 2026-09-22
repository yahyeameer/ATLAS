# Phase T0 — edge discovery

PRD §26: data pipeline, shared features, backtester, 2–3 setups. Exit gate:
**one setup passes the dev and validation gates.** If none passes, stop and
research more (§26); the engine is not built around a strategy without edge.

## Status

| Piece | State |
| --- | --- |
| Dukascopy M1 bid/ask downloader, cache, decoder | Built; not yet run against the live feed (see Blockers) |
| MT5 "Export bars" importer | Built (bid + spread column, server time NY+7) |
| Bid/ask M1 store (parquet per symbol-year), M15/H1 resampling, quality report | Built |
| Shared feature library (EMA, ATR, ADX, ATR percentile, H1 context, sessions, prior-day and Asian ranges) | Built, causal (tested) |
| Setups: trend pullback, session breakout (momentum / retest), liquidity sweep | Built, v0.1.0 |
| Bid/ask M1 trade simulator with stressed spread, commission, slippage, swap | Built |
| Walk-forward, validation pass, §15 gates, §22 robustness checks | Built |
| Experiment registry with monthly budget and trial counting for the deflated Sharpe | Built |
| Run on real 2019–mid-2025 data | **Blocked on data access** |

## Blockers

- **Market data.** The cloud environment that built this cannot reach
  `datafeed.dukascopy.com` (nor any other market-data host). Either allow that
  domain for the environment, run the two `data` commands below on a machine
  that can, or import MT5 exports with `data import-mt5`.

## Running it

```bash
pip install -e '.[dev]'
atlas-research data download --symbols EURUSD GBPUSD   # 2019-01-01 .. 2025-06-30, resumable
atlas-research data build    --symbols EURUSD GBPUSD   # -> data/m1/<SYMBOL>/<YEAR>.parquet
atlas-research data quality  --symbols EURUSD GBPUSD   # gaps, crossed quotes, spread stats per year
atlas-research t0 run --all                            # -> research/runs/<experiment_id>/report.md
```

Each run appends to `research/experiments.jsonl` (committed) and writes a
report, a summary JSON with the §4 Kanban metadata shape, and the
out-of-sample trade list under `research/runs/` (not committed). For a dry
run without market data:

```bash
atlas-research data synthetic --data-root /tmp/syn
atlas-research t0 run --all --data-root /tmp/syn --registry /tmp/syn/exp.jsonl --out /tmp/syn/runs
```

Synthetic runs are refused against the real registry so they never count as trials.

## How a run works

1. **Data.** M1 bid and ask candles, UTC, from 2019-01-01 up to the day before
   the holdout. Dukascopy's M1 candles stand in for the tick/1-second data the
   PRD names: they keep both sides and a per-minute spread, which is enough
   for M15 decisions. Tick replay can replace them later without touching the
   setups.
2. **Features.** One M15 frame per symbol, built from M1. A row holds what is
   known at the bar's close; H1 context comes from the last *closed* hour.
   The same module is meant to run live (§22 "one shared feature library").
3. **Signals.** Setups decide at the M15 close and give a direction and a stop
   beyond structure, 1.0–1.5 × ATR(M15) (§18 baseline). Shared edge filters
   (§16): spread ≤ 20% of stop, no entries in the first 15 minutes after the
   London or New York open (except the session breakout), none in the
   16:45–18:15 New York rollover, none after 18:00 UTC Friday.
4. **Fills.** Entry at the next M1 open: buy at ask, sell at bid. Spread
   stressed ×1.5 around the mid. Target 2R from the actual fill. Longs exit on
   the bid, shorts on the ask; stop and target in the same minute count as a
   stop; a gap through the stop fills at the open. Friday 20:00 UTC flatten.
   Commission, slippage and swap (Wednesday triple) are charged in R.
5. **Walk-forward on dev (2019–2023).** Every grid point is backtested once;
   each 12-month train window picks the best expectancy and trades the next
   3 months. Walk-forward efficiency = OOS R per year ÷ in-sample R per year.
6. **Validation (Jan 2024–Jun 2025).** Final parameters are chosen on all of
   dev and traded on validation once.
7. **Gates** (below), then the experiment is appended to the registry whether
   it passed or not.

## Gates

"All OOS" means the dev walk-forward test windows plus validation.

| Gate | Threshold | Scope | Source |
| --- | --- | --- | --- |
| OOS trades | ≥ 300 | all OOS | §15 |
| Expectancy after costs (1.5× spread) | ≥ +0.10 R | dev OOS **and** validation | §15 |
| Profit factor | ≥ 1.25 | dev OOS **and** validation | §15 |
| Max drawdown, Monte Carlo p95 (10,000 reshuffles) | < 60% of firm max DD | all OOS | §15 |
| Daily-loss breach probability per evaluation | < 2% | all OOS | §15 |
| Deflated Sharpe ratio (every registered trial counted) | > 0.95 | all OOS | §15 |
| Walk-forward efficiency | ≥ 50% | dev | §15 |
| Expectancy at 2× spread | > 0 | all OOS | §15 |
| Largest single-year share of profit | ≤ 40% | all OOS | §22 |
| Skip-10% Monte Carlo expectancy, 5th percentile | > 0 | all OOS | §22 check, ATLAS threshold |
| Expectancy minus random-entry control p95 | > 0 | all OOS | §22 check, ATLAS threshold |
| Worst ±20% parameter neighbour expectancy | > 0 | validation | §22 check, ATLAS threshold |

Calibration (Brier) and paper drift are T2 and T7 gates and are not part of T0.

## Defaults picked where the PRD is open

All of these live in `atlas_research/configs/t0.yaml`, not in code.

- **Timeframe:** M15 decisions with H1 context, as §16 describes (open question: M15 vs H1).
- **Account for drawdown and breach gates:** 0.40% risk per trade, 10% firm max drawdown, 5% firm daily loss, 30-day evaluation. Replace once the prop firm is chosen.
- **Costs:** $7/lot round-turn commission, 0.1 pip entry and 0.2 pip stop slippage, 0.5–0.6 pip swap per night charged to both sides. Replace with real fills once T4 cost tracking exists.
- **Liquidity sweep** runs on EURUSD only; §16 requires separate validation before GBPUSD or XAUUSD.
- **Grids** are 4 points per setup, declared before any result, so the deflated Sharpe stays honest.

## Not covered yet

- **News blackout** (§16): no economic-calendar source is wired in yet, so the ±10/15-minute high-impact news filter is not applied. Results will be slightly optimistic around releases.
- **Regime filter** beyond each setup's own ADX/EMA conditions, and the ATR-percentile gate, are computed but not yet used as filters.
- **Floating loss** is not modelled in the daily-breach Monte Carlo (closed P/L only); the §19 internal buffers cover the gap.
- **Holdout** guard is in research code only. The real refusal is server-side in `atlas-backtest` (Phase H2).
