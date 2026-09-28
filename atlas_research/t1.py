"""Phase T1 exit research: exit variants against the fixed 2R baseline (PRD §18, §26).

For one strategy at fixed entry parameters this:

1. generates the setup's signals once, so every variant trades the same entries;
2. simulates every declared exit variant over dev + validation at 1.5x spread;
3. walk-forward on dev: each 12-month train window picks the variant with the
   best expectancy among those whose drawdown is not materially worse than
   the baseline's (§18: expectancy and drawdown together), which then trades
   the next 3-month window; the baseline trades the same windows;
4. picks the variant on the whole of dev and trades validation once;
5. evaluates the T1 gate ("beats fixed baseline OOS") and appends the
   experiment, pass or fail, to the registry. Each non-baseline variant is a
   trial for the strategy's deflated Sharpe.

Entry parameters come from the latest T0 run that passed, else the latest T0
run, else the setup defaults. A strategy that never passed T0 can be run but
never passes T1: exits are only worth tuning on entries with an edge.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd

from atlas_engine.setups import SETUPS, EdgeFilters

from . import metrics
from .backtest import M1Path
from .exits import MANAGED_COLS, ExitSpec, ManagementFrame, simulate_managed
from .registry import Registry
from .t0 import Market, Window, _ts, gate, in_window, prepare_market, walk_forward_folds

DEFAULT_T1_CONFIG = Path(__file__).parent / "configs" / "t1.yaml"
KIND = "exit_research"


def load_variants(t1cfg: dict) -> dict[str, ExitSpec]:
    specs = {name: ExitSpec.from_dict(name, d) for name, d in (t1cfg.get("variants") or {}).items()}
    if t1cfg["baseline"] not in specs:
        raise ValueError(f"baseline variant {t1cfg['baseline']!r} is not declared")
    return specs


def t0_entries(registry: Registry, strategy: str) -> list[dict]:
    """Full T0 walk-forward runs (single backtests and exit research excluded)."""
    return [e for e in registry.entries(strategy) if e.get("kind", "walk_forward") == "walk_forward"]


def entry_params(registry: Registry, strategy: str, explicit: dict | None) -> tuple[dict, str]:
    setup = SETUPS[strategy]
    if explicit:
        unknown = set(explicit) - set(setup.defaults)
        if unknown:
            raise ValueError(f"unknown parameter(s) for {strategy}: {', '.join(sorted(unknown))}")
        return {**setup.defaults, **explicit}, "given"
    runs = t0_entries(registry, strategy)
    passed = [e for e in runs if e.get("passed") is True]
    for label, pool in (("latest passing T0 run", passed), ("latest T0 run (failed)", runs)):
        if pool:
            e = pool[-1]
            return {**setup.defaults, **(e.get("final_params") or {})}, f"{label} {e['experiment_id']}"
    return dict(setup.defaults), "setup defaults (no T0 run recorded)"


def max_dd_r(trades: pd.DataFrame) -> float:
    if trades.empty:
        return 0.0
    r = trades.sort_values("exit_time")["r"].to_numpy(float)
    return float(metrics.max_drawdown(np.cumsum(r)[None, :])[0])


class ExitRunner:
    """Simulates exit variants on one fixed signal set, cached by (variant, spread multiple)."""

    def __init__(self, markets: list[Market], signals: dict[str, pd.DataFrame], specs: dict[str, ExitSpec]):
        self.markets, self.signals, self.specs = markets, signals, specs
        self._cache: dict = {}
        self._mgmt: dict = {}

    def trades(self, variant: str, spread_mult: float) -> pd.DataFrame:
        key = (variant, spread_mult)
        if key not in self._cache:
            spec = self.specs[variant]
            parts = []
            for m in self.markets:
                sig = self.signals.get(m.symbol)
                if sig is None or sig.empty:
                    continue
                mg = None
                if spec.needs_management:
                    key = (m.symbol, spec.swing_bars)
                    if key not in self._mgmt:
                        self._mgmt[key] = ManagementFrame(m.features, spec.swing_bars)
                    mg = self._mgmt[key]
                if spread_mult not in m.paths:
                    m.paths[spread_mult] = M1Path(m.m1, spread_mult)
                parts.append(simulate_managed(sig, m.m1, m.features, m.costs.with_spread(spread_mult), spec, m.symbol,
                                              m.paths[spread_mult], mg))
            self._cache[key] = _concat(parts)
        return self._cache[key]


def _concat(parts: list[pd.DataFrame]) -> pd.DataFrame:
    parts = [p for p in parts if len(p)]
    if not parts:
        return pd.DataFrame(columns=MANAGED_COLS)
    return pd.concat(parts, ignore_index=True).sort_values("entry_time", ignore_index=True)


def pick_variant(runner: ExitRunner, names: list[str], baseline: str, w: Window, mult: float, sel: dict) -> str:
    """Best train expectancy among variants whose drawdown is within tolerance of the baseline's."""
    base = in_window(runner.trades(baseline, mult), w)
    cap = max_dd_r(base) * (1 + sel["max_dd_vs_baseline"])
    best, best_exp = baseline, base["r"].mean() if len(base) else -np.inf
    for name in names:
        t = in_window(runner.trades(name, mult), w)
        if len(t) < sel["min_train_trades"] or max_dd_r(t) > cap + 1e-12:
            continue
        if t["r"].mean() > best_exp:
            best, best_exp = name, t["r"].mean()
    return best


