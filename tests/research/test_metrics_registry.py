import datetime as dt

import numpy as np
import pandas as pd
import pytest

from atlas_research import metrics
from atlas_research.data import HoldoutAccessError, check_not_holdout, load_m1
from atlas_research.registry import BudgetExceeded, Registry
from atlas_research.t0 import expand_grid, neighbours, walk_forward_folds


def test_profit_factor_and_summary():
    t = pd.DataFrame({"r": [2.0, -1.0, -1.0, 2.0], "cost_r": [0.1] * 4})
    s = metrics.summary(t)
    assert s["expectancy_r"] == pytest.approx(0.5)
    assert s["profit_factor"] == pytest.approx(2.0)
    assert s["win_rate"] == pytest.approx(0.5)


def test_max_drawdown():
    eq = np.array([[1.0, 3.0, 0.5, 2.0, -1.0]])
    assert metrics.max_drawdown(eq)[0] == pytest.approx(4.0)


def test_mc_drawdown_scales_with_risk():
    r = np.tile([2.0, -1.0, -1.0, -1.0], 50)
    a = metrics.mc_drawdown(r, 0.5, 500, seed=1)["dd_p95_pct"]
    b = metrics.mc_drawdown(r, 1.0, 500, seed=1)["dd_p95_pct"]
    assert b == pytest.approx(2 * a)


def test_deflated_sharpe_falls_with_more_trials():
    rng = np.random.default_rng(0)
    r = rng.normal(0.15, 1.0, 400)
    one = metrics.deflated_sharpe(r, 1, 0.0)
    many = metrics.deflated_sharpe(r, 200, 0.01)
    assert one > 0.95
    assert many < one


def test_expected_max_sharpe_grows_with_trials():
    assert metrics.expected_max_sharpe(1, 0.01) == 0.0
    assert 0 < metrics.expected_max_sharpe(10, 0.01) < metrics.expected_max_sharpe(1000, 0.01)


def test_daily_breach_probability():
    days = pd.date_range("2020-01-06 12:00", periods=40, freq="B", tz="UTC")
    calm = pd.DataFrame({"exit_time": days, "r": [-1.0] * 40})
    assert metrics.daily_breach_probability(calm, 0.4, 5.0, 30, 1000) == 0.0
    wild = pd.DataFrame({"exit_time": list(days) * 15, "r": [-1.0] * 600})
    assert metrics.daily_breach_probability(wild, 0.4, 5.0, 30, 1000) == 1.0


def test_holdout_is_refused(tmp_path):
    with pytest.raises(HoldoutAccessError):
        check_not_holdout(pd.Timestamp("2025-07-02"), pd.Timestamp("2025-07-01"))
    with pytest.raises(HoldoutAccessError):
        load_m1(tmp_path, "EURUSD", "2025-01-01", "2025-08-01", "2025-07-01")
    check_not_holdout(pd.Timestamp("2025-07-01"), pd.Timestamp("2025-07-01"))  # exclusive end is fine


def test_registry_budget_and_trials(tmp_path):
    reg = Registry(tmp_path / "exp.jsonl")
    now = dt.datetime(2026, 9, 22, tzinfo=dt.timezone.utc)
    for i in range(3):
        reg.append({"strategy": "s", "created_at": now.isoformat(), "trial_sharpes": [0.1 * i, 0.2]})
    reg.append({"strategy": "other", "created_at": now.isoformat(), "trial_sharpes": [9.0]})
    assert reg.trial_sharpes("s") == [0.0, 0.2, 0.1, 0.2, 0.2, 0.2]
    reg.check_budget("s", 4, now)
    with pytest.raises(BudgetExceeded):
        reg.check_budget("s", 3, now)
    reg.check_budget("s", 3, dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc))


def test_walk_forward_folds():
    dev = (pd.Timestamp("2019-01-01", tz="UTC"), pd.Timestamp("2024-01-01", tz="UTC"))
    folds = walk_forward_folds(dev, 12, 3)
    assert len(folds) == 16
    assert folds[0] == ((dev[0], pd.Timestamp("2020-01-01", tz="UTC")), (pd.Timestamp("2020-01-01", tz="UTC"), pd.Timestamp("2020-04-01", tz="UTC")))
    assert folds[-1][1][1] == dev[1]


def test_grid_and_neighbours():
    assert len(expand_grid({"a": [1, 2], "b": ["x", "y", "z"]})) == 6
    nb = neighbours({"n": 4, "x": 1.0, "zero": 0.0, "mode": "m"}, 0.2)
    assert {"n": 3, "x": 1.0, "zero": 0.0, "mode": "m"} in nb
    assert {"n": 5, "x": 1.0, "zero": 0.0, "mode": "m"} in nb
    assert any(d["x"] == pytest.approx(0.8) for d in nb)
    assert len(nb) == 4
