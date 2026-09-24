"""T2 research side: dataset causality, the keep/kill harness on planted and absent edges, end-to-end run."""

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from atlas_engine.adapters.jev import JevAdapter
from atlas_engine.decisions import EVGate
from atlas_engine.decisions.state import NUMERIC
from atlas_research.selection.arms import GBMArm, JevArm
from atlas_research.selection.dataset import recent_mean
from atlas_research.selection.keep_kill import RULES, Criteria, run_folds, training_set, verdict

pytest.importorskip("sklearn")

MONTH = pd.DateOffset(months=1)


def test_recent_mean_only_sees_closed_outcomes():
    t = pd.to_datetime(["2020-01-01 10:00", "2020-01-01 11:00", "2020-01-01 12:00"], utc=True)
    exits = pd.Series(pd.to_datetime(["2020-01-01 11:30", "2020-01-01 11:15", "2020-01-01 13:00"], utc=True))
    r = pd.Series([2.0, -1.0, 2.0])
    out = recent_mean(pd.Series(t), exits, r, n=20)
    assert np.isnan(out[0]) and np.isnan(out[1])  # nothing had closed yet
    assert out[2] == pytest.approx(0.5)  # both earlier trades closed by 12:00; the third's own outcome is not used


def test_training_set_drops_outcomes_not_yet_known():
    c = pd.DataFrame({
        "decision_time": pd.to_datetime(["2020-01-10", "2020-01-30"], utc=True),
        "exit_time": pd.to_datetime(["2020-01-11", "2020-02-02"], utc=True),
    })
    w = (pd.Timestamp("2020-01-01", tz="UTC"), pd.Timestamp("2020-02-01", tz="UTC"))
    assert len(training_set(c, w)) == 1


def synthetic_candidates(n=6000, edge=True, seed=0):
    """Candidates whose target-first chance depends on htf_aligned when ``edge`` is set, else constant."""
    rng = np.random.default_rng(seed)
    t = pd.Timestamp("2019-01-01", tz="UTC") + pd.to_timedelta(np.sort(rng.uniform(0, 4 * 365, n)), unit="D")
    c = pd.DataFrame({k: rng.normal(size=n) for k in NUMERIC})
    c["htf_aligned"] = rng.choice([-1.0, 1.0], n)
    c["recent_signal_r20"] = rng.normal(size=n)
    p = np.where(c["htf_aligned"] > 0, 0.48, 0.20) if edge else np.full(n, 0.32)
    c["y"] = (rng.random(n) < p).astype(int)
    c["decision_time"] = t
    c["entry_time"] = t
    c["exit_time"] = t + pd.Timedelta(hours=6)
    c["session"] = rng.choice(["asia", "london", "overlap", "new_york"], n)
    c["regime"] = "trend_normal_vol"
    c["setup"] = "trend_pullback"
    c["symbol"] = "EURUSD"
    c["target_r"] = 2.0
    c["cost_r_est"] = 0.05
    c["r"] = np.where(c["y"] == 1, 2.0, -1.0) - 0.05
    c["cost_r"] = 0.05
    return c


def trades_of(c: pd.DataFrame) -> pd.DataFrame:
    return c[["entry_time", "exit_time", "symbol", "r", "cost_r"]].copy()


def folds():
    start = pd.Timestamp("2019-01-01", tz="UTC")
    out = []
    for k in range(0, 36, 3):
        a = start + k * MONTH
        out.append(((a, a + 12 * MONTH), (a + 12 * MONTH, a + 15 * MONTH)))
    return out


def run(c, arms):
    crit = Criteria(bootstrap=2000)
    res = run_folds(c, folds(), arms, EVGate(0.15), trades_of, crit)
    return res, verdict(res, arms, crit)


def test_planted_conditional_edge_is_kept():
    arm = GBMArm(params={"max_depth": 2, "learning_rate": 0.1, "max_iter": 50, "min_samples_leaf": 40, "l2_regularization": 1.0})
    res, v = run(synthetic_candidates(edge=True), [arm])
    g = v["arms"]["rules+gbm"]
    assert v["arms"][RULES]["expectancy_r"] < 0  # the rules alone lose
    assert g["expectancy_r"] > 0.3 and 0.3 < g["kept_fraction"] < 0.7
    assert g["checks"] == {"enough_trades": True, "beats_rules_only": True, "brier_beats_base_rate": True}
    assert v["passed"] and v["best_arm"] == "rules+gbm"


def test_no_edge_is_killed():
    arm = GBMArm(params={"max_depth": 2, "learning_rate": 0.1, "max_iter": 50, "min_samples_leaf": 40, "l2_regularization": 1.0})
    _, v = run(synthetic_candidates(edge=False, seed=1), [arm])
    assert not v["passed"]
    assert not v["arms"]["rules+gbm"]["checks"]["brier_beats_base_rate"]


def test_jev_arm_must_also_beat_gbm():
    c = synthetic_candidates(edge=True, n=3000, seed=2)
    c["candidate_id"] = [f"C{i}" for i in range(len(c))]
    truth = dict(zip(c["candidate_id"], np.where(c["htf_aligned"] > 0, 0.48, 0.20)))

    def fake_jev(req):
        return {"setup_id": req["setup_id"], "p_target_first": float(truth[req["setup_id"]]), "regime": "trend_normal_vol",
                "reason_codes": [], "model_version": "jev-test"}

    jev = JevArm(JevAdapter(fake_jev, "jev-test"))
    gbm = GBMArm(params={"max_depth": 2, "learning_rate": 0.1, "max_iter": 50, "min_samples_leaf": 40, "l2_regularization": 1.0})
    _, v = run(c, [gbm, jev])
    j = v["arms"]["rules+jev"]
    assert "beats_gbm" in j["checks"] and j["checks"]["beats_rules_only"]
    # Jev sees the same edge as the baseline, so it cannot show it is better: killed.
    assert not j["checks"]["beats_gbm"] and not j["kept"]


def test_t2_end_to_end_on_random_walk(tmp_path: Path):
    from atlas_engine.market_data import synthetic
    from atlas_research.cli import DEFAULT_CONFIG, DEFAULT_T2_CONFIG, load_config
    from atlas_research.registry import Registry
    from atlas_research.t2 import run_t2

    cfg = load_config(DEFAULT_CONFIG)
    cfg["segments"] = {"dev": ["2019-01-01", "2021-01-01"], "validation": ["2021-01-01", "2021-07-01"], "holdout_start": "2021-07-01"}
    cfg["strategies"]["trend_pullback"]["symbols"] = ["EURUSD"]
    t2 = load_config(DEFAULT_T2_CONFIG)
    t2["criteria"]["bootstrap"] = 1000
    m1 = synthetic.random_walk_m1("EURUSD", "2019-01-01", "2021-07-01", seed=5)
    seen = []

    def load(sym, a, b):
        seen.append(b)
        return m1.loc[(m1.index >= a) & (m1.index < b)]

    reg = Registry(tmp_path / "exp.jsonl")
    res = run_t2("trend_pullback", cfg, t2, load, reg, out_dir=tmp_path / "runs")
    assert not res["passed"]  # no edge by construction
    assert max(seen) <= pd.Timestamp("2021-07-01", tz="UTC")
    assert set(res["arms"]) == {RULES, "rules+gbm"}
    entries = reg.entries("trend_pullback+selection")
    assert len(entries) == 1 and entries[0]["passed"] is False and entries[0]["failed_checks"]
    assert reg.entries("trend_pullback") == []  # T0's trial count is untouched
    assert all(Path(p).exists() for p in res["kanban_metadata"]["artifacts"])