def paired_gain(chosen: pd.DataFrame, baseline: pd.DataFrame, sims: int, seed: int) -> dict:
    """Bootstrap of the per-entry R difference on entries both exit sets took.

    Exits change which later signals are free to trade (one position per
    setup per symbol), so only shared entries are paired; the unpaired ones
    are counted, not dropped silently.
    """
    key = ["symbol", "decision_time"]
    both = chosen[key + ["r"]].merge(baseline[key + ["r"]], on=key, suffixes=("_c", "_b"))
    diff = (both["r_c"] - both["r_b"]).to_numpy(float)
    out = {"paired": len(diff), "unpaired_chosen": len(chosen) - len(diff), "unpaired_baseline": len(baseline) - len(diff)}
    if len(diff) < 2:
        return {**out, "mean": 0.0, "p05": 0.0}
    rng = np.random.default_rng(seed)
    means = diff[rng.integers(0, len(diff), size=(sims, len(diff)))].mean(axis=1)
    return {**out, "mean": float(diff.mean()), "p05": float(np.percentile(means, 5))}


def mfe_mae_diagnostics(trades: pd.DataFrame) -> dict:
    """What the baseline's excursions say about which exits could help (see the mfe-mae-analysis skill)."""
    if trades.empty:
        return {}
    losers, winners = trades[trades["r"] <= 0], trades[trades["r"] > 0]
    return {
        "trades": len(trades),
        "losers_reaching_1r": float((losers["mfe_r"] >= 1.0).mean()) if len(losers) else 0.0,
        "losers_reaching_0_5r": float((losers["mfe_r"] >= 0.5).mean()) if len(losers) else 0.0,
        "loser_mfe_median_r": float(losers["mfe_r"].median()) if len(losers) else 0.0,
        "winner_mae_median_r": float(winners["mae_r"].median()) if len(winners) else 0.0,
        "mfe_p75_r": float(trades["mfe_r"].quantile(0.75)),
        "share_reaching_target": float((trades["mfe_r"] >= 2.0).mean()),
    }


def variant_row(name: str, spec: ExitSpec, dev_t: pd.DataFrame, val_t: pd.DataFrame, risk_pct: float, sims: int, seed: int) -> dict:
    d, v = metrics.summary(dev_t), metrics.summary(val_t)
    both = _concat([dev_t, val_t])
    return {
        "variant": name,
        "rules": spec.as_dict(),
        "dev_trades": d["trades"], "dev_expectancy_r": d["expectancy_r"], "dev_pf": d["profit_factor"],
        "val_trades": v["trades"], "val_expectancy_r": v["expectancy_r"], "val_pf": v["profit_factor"],
        "win_rate": metrics.summary(both)["win_rate"],
        "avg_bars_held": float(both["bars_held"].mean()) if len(both) else 0.0,
        "stop_moves_per_trade": float(both["stop_moves"].mean()) if len(both) else 0.0,
        "max_dd_r_dev": max_dd_r(dev_t),
        "mc_dd_p95_pct": metrics.mc_drawdown(both["r"].to_numpy(float), risk_pct, sims, seed)["dd_p95_pct"],
        "dev_sharpe": metrics.sharpe(dev_t["r"].to_numpy(float)),
        "exit_reasons": both["exit_reason"].value_counts().to_dict() if len(both) else {},
    }


