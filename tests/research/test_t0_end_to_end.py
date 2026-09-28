"""End-to-end T0 runs on synthetic data: no false pass on noise, and a planted edge is found."""

from pathlib import Path

import pytest

from atlas_engine.market_data import synthetic
from atlas_research.cli import DEFAULT_CONFIG, load_config
from atlas_research.registry import Registry
from atlas_research.t0 import run_t0


@pytest.fixture()
def cfg():
    c = load_config(DEFAULT_CONFIG)
    c["segments"] = {"dev": ["2019-01-01", "2022-01-01"], "validation": ["2022-01-01", "2023-01-01"], "holdout_start": "2023-01-01"}
    c["gates"]["mc_sims"] = 2000
    c["gates"]["random_control_runs"] = 20
    c["strategies"]["session_breakout"]["symbols"] = ["EURUSD"]
    return c


def _run(cfg, tmp_path: Path, momentum: float) -> dict:
    m1 = synthetic.random_walk_m1("EURUSD", "2019-01-01", "2023-01-01", seed=5, momentum=momentum)
    load = lambda sym, a, b: m1.loc[(m1.index >= a) & (m1.index < b)]  # noqa: E731
    return run_t0("session_breakout", cfg, load, Registry(tmp_path / "exp.jsonl"), out_dir=tmp_path / "runs")


def _gate(res: dict, name: str, scope: str) -> dict:
    return next(g for g in res["gates"] if g["gate"] == name and g["scope"].startswith(scope))


def test_random_walk_never_passes(cfg, tmp_path):
    res = _run(cfg, tmp_path, momentum=0.0)
    assert not res["passed"]
    assert not _gate(res, "Expectancy after costs (R)", "dev")["passed"]
    assert not _gate(res, "Expectancy after costs (R)", "validation")["passed"]
    assert not _gate(res, "Expectancy minus random-entry p95 (R)", "all")["passed"]
    # Failures are recorded too, with the handoff shape and report artifacts.
    entries = Registry(tmp_path / "exp.jsonl").entries("session_breakout")
    assert len(entries) == 1 and entries[0]["passed"] is False
    assert len(entries[0]["trial_sharpes"]) == 4
    assert all(Path(p).exists() for p in res["kanban_metadata"]["artifacts"])


def test_planted_momentum_edge_is_found(cfg, tmp_path):
    res = _run(cfg, tmp_path, momentum=0.35)
    assert _gate(res, "Expectancy after costs (R)", "dev")["passed"]
    assert _gate(res, "Expectancy after costs (R)", "validation")["passed"]
    assert _gate(res, "Expectancy minus random-entry p95 (R)", "all")["passed"]
    assert _gate(res, "Walk-forward efficiency", "dev")["passed"]
    assert _gate(res, "Expectancy at 2x spread (R)", "all")["passed"]


def test_h1_variant_runs_and_shares_the_setups_trials(cfg, tmp_path):
    cfg["strategies"]["session_breakout_h1"]["symbols"] = ["EURUSD"]
    m1 = synthetic.random_walk_m1("EURUSD", "2019-01-01", "2023-01-01", seed=5)
    load = lambda sym, a, b: m1.loc[(m1.index >= a) & (m1.index < b)]  # noqa: E731
    reg = Registry(tmp_path / "exp.jsonl")
    run_t0("session_breakout", cfg, load, reg)
    res = run_t0("session_breakout_h1", cfg, load, reg)
    assert not res["passed"]
    assert res["setup"] == "session_breakout" and res["bar"] == "1h"
    assert res["dsr"]["n_trials"] == 8
    entry = reg.entries("session_breakout_h1")[0]
    assert entry["setup"] == "session_breakout" and entry["bar"] == "1h"
