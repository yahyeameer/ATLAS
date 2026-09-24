"""Keep/kill harness: rules-only vs rules + selection arms on identical candidates (PRD §17, T2 exit gate).

For every fold, each arm is fitted on candidates that had fully closed inside
the training window (a label is only known after its exit) and scores the
next test window. Rules-only takes every test candidate; an arm takes those
the EV gate accepts. Each arm's accepted signals are then re-simulated with
the one-position rule, so its trades are what it would really have traded.

An arm is kept only if, out of sample, it has at least ``min_trades``
trades, its expectancy beats rules-only with bootstrap probability at least
``win_prob``, and its calibrated probabilities beat the base rate on Brier.
Jev must also beat the gradient-boosted arm. The phase gate is that the best
arm is kept.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import numpy as np
import pandas as pd

from atlas_engine.calibration import brier, brier_skill
from atlas_engine.decisions import EVGate

from .. import metrics
from .arms import Arm

RULES = "rules_only"
Window = tuple[pd.Timestamp, pd.Timestamp]


@dataclass(frozen=True)
class Criteria:
    min_trades: int = 300
    win_prob: float = 0.95
    bootstrap: int = 10_000
    min_train_candidates: int = 150


def in_decision_window(c: pd.DataFrame, w: Window) -> pd.DataFrame:
    t = pd.DatetimeIndex(c["decision_time"])
    return c.loc[(t >= w[0]) & (t < w[1])]


def training_set(c: pd.DataFrame, w: Window) -> pd.DataFrame:
    """Candidates decided in the window whose outcome was known before it ended."""
    d = in_decision_window(c, w)
    return d.loc[pd.DatetimeIndex(d["exit_time"]) < w[1]]


def p_better(a: np.ndarray, b: np.ndarray, n: int, seed: int) -> float:
    """Bootstrap probability that mean(a) > mean(b), resampling each independently."""
    if len(a) < 2 or len(b) < 2:
        return 0.0
    rng = np.random.default_rng(seed)
    ma = a[rng.integers(0, len(a), (n, len(a)))].mean(axis=1)
    mb = b[rng.integers(0, len(b), (n, len(b)))].mean(axis=1)
    return float((ma > mb).mean())


def run_folds(
    candidates: pd.DataFrame,
    folds: list[tuple[Window, Window]],
    arms: list[Arm],
    gate: EVGate,
    simulate_fn: Callable[[pd.DataFrame], pd.DataFrame],
    criteria: Criteria = Criteria(),
) -> dict:
    """Trades and scored predictions per arm over ``folds``."""
    trades = {RULES: [], **{a.name: [] for a in arms}}
    scored = {a.name: [] for a in arms}
    fold_rows = []
    for train_w, test_w in folds:
        train = training_set(candidates, train_w)
        test = in_decision_window(candidates, test_w)
        row = {"train": f"{train_w[0]:%Y-%m}..{train_w[1]:%Y-%m}", "test": f"{test_w[0]:%Y-%m}..{test_w[1]:%Y-%m}",
               "train_candidates": len(train), "test_candidates": len(test)}
        if len(train) < criteria.min_train_candidates or test.empty:
            row["skipped"] = "too few training candidates" if len(test) else "no test candidates"
            fold_rows.append(row)
            continue
        base_rate = float(train["y"].mean())
        t_rules = simulate_fn(test)
        trades[RULES].append(t_rules)
        row[f"{RULES}_trades"] = len(t_rules)
        for arm in arms:
            arm.fit(train)
            p = arm.predict(test)
            take = gate.mask(p, test["target_r"].to_numpy(float), test["cost_r_est"].to_numpy(float))
            t_arm = simulate_fn(test.loc[take])
            trades[arm.name].append(t_arm)
            scored[arm.name].append(pd.DataFrame({
                "p": p, "y": test["y"].to_numpy(), "base_rate": base_rate, "take": take,
                "symbol": test["symbol"].to_numpy(), "session": test["session"].to_numpy()}))
            row[f"{arm.name}_kept"] = float(take.mean())
            row[f"{arm.name}_trades"] = len(t_arm)
        fold_rows.append(row)
    return {
        "trades": {k: _concat(v) for k, v in trades.items()},
        "scored": {k: pd.concat(v, ignore_index=True) if v else pd.DataFrame(columns=["p", "y", "base_rate", "take", "symbol", "session"]) for k, v in scored.items()},
        "folds": fold_rows,
    }


def _concat(parts: list[pd.DataFrame]) -> pd.DataFrame:
    parts = [p for p in parts if len(p)]
    if not parts:
        return pd.DataFrame(columns=["r", "cost_r", "entry_time", "exit_time", "symbol"])
    return pd.concat(parts, ignore_index=True).sort_values("entry_time", ignore_index=True)


def calibration_scores(scored: pd.DataFrame) -> dict:
    ok = scored.loc[np.isfinite(scored["p"].to_numpy(float))]
    if ok.empty:
        return {"n": 0, "brier": float("nan"), "brier_base_rate": float("nan"), "brier_skill": float("nan"), "by_group": []}
    y = ok["y"].to_numpy(float)
    base = ok["base_rate"].to_numpy(float)
    rows = []
    for (sym, ses), g in ok.groupby(["symbol", "session"]):
        rows.append({"symbol": sym, "session": ses, "n": len(g), "brier": brier(g["p"], g["y"]), "brier_base_rate": brier(g["base_rate"], g["y"])})
    return {
        "n": len(ok),
        "brier": brier(ok["p"], y),
        "brier_base_rate": brier(base, y),
        # Skill against the train-window base rate each fold actually had.
        "brier_skill": float(1 - brier(ok["p"], y) / brier(base, y)) if brier(base, y) > 0 else float("nan"),
        "brier_skill_vs_oos_rate": brier_skill(ok["p"], y, float(y.mean())),
        "by_group": rows,
    }


def verdict(result: dict, arms: list[Arm], criteria: Criteria, seed: int = 0) -> dict:
    """Per-arm OOS summary, keep/kill decision and the phase gate."""
    rules_r = result["trades"][RULES]["r"].to_numpy(float)
    rules = metrics.summary(result["trades"][RULES])
    out = {RULES: {**rules, "kept": None}}
    for arm in arms:
        t = result["trades"][arm.name]
        r = t["r"].to_numpy(float)
        s = metrics.summary(t)
        cal = calibration_scores(result["scored"][arm.name])
        scored = result["scored"][arm.name]
        s.update(
            kept_fraction=float(scored["take"].mean()) if len(scored) else 0.0,
            p_beats_rules=p_better(r, rules_r, criteria.bootstrap, seed),
            calibration=cal,
        )
        checks = {
            "enough_trades": len(r) >= criteria.min_trades,
            "beats_rules_only": s["expectancy_r"] > rules["expectancy_r"] and s["p_beats_rules"] >= criteria.win_prob,
            "brier_beats_base_rate": bool(np.isfinite(cal["brier_skill"]) and cal["brier_skill"] > 0),
        }
        s["checks"] = checks
        out[arm.name] = s
    gbm = next((a.name for a in arms if a.name.endswith("gbm")), None)
    for arm in arms:
        s = out[arm.name]
        if arm.name.endswith("jev") and gbm:
            g_r = result["trades"][gbm]["r"].to_numpy(float)
            s["p_beats_gbm"] = p_better(result["trades"][arm.name]["r"].to_numpy(float), g_r, criteria.bootstrap, seed + 1)
            s["checks"]["beats_gbm"] = s["expectancy_r"] > out[gbm]["expectancy_r"] and s["p_beats_gbm"] >= criteria.win_prob
        s["kept"] = all(s["checks"].values())
    ranked = sorted((a.name for a in arms), key=lambda n: out[n]["expectancy_r"], reverse=True)
    best = ranked[0] if ranked else None
    return {"arms": out, "best_arm": best, "passed": bool(best and out[best]["kept"])}