def run_t1(
    strategy: str,
    cfg: dict,
    t1cfg: dict,
    load_m1: Callable[[str, pd.Timestamp, pd.Timestamp], pd.DataFrame],
    registry: Registry,
    out_dir: Path | None = None,
    now: dt.datetime | None = None,
    seed: int = 0,
    params: dict | None = None,
) -> dict:
    now = now or dt.datetime.now(dt.timezone.utc)
    scfg = cfg["strategies"][strategy]
    registry.check_budget(strategy, cfg["budget"]["experiments_per_strategy_per_month"], now)
    setup = SETUPS[strategy]
    specs = load_variants(t1cfg)
    baseline = t1cfg["baseline"]
    others = [n for n in specs if n != baseline]
    sel, g1, g0, acct = t1cfg["selection"], t1cfg["gates"], cfg["gates"], cfg["account"]
    dev: Window = tuple(_ts(x) for x in cfg["segments"]["dev"])
    val: Window = tuple(_ts(x) for x in cfg["segments"]["validation"])
    base_mult, stress_mult = cfg["costs"]["spread_mult"], cfg["costs"]["stress_spread_mult"]

    entry, entry_source = entry_params(registry, strategy, params)
    t0_passed = any(e.get("passed") is True for e in t0_entries(registry, strategy))

    markets = [prepare_market(s, load_m1(s, dev[0], val[1]), cfg) for s in scfg["symbols"]]
    filters = EdgeFilters(**cfg["filters"])
    signals = {m.symbol: setup.signals(m.features, entry, filters) for m in markets}
    runner = ExitRunner(markets, signals, specs)

    # Walk-forward on dev.
    wf = cfg["walk_forward"]
    folds, ch_parts, ch_stress, bl_parts, bl_stress = [], [], [], [], []
    for train, test in walk_forward_folds(dev, wf["train_months"], wf["test_months"]):
        pick = pick_variant(runner, others, baseline, train, base_mult, sel)
        te, tb = in_window(runner.trades(pick, base_mult), test), in_window(runner.trades(baseline, base_mult), test)
        ch_parts.append(te)
        bl_parts.append(tb)
        ch_stress.append(in_window(runner.trades(pick, stress_mult), test))
        bl_stress.append(in_window(runner.trades(baseline, stress_mult), test))
        folds.append({
            "train": f"{train[0]:%Y-%m}..{train[1]:%Y-%m}", "test": f"{test[0]:%Y-%m}..{test[1]:%Y-%m}", "pick": pick,
            "test_trades": len(te), "test_expectancy": te["r"].mean() if len(te) else 0.0,
            "baseline_expectancy": tb["r"].mean() if len(tb) else 0.0,
        })
    ch_wf, bl_wf = _concat(ch_parts), _concat(bl_parts)

    # Variant chosen on the whole of dev, traded once on validation.
    chosen = pick_variant(runner, others, baseline, dev, base_mult, sel)
    ch_val, bl_val = in_window(runner.trades(chosen, base_mult), val), in_window(runner.trades(baseline, base_mult), val)
    ch_oos, bl_oos = _concat([ch_wf, ch_val]), _concat([bl_wf, bl_val])
    ch_oos_stress = _concat(ch_stress + [in_window(runner.trades(chosen, stress_mult), val)])
    bl_oos_stress = _concat(bl_stress + [in_window(runner.trades(baseline, stress_mult), val)])

    risk_pct, sims = acct["risk_pct"], g0["mc_sims"]
    mc_ch = metrics.mc_drawdown(ch_oos["r"].to_numpy(float), risk_pct, sims, seed)
    mc_bl = metrics.mc_drawdown(bl_oos["r"].to_numpy(float), risk_pct, sims, seed)
    s = {k: metrics.summary(t) for k, t in (("chosen_dev_oos", ch_wf), ("baseline_dev_oos", bl_wf), ("chosen_validation", ch_val),
                                            ("baseline_validation", bl_val), ("chosen_all_oos", ch_oos), ("baseline_all_oos", bl_oos))}
    gain_dev = s["chosen_dev_oos"]["expectancy_r"] - s["baseline_dev_oos"]["expectancy_r"]
    gain_val = s["chosen_validation"]["expectancy_r"] - s["baseline_validation"]["expectancy_r"]
    stress_gain = (ch_oos_stress["r"].mean() if len(ch_oos_stress) else 0.0) - (bl_oos_stress["r"].mean() if len(bl_oos_stress) else 0.0)
    pair = paired_gain(ch_oos, bl_oos, min(sims, 5000), seed)
    dd_ratio = mc_ch["dd_p95_pct"] / mc_bl["dd_p95_pct"] if mc_bl["dd_p95_pct"] > 0 else (0.0 if mc_ch["dd_p95_pct"] == 0 else np.inf)

    rows = [variant_row(n, specs[n], in_window(runner.trades(n, base_mult), dev), in_window(runner.trades(n, base_mult), val),
                        risk_pct, sims, seed) for n in specs]
    trial_sharpes = [r["dev_sharpe"] for r in rows if r["variant"] != baseline]
    all_trials = registry.trial_sharpes(strategy) + trial_sharpes
    var_trials = float(np.var(all_trials, ddof=1)) if len(all_trials) > 1 else 0.0
    dsr = metrics.deflated_sharpe(ch_oos["r"].to_numpy(float), len(all_trials), var_trials)

    ours = "ATLAS default (PRD names the gate, not a threshold)"
    gates = [
        gate("Entry setup passed T0", float(t0_passed), ">=", 1.0, "registry", "PRD §26"),
        gate("Expectancy gain vs fixed baseline (R)", gain_dev, ">", g1["min_expectancy_gain_r"], "dev walk-forward OOS", "PRD §26"),
        gate("Expectancy gain vs fixed baseline (R)", gain_val, ">", g1["min_expectancy_gain_r"], "validation", "PRD §26"),
        gate("MC p95 drawdown vs baseline (ratio)", dd_ratio, "<=", 1 + g1["max_mc_dd_vs_baseline"], "all OOS", "PRD §18, " + ours),
        gate("Expectancy gain at 2x spread (R)", stress_gain, ">", 0.0, "all OOS", "PRD §15, " + ours),
        gate("Paired gain per shared entry, bootstrap p05 (R)", pair["p05"], ">", 0.0, f"all OOS, {pair['paired']} shared entries", ours),
        gate("Chosen expectancy after costs (R)", s["chosen_all_oos"]["expectancy_r"], ">=", g0["min_expectancy_r"], "all OOS", "PRD §15"),
        gate("Deflated Sharpe ratio, exit trials counted", dsr, ">", g0["min_dsr"], f"all OOS, {len(all_trials)} trials", "PRD §15"),
    ]
    passed = all(x["passed"] for x in gates)

    exp_id = f"{strategy}-t1-{now:%Y%m%d-%H%M%S}-" + hashlib.sha1(
        json.dumps([entry, t1cfg, cfg["segments"]], sort_keys=True, default=str).encode()).hexdigest()[:6]
    ch_r = ch_oos["r"].to_numpy(float)
    result = {
        "experiment_id": exp_id,
        "kind": KIND,
        "strategy": strategy,
        "strategy_version": setup.version,
        "created_at": now.isoformat(),
        "hypothesis": "An exit variant beats the fixed 2R baseline out of sample on the same entries.",
        "symbols": scfg["symbols"],
        "data_window": {"dev": [str(dev[0].date()), str(dev[1].date())], "validation": [str(val[0].date()), str(val[1].date())]},
        "entry_params": entry,
        "entry_params_source": entry_source,
        "entry_passed_t0": t0_passed,
        "baseline": baseline,
        "chosen_variant": chosen,
        "chosen_rules": specs[chosen].as_dict(),
        "passed": passed,
        "gates": gates,
        "segments": s,
        "walk_forward": {"folds": folds},
        "monte_carlo": {"chosen": mc_ch, "baseline": mc_bl},
        "dsr": {"value": dsr, "n_trials": len(all_trials), "var_trials": var_trials},
        "paired_gain": pair,
        "variants": rows,
        "trial_sharpes": trial_sharpes,
        "baseline_mfe_mae": mfe_mae_diagnostics(bl_oos),
        "kanban_metadata": {
            "experiment_id": exp_id,
            "strategy_version": setup.version,
            "data_window": "dev+validation",
            "trades": len(ch_oos),
            "expectancy_r": float(ch_r.mean()) if len(ch_r) else 0.0,
            "pf": metrics.profit_factor(ch_r),
            "max_dd_mc95": mc_ch["dd_p95_pct"],
            "dsr": dsr,
            "artifacts": [],
        },
    }
    registry.append({k: result[k] for k in (
        "experiment_id", "kind", "strategy", "strategy_version", "created_at", "hypothesis", "symbols", "data_window",
        "trial_sharpes", "passed", "kanban_metadata", "baseline", "chosen_variant")}
        | {"grid": {n: specs[n].as_dict() for n in specs}, "final_params": entry,
           "failed_gates": [x["gate"] + " / " + x["scope"] for x in gates if not x["passed"]]})

    if out_dir is not None:
        run_dir = Path(out_dir) / exp_id
        result["kanban_metadata"]["artifacts"] = write_t1_report(run_dir, result, ch_oos, bl_oos)
    return result


