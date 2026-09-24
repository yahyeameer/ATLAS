"""Phase T0 edge-discovery run: walk-forward on dev, one pass on validation, gates.

For one strategy (a setup applied to its symbols) this:

1. backtests every grid point over dev + validation at 1.5x spread (signals
   are causal, so one pass sliced by entry time equals per-window runs);
2. walk-forward on dev: each 12-month train window picks the grid point with
   the best expectancy, which then trades the next 3-month test window;
3. picks final parameters on the whole dev period and trades validation once;
4. evaluates the §15 gates plus the §22 robustness checks;
5. appends the experiment, pass or fail, to the registry.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import itertools
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd

from atlas_engine.features import sessions
from atlas_engine.features.frame import FeatureConfig, build_features
from atlas_engine.setups import SETUPS, EdgeFilters, Setup

from . import metrics
from .backtest import TRADE_COLS, CostModel, ExitPolicy, M1Path, simulate
from .registry import Registry

Window = tuple[pd.Timestamp, pd.Timestamp]


@dataclass
class Market:
    symbol: str
    m1: pd.DataFrame
    features: pd.DataFrame
    costs: CostModel
    paths: dict[float, M1Path]


def _ts(x) -> pd.Timestamp:
    t = pd.Timestamp(x)
    return t.tz_localize("UTC") if t.tzinfo is None else t


def expand_grid(grid: dict) -> list[dict]:
    keys = list(grid)
    return [dict(zip(keys, combo)) for combo in itertools.product(*(grid[k] for k in keys))] or [{}]


def in_window(trades: pd.DataFrame, w: Window) -> pd.DataFrame:
    t = pd.DatetimeIndex(trades["entry_time"])
    return trades.loc[(t >= w[0]) & (t < w[1])]


def walk_forward_folds(dev: Window, train_months: int, test_months: int) -> list[tuple[Window, Window]]:
    folds = []
    start = dev[0]
    while True:
        split = start + pd.DateOffset(months=train_months)
        end = split + pd.DateOffset(months=test_months)
        if end > dev[1]:
            return folds
        folds.append(((start, split), (split, end)))
        start = start + pd.DateOffset(months=test_months)


class Runner:
    """Backtests one setup across markets, caching by (params, spread multiple)."""

    def __init__(self, setup: Setup, markets: list[Market], exits: ExitPolicy, filters: EdgeFilters):
        self.setup, self.markets, self.exits, self.filters = setup, markets, exits, filters
        self._cache: dict = {}

    def trades(self, params: dict, spread_mult: float) -> pd.DataFrame:
        key = (json.dumps(params, sort_keys=True), spread_mult)
        if key not in self._cache:
            parts = []
            for m in self.markets:
                sig = self.setup.signals(m.features, params, self.filters)
                parts.append(simulate(sig, m.m1, m.costs.with_spread(spread_mult), self.exits, m.symbol, self._path(m, spread_mult)))
            self._cache[key] = _concat(parts)
        return self._cache[key]

    def simulate_signals(self, signals_by_symbol: dict[str, pd.DataFrame], spread_mult: float) -> pd.DataFrame:
        parts = []
        for m in self.markets:
            sig = signals_by_symbol.get(m.symbol)
            if sig is not None and len(sig):
                parts.append(simulate(sig, m.m1, m.costs.with_spread(spread_mult), self.exits, m.symbol, self._path(m, spread_mult)))
        return _concat(parts)

    @staticmethod
    def _path(m: Market, spread_mult: float) -> M1Path:
        if spread_mult not in m.paths:
            m.paths[spread_mult] = M1Path(m.m1, spread_mult)
        return m.paths[spread_mult]


def _concat(parts: list[pd.DataFrame]) -> pd.DataFrame:
    parts = [p for p in parts if len(p)]
    if not parts:
        return pd.DataFrame(columns=TRADE_COLS)
    return pd.concat(parts, ignore_index=True).sort_values("entry_time", ignore_index=True)


def prepare_market(symbol: str, m1: pd.DataFrame, cfg: dict, bar: str = "15min") -> Market:
    c = cfg["costs"]["per_symbol"][symbol]
    costs = CostModel(spread_mult=cfg["costs"]["spread_mult"], **c)
    return Market(symbol, m1, build_features(m1, FeatureConfig(bar=bar)), costs, {})


def select(trades_by_point: list[pd.DataFrame], w: Window, min_trades: int) -> int | None:
    best, best_exp = None, -np.inf
    for i, t in enumerate(trades_by_point):
        tw = in_window(t, w)
        if len(tw) >= min_trades and tw["r"].mean() > best_exp:
            best, best_exp = i, tw["r"].mean()
    return best


def neighbours(params: dict, frac: float) -> list[dict]:
    out = []
    for k, v in params.items():
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v == 0:
            continue
        for sign in (-1, 1):
            nv = v * (1 + sign * frac)
            if isinstance(v, int):
                nv = int(round(nv))
                if nv == v:
                    nv = v + sign
            out.append({**params, k: nv})
    return out


def random_control(runner: Runner, reference: pd.DataFrame, windows: list[Window], runs: int, seed: int) -> np.ndarray:
    """Expectancy of random entries with the same count, direction mix, stop sizes and exits."""
    rng = np.random.default_rng(seed)
    if reference.empty:
        return np.array([])
    stop_atr = (reference["risk"] / reference["atr"]).to_numpy()
    long_share = float((reference["direction"] == 1).mean())
    per_symbol = reference["symbol"].value_counts().to_dict()
    pools = {}
    for m in runner.markets:
        f = m.features
        ct = pd.DatetimeIndex(f["close_time"])
        ok = f["atr"].notna().to_numpy() & ~f["blk_rollover"].to_numpy() & ~sessions.friday_cutoff(ct, runner.filters.friday_no_entry_after_utc)
        inw = np.zeros(len(f), bool)
        for a, b in windows:
            inw |= (ct >= a) & (ct < b)
        pools[m.symbol] = f.loc[ok & inw]
    out = []
    for _ in range(runs):
        sigs = {}
        for sym, n in per_symbol.items():
            pool = pools.get(sym)
            if pool is None or pool.empty:
                continue
            rows = pool.iloc[np.sort(rng.choice(len(pool), size=min(n, len(pool)), replace=False))]
            d = np.where(rng.random(len(rows)) < long_share, 1, -1)
            k = rng.choice(stop_atr, size=len(rows))
            sigs[sym] = pd.DataFrame(
                {
                    "decision_time": rows["close_time"].to_numpy(),
                    "direction": d,
                    "stop": rows["close"].to_numpy() - d * k * rows["atr"].to_numpy(),
                    "atr": rows["atr"].to_numpy(),
                    "spread": rows["spread"].to_numpy(),
                    "setup": "random_control",
                }
            )
        t = runner.simulate_signals(sigs, runner.markets[0].costs.spread_mult)
        out.append(t["r"].mean() if len(t) else 0.0)
    return np.array(out)


def gate(name: str, value: float, op: str, threshold: float, scope: str, source: str = "PRD §15") -> dict:
    passed = {">=": value >= threshold, ">": value > threshold, "<": value < threshold, "<=": value <= threshold}[op]
    return {"gate": name, "value": value, "rule": f"{op} {threshold:g}", "passed": bool(passed), "scope": scope, "source": source}


def run_t0(
    strategy: str,
    cfg: dict,
    load_m1: Callable[[str, pd.Timestamp, pd.Timestamp], pd.DataFrame],
    registry: Registry,
    out_dir: Path | None = None,
    now: dt.datetime | None = None,
    seed: int = 0,
) -> dict:
    now = now or dt.datetime.now(dt.timezone.utc)
    scfg = cfg["strategies"][strategy]
    registry.check_budget(strategy, cfg["budget"]["experiments_per_strategy_per_month"], now)
    # A strategy is a setup plus its decision timeframe; variants share a setup.
    setup_name = scfg.get("setup", strategy)
    setup = SETUPS[setup_name]
    bar = scfg.get("bar", "15min")
    g = cfg["gates"]
    acct = cfg["account"]
    dev: Window = tuple(_ts(x) for x in cfg["segments"]["dev"])
    val: Window = tuple(_ts(x) for x in cfg["segments"]["validation"])

    markets = [prepare_market(s, load_m1(s, dev[0], val[1]), cfg, bar) for s in scfg["symbols"]]
    exits = ExitPolicy(rr=cfg["exits"]["rr"], friday_flatten_utc=cfg["exits"]["friday_flatten_utc"])
    filters = EdgeFilters(**cfg["filters"])
    runner = Runner(setup, markets, exits, filters)
    base_mult = cfg["costs"]["spread_mult"]
    stress_mult = cfg["costs"]["stress_spread_mult"]

    grid = [{**setup.defaults, **p} for p in expand_grid(scfg.get("grid", {}))]
    by_point = [runner.trades(p, base_mult) for p in grid]
    trial_sharpes = [metrics.sharpe(in_window(t, dev)["r"].to_numpy()) for t in by_point]

    # Walk-forward on dev.
    wf = cfg["walk_forward"]
    fold_rows, oos_parts, oos_stress_parts, oos_windows = [], [], [], []
    is_r = is_years = oos_r = oos_years = 0.0
    for train, test in walk_forward_folds(dev, wf["train_months"], wf["test_months"]):
        pick = select(by_point, train, wf["min_train_trades"])
        row = {"train": f"{train[0]:%Y-%m}..{train[1]:%Y-%m}", "test": f"{test[0]:%Y-%m}..{test[1]:%Y-%m}", "pick": pick}
        if pick is not None:
            tr, te = in_window(by_point[pick], train), in_window(by_point[pick], test)
            oos_parts.append(te)
            oos_windows.append(test)
            oos_stress_parts.append(in_window(runner.trades(grid[pick], stress_mult), test))
            is_r += tr["r"].sum()
            is_years += (train[1] - train[0]).days / 365.25
            oos_r += te["r"].sum()
            oos_years += (test[1] - test[0]).days / 365.25
            row.update(train_expectancy=tr["r"].mean(), test_trades=len(te), test_expectancy=te["r"].mean() if len(te) else 0.0)
        fold_rows.append(row)
    wf_oos = _concat(oos_parts)
    wfe = (oos_r / oos_years) / (is_r / is_years) if is_years and oos_years and is_r > 0 else 0.0

    # Final parameters from the whole dev period, traded once on validation.
    final_idx = select(by_point, dev, wf["min_train_trades"])
    final = grid[final_idx] if final_idx is not None else grid[0]
    val_trades = in_window(runner.trades(final, base_mult), val)
    val_stress = in_window(runner.trades(final, stress_mult), val)

    oos = _concat([wf_oos, val_trades])
    oos_stress = _concat(oos_stress_parts + [val_stress])
    r = oos["r"].to_numpy(float)

    # Every variant of a setup counts toward its deflated Sharpe trials.
    prior = registry.trial_sharpes(setup=setup_name)
    all_trials = prior + trial_sharpes
    n_trials = len(all_trials)
    var_trials = float(np.var(all_trials, ddof=1)) if n_trials > 1 else 0.0
    dsr = metrics.deflated_sharpe(r, n_trials, var_trials)

    risk_pct = acct["risk_pct"]
    mc = metrics.mc_drawdown(r, risk_pct, g["mc_sims"], seed)
    mc_skip = metrics.mc_drawdown(r, risk_pct, g["mc_sims"], seed + 1, skip_frac=0.10)
    breach = metrics.daily_breach_probability(oos, risk_pct, acct["firm_daily_loss_pct"], acct["eval_days"], g["mc_sims"], seed)
    years = metrics.breakdown(oos, "year")
    max_year_share = float(years["share_of_profit"].max()) if len(years) and r.sum() > 0 else 1.0

    rand = random_control(runner, oos, oos_windows + [val], g["random_control_runs"], seed)
    rand_p95 = float(np.percentile(rand, 95)) if len(rand) else 0.0
    nbr = [in_window(runner.trades(p, base_mult), val)["r"].mean() for p in neighbours(final, g["neighborhood"])]
    nbr = [0.0 if np.isnan(x) else float(x) for x in nbr]

    dev_s, val_s = metrics.summary(wf_oos), metrics.summary(val_trades)
    ours = "ATLAS default (PRD names the check, not a threshold)"
    gates = [
        gate("OOS trades", len(oos), ">=", g["min_oos_trades"], "dev walk-forward + validation"),
        gate("Expectancy after costs (R)", dev_s["expectancy_r"], ">=", g["min_expectancy_r"], "dev walk-forward OOS"),
        gate("Expectancy after costs (R)", val_s["expectancy_r"], ">=", g["min_expectancy_r"], "validation"),
        gate("Profit factor", dev_s["profit_factor"], ">=", g["min_profit_factor"], "dev walk-forward OOS"),
        gate("Profit factor", val_s["profit_factor"], ">=", g["min_profit_factor"], "validation"),
        gate("Max DD, Monte Carlo p95 (% equity)", mc["dd_p95_pct"], "<", g["max_dd_frac_of_firm"] * acct["firm_max_drawdown_pct"], "all OOS"),
        gate("Daily-loss breach probability", breach, "<", g["max_daily_breach_prob"], "all OOS"),
        gate("Deflated Sharpe ratio", dsr, ">", g["min_dsr"], f"all OOS, {n_trials} trials"),
        gate("Walk-forward efficiency", wfe, ">=", g["min_wfe"], "dev"),
        gate("Expectancy at 2x spread (R)", float(oos_stress["r"].mean()) if len(oos_stress) else 0.0, ">", 0.0, "all OOS"),
        gate("Largest single-year share of profit", max_year_share, "<=", g["max_year_share"], "all OOS", "PRD §22"),
        gate("Skip-10% Monte Carlo expectancy p05 (R)", mc_skip["expectancy_p05"], ">", 0.0, "all OOS", ours),
        gate("Expectancy minus random-entry p95 (R)", float(r.mean() - rand_p95) if len(r) else 0.0, ">", 0.0, "all OOS", ours),
        gate("Worst ±20% neighbour expectancy (R)", min(nbr) if nbr else 0.0, ">", 0.0, "validation", ours),
    ]
    passed = all(x["passed"] for x in gates)

    exp_id = f"{strategy}-{now:%Y%m%d-%H%M%S}-" + hashlib.sha1(json.dumps([scfg, cfg["segments"]], sort_keys=True, default=str).encode()).hexdigest()[:6]
    result = {
        "experiment_id": exp_id,
        "strategy": strategy,
        "setup": setup_name,
        "bar": bar,
        "strategy_version": setup.version,
        "created_at": now.isoformat(),
        "hypothesis": scfg.get("hypothesis", ""),
        "symbols": scfg["symbols"],
        "data_window": {"dev": [str(dev[0].date()), str(dev[1].date())], "validation": [str(val[0].date()), str(val[1].date())]},
        "grid": scfg.get("grid", {}),
        "trial_sharpes": trial_sharpes,
        "final_params": final,
        "passed": passed,
        "gates": gates,
        "dev_oos": dev_s,
        "validation": val_s,
        "walk_forward": {"efficiency": wfe, "folds": fold_rows},
        "monte_carlo": {**mc, "skip10": mc_skip, "daily_breach_prob": breach},
        "dsr": {"value": dsr, "n_trials": n_trials, "var_trials": var_trials},
        "random_control": {"runs": len(rand), "mean": float(rand.mean()) if len(rand) else 0.0, "p95": rand_p95},
        "neighbours": nbr,
        # Kanban handoff shape from PRD §4.
        "kanban_metadata": {
            "experiment_id": exp_id,
            "strategy_version": setup.version,
            "data_window": "dev+validation",
            "trades": len(oos),
            "expectancy_r": float(r.mean()) if len(r) else 0.0,
            "pf": metrics.profit_factor(r),
            "max_dd_mc95": mc["dd_p95_pct"],
            "dsr": dsr,
            "artifacts": [],
        },
    }
    registry.append({k: result[k] for k in (
        "experiment_id", "strategy", "setup", "bar", "strategy_version", "created_at", "hypothesis", "symbols", "data_window",
        "grid", "trial_sharpes", "final_params", "passed", "kanban_metadata")} | {"failed_gates": [x["gate"] + " / " + x["scope"] for x in gates if not x["passed"]]})

    if out_dir is not None:
        from .report import write_report

        run_dir = Path(out_dir) / exp_id
        result["kanban_metadata"]["artifacts"] = write_report(run_dir, result, oos, years, metrics.breakdown(oos, "session"), metrics.breakdown(oos, "symbol"))
    return result
