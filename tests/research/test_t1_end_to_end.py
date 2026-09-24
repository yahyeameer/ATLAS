"""End-to-end T1 runs on synthetic data: noise never passes, a planted trend edge is kept by trailing exits."""

from pathlib import Path

import pytest

from atlas_engine.market_data import synthetic
from atlas_research.cli import DEFAULT_CONFIG, load_config, main
from atlas_research.registry import Registry
from atlas_research.t1 import DEFAULT_T1_CONFIG, entry_params, run_t1

T0_PASS = {"experiment_id": "session_breakout-t0-pass", "strategy": "session_breakout", "created_at": "2026-09-01T00:00:00",
           "passed": True, "final_params": {"mode": "momentum"}, "trial_sharpes": [0.05, 0.02, 0.01, 0.03]}


@pytest.fixture()
def cfg():
    c = load_config(DEFAULT_CONFIG)
    c["segments"] = {"dev": ["2019-01-01", "2022-01-01"], "validation": ["2022-01-01", "2023-01-01"], "holdout_start": "2023-01-01"}
    c["gates"]["mc_sims"] = 2000
    c["strategies"]["session_breakout"]["symbols"] = ["EURUSD"]
    return c


def _run(cfg, tmp_path: Path, momentum: float, t0_passed: bool) -> tuple[dict, Registry]:
    m1 = synthetic.random_walk_m1("EURUSD", "2019-01-01", "2023-01-01", seed=5, momentum=momentum)
    load = lambda sym, a, b: m1.loc[(m1.index >= a) & (m1.index < b)]  # noqa: E731
    reg = Registry(tmp_path / "exp.jsonl")
    if t0_passed:
        reg.append(T0_PASS)
    return run_t1("session_breakout", cfg, load_config(DEFAULT_T1_CONFIG), load, reg, out_dir=tmp_path / "runs"), reg


def _gate(res: dict, name: str, scope: str = "") -> dict:
    return next(g for g in res["gates"] if g["gate"].startswith(name) and g["scope"].startswith(scope))


def test_noise_never_passes_even_with_a_t0_pass_on_record(cfg, tmp_path):
    res, reg = _run(cfg, tmp_path, momentum=0.0, t0_passed=True)
    assert not res["passed"]
    assert not _gate(res, "Paired gain")["passed"]
    assert not _gate(res, "Chosen expectancy")["passed"]
    # Recorded pass or fail, with every non-baseline variant counted as a trial.
    t1 = [e for e in reg.entries("session_breakout") if e.get("kind") == "exit_research"]
    assert len(t1) == 1 and t1[0]["passed"] is False
    assert len(t1[0]["trial_sharpes"]) == len(res["variants"]) - 1
    assert res["dsr"]["n_trials"] == 4 + len(res["variants"]) - 1
    assert all(Path(p).exists() for p in res["kanban_metadata"]["artifacts"])


def test_planted_trend_edge_is_kept_by_a_trailing_exit(cfg, tmp_path):
    res, _ = _run(cfg, tmp_path, momentum=0.35, t0_passed=True)
    assert res["passed"], [g for g in res["gates"] if not g["passed"]]
    assert res["chosen_variant"] in {"atr_trail", "structure_trail", "partial_trail"}
    assert res["entry_params"]["mode"] == "momentum" and "passing T0" in res["entry_params_source"]


def test_a_setup_that_never_passed_t0_cannot_pass_t1(cfg, tmp_path):
    res, _ = _run(cfg, tmp_path, momentum=0.35, t0_passed=False)
    assert not res["passed"]
    assert [g["gate"] for g in res["gates"] if not g["passed"]] == ["Entry setup passed T0"]
    report = Path(res["kanban_metadata"]["artifacts"][0]).read_text()
    assert "has not passed T0" in report


def test_entry_params_prefer_the_latest_passing_t0_run(tmp_path):
    reg = Registry(tmp_path / "exp.jsonl")
    assert entry_params(reg, "session_breakout", None)[1].startswith("setup defaults")
    reg.append({**T0_PASS, "final_params": {"mode": "retest"}})
    reg.append({**T0_PASS, "experiment_id": "later-fail", "passed": False, "final_params": {"mode": "momentum"}})
    reg.append({"experiment_id": "bt", "kind": "backtest", "strategy": "session_breakout", "created_at": "x", "passed": None,
                "final_params": {"mode": "momentum"}})
    params, source = entry_params(reg, "session_breakout", None)
    assert params["mode"] == "retest" and "passing" in source
    with pytest.raises(ValueError, match="unknown parameter"):
        entry_params(reg, "session_breakout", {"nope": 1})


def test_cli_refuses_synthetic_runs_in_the_real_registry(tmp_path, monkeypatch):
    (tmp_path / "SYNTHETIC").write_text("x")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit, match="refusing"):
        main(["t1", "run", "session_breakout", "--data-root", str(tmp_path)])