def write_t1_report(run_dir: Path, result: dict, chosen: pd.DataFrame, baseline: pd.DataFrame) -> list[str]:
    from .report import _table

    run_dir.mkdir(parents=True, exist_ok=True)
    paths = {k: run_dir / n for k, n in (("report", "report.md"), ("summary", "summary.json"),
                                         ("chosen", "oos_trades.csv"), ("baseline", "baseline_oos_trades.csv"))}
    paths["summary"].write_text(json.dumps(result, indent=2, sort_keys=True, default=str))
    chosen.to_csv(paths["chosen"], index=False)
    baseline.to_csv(paths["baseline"], index=False)

    gates = pd.DataFrame(result["gates"])[["gate", "scope", "value", "rule", "passed", "source"]].set_index("gate")
    variants = pd.DataFrame(result["variants"]).drop(columns=["rules", "exit_reasons", "dev_sharpe"]).set_index("variant")
    reasons = pd.DataFrame({r["variant"]: r["exit_reasons"] for r in result["variants"]}).fillna(0).astype(int).T
    verdict = "PASSED every gate" if result["passed"] else f"FAILED {int((~gates['passed']).sum())} of {len(gates)} gates"
    warn = [] if result["entry_passed_t0"] else [
        "> The entry setup has not passed T0. Exit results on entries without an edge are exploratory only;",
        "> this run cannot pass T1 (PRD §26).", ""]
    md = [
        f"# T1 exit research: {result['strategy']} — {verdict}",
        "",
        *warn,
        f"- Experiment: `{result['experiment_id']}` (strategy version {result['strategy_version']})",
        f"- Entries: `{json.dumps(result['entry_params'], sort_keys=True)}` from {result['entry_params_source']}",
        f"- Symbols: {', '.join(result['symbols'])}",
        f"- Dev {result['data_window']['dev'][0]} to {result['data_window']['dev'][1]}, validation "
        f"{result['data_window']['validation'][0]} to {result['data_window']['validation'][1]}; holdout not loaded",
        f"- Baseline `{result['baseline']}`; variant chosen on dev: `{result['chosen_variant']}` "
        f"`{json.dumps(result['chosen_rules'], sort_keys=True)}`",
        "",
        "## Gates",
        "",
        _table(gates),
        "## Chosen vs baseline (R after costs)",
        "",
        _table(pd.DataFrame(result["segments"])),
        "## Every variant (dev in full, validation once)",
        "",
        _table(variants),
        "## Exit reasons (dev + validation)",
        "",
        _table(reasons),
        "## Walk-forward folds",
        "",
        _table(pd.DataFrame(result["walk_forward"]["folds"]).set_index("test")),
        "## Baseline MFE / MAE (all OOS)",
        "",
        _table(pd.Series(result["baseline_mfe_mae"], name="value").to_frame()),
        f"Paired gain on {result['paired_gain']['paired']} shared entries: mean {result['paired_gain']['mean']:+.3f} R, "
        f"bootstrap p05 {result['paired_gain']['p05']:+.3f} R ({result['paired_gain']['unpaired_chosen']} chosen and "
        f"{result['paired_gain']['unpaired_baseline']} baseline trades unpaired).",
        "",
    ]
    paths["report"].write_text("\n".join(md))
    return [str(paths[k]) for k in ("report", "summary", "chosen", "baseline")]
