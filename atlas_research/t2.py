"""Phase T2 selection-layer run: rules-only vs rules + GBM (+ Jev when a transport is given).

For one strategy this:

1. builds every candidate signal over dev + validation at the T0 cost stress,
   each with its decision state and its independent outcome;
2. walks forward over dev with the T0 folds (train 12 months, test 3), fitting
   each arm on closed training candidates and scoring the next window;
3. fits on all of dev and scores validation once;
4. applies the keep/kill criteria to dev walk-forward + validation together;
5. appends the experiment, pass or fail, to the registry. Every arm is a
   trial for the deflated Sharpe count of ``<strategy>+selection``.

Nothing here reads the holdout: data comes through the same guarded loader
as T0, and the segments end where the holdout starts.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd

from atlas_engine.decisions import EVGate
from atlas_engine.setups import SETUPS, EdgeFilters

from . import metrics
from .backtest import ExitPolicy
from .registry import Registry
from .selection.arms import Arm, GBMArm
from .selection.dataset import build_candidates, signals_of
from .selection.keep_kill import RULES, Criteria, in_decision_window, run_folds, verdict
from .t0 import Runner, _ts, prepare_market, walk_forward_folds


def selection_strategy(strategy: str) -> str:
    return f"{strategy}+selection"


def setup_params(strategy: str, t2cfg: dict, registry: Registry) -> tuple[dict, str]:
    if t2cfg.get("params", {}).get(strategy):
        return dict(t2cfg["params"][strategy]), "t2.yaml"
    t0_runs = registry.entries(strategy)
    if t0_runs:
        return dict(t0_runs[-1]["final_params"]), f"T0 {t0_runs[-1]['experiment_id']}"
    return dict(SETUPS[strategy].defaults), "setup defaults"


def run_t2(
    strategy: str,
    cfg: dict,
    t2cfg: dict,
    load_m1: Callable[[str, pd.Timestamp, pd.Timestamp], pd.DataFrame],
    registry: Registry,
    out_dir: Path | None = None,
    extra_arms: list[Arm] | None = None,
    now: dt.datetime | None = None,
    seed: int = 0,
) -> dict:
    now = now or dt.datetime.now(dt.timezone.utc)
    name = selection_strategy(strategy)
    registry.check_budget(name, t2cfg["budget"]["experiments_per_strategy_per_month"], now)
    scfg = cfg["strategies"][strategy]
    setup = SETUPS[strategy]
    dev = tuple(_ts(x) for x in cfg["segments"]["dev"])
    val = tuple(_ts(x) for x in cfg["segments"]["validation"])
    params, params_source = setup_params(strategy, t2cfg, registry)
    params = {**setup.defaults, **params}

    markets = [prepare_market(s, load_m1(s, dev[0], val[1]), cfg) for s in scfg["symbols"]]
    exits = ExitPolicy(rr=cfg["exits"]["rr"], friday_flatten_utc=cfg["exits"]["friday_flatten_utc"])
    filters = EdgeFilters(**cfg["filters"])
    mult = cfg["costs"]["spread_mult"]
    candidates = build_candidates(setup, markets, params, exits, filters, mult)
    if candidates.empty:
        raise ValueError(f"{strategy}: the setup produced no candidates on dev + validation")

    runner = Runner(setup, markets, exits, filters)
    simulate_fn = lambda c: runner.simulate_signals(signals_of(c), mult) if len(c) else runner.simulate_signals({}, mult)  # noqa: E731

    crit = Criteria(**t2cfg["criteria"])
    gate = EVGate(t2cfg["ev_min"])
    gcfg = t2cfg["gbm"]
    arms: list[Arm] = [GBMArm(params=dict(gcfg["params"]), calibration_frac=gcfg["calibration_frac"], seed=seed)]
    arms += list(extra_arms or [])

    wf = cfg["walk_forward"]
    dev_folds = walk_forward_folds(dev, wf["train_months"], wf["test_months"])
    dev_res = run_folds(candidates, dev_folds, arms, gate, simulate_fn, crit)
    val_res = run_folds(candidates, [(dev, val)], arms, gate, simulate_fn, crit)
    all_res = {
        "trades": {k: _cat(dev_res["trades"][k], val_res["trades"][k]) for k in dev_res["trades"]},
        "scored": {k: pd.concat([dev_res["scored"][k], val_res["scored"][k]], ignore_index=True) for k in dev_res["scored"]},
    }
    v = verdict(all_res, arms, crit, seed)
    seg = {
        "dev_walk_forward": {k: metrics.summary(t) for k, t in dev_res["trades"].items()},
        "validation": {k: metrics.summary(t) for k, t in val_res["trades"].items()},
    }
    trial_sharpes = [metrics.sharpe(dev_res["trades"][a.name]["r"].to_numpy(float)) for a in arms]
    prior = registry.trial_sharpes(name)
    all_trials = prior + trial_sharpes
    best = v["best_arm"]
    best_r = all_res["trades"][best]["r"].to_numpy(float) if best else np.array([])
    dsr = metrics.deflated_sharpe(best_r, len(all_trials), float(np.var(all_trials, ddof=1)) if len(all_trials) > 1 else 0.0)

    exp_id = f"{name}-{now:%Y%m%d-%H%M%S}-" + hashlib.sha1(
        json.dumps([params, t2cfg, cfg["segments"]], sort_keys=True, default=str).encode()).hexdigest()[:6]
    n_val = len(in_decision_window(candidates, val))
    result = {
        "experiment_id": exp_id,
        "strategy": name,
        "setup": strategy,
        "strategy_version": setup.version,
        "created_at": now.isoformat(),
        "hypothesis": f"A calibrated selection model lifts {strategy} expectancy above rules-only out of sample.",
        "symbols": scfg["symbols"],
        "data_window": {"dev": [str(dev[0].date()), str(dev[1].date())], "validation": [str(val[0].date()), str(val[1].date())]},
        "setup_params": params,
        "setup_params_source": params_source,
        "ev_min": gate.ev_min,
        "criteria": crit.__dict__,
        "arms_config": [a.describe() for a in arms],
        "candidates": {"total": len(candidates), "validation": n_val, "target_first_rate": float(candidates["y"].mean())},
        "trial_sharpes": trial_sharpes,
        "passed": v["passed"],
        "best_arm": best,
        "arms": v["arms"],
        "segments": seg,
        "folds": dev_res["folds"] + val_res["folds"],
        "dsr": {"value": dsr, "n_trials": len(all_trials), "arm": best},
        "kanban_metadata": {
            "experiment_id": exp_id,
            "strategy_version": setup.version,
            "data_window": "dev+validation",
            "trades": int(v["arms"][best]["trades"]) if best else 0,
            "expectancy_r": float(v["arms"][best]["expectancy_r"]) if best else 0.0,
            "rules_only_expectancy_r": float(v["arms"][RULES]["expectancy_r"]),
            "dsr": dsr,
            "artifacts": [],
        },
    }
    registry.append({k: result[k] for k in (
        "experiment_id", "strategy", "strategy_version", "created_at", "hypothesis", "symbols", "data_window",
        "trial_sharpes", "passed", "kanban_metadata")} | {"final_params": params, "grid": {}, "failed_checks": [
            f"{arm}: {c}" for arm, s in v["arms"].items() if arm != RULES for c, ok in s["checks"].items() if not ok]})

    if out_dir is not None:
        from .selection.report import write_report

        result["kanban_metadata"]["artifacts"] = write_report(Path(out_dir) / exp_id, result, all_res["trades"], candidates)
    return result


def _cat(a: pd.DataFrame, b: pd.DataFrame) -> pd.DataFrame:
    parts = [p for p in (a, b) if len(p)]
    return pd.concat(parts, ignore_index=True) if parts else a
