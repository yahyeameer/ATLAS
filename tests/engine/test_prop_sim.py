"""Prop evaluation Monte Carlo (T3 exit gate machinery)."""

import pandas as pd
import pytest

from atlas_research import prop_sim


def trades(rows):
    return pd.DataFrame(rows, columns=["entry_time", "exit_time", "r", "mae_r"]).assign(
        entry_time=lambda d: pd.to_datetime(d["entry_time"], utc=True),
        exit_time=lambda d: pd.to_datetime(d["exit_time"], utc=True))


def test_day_blocks_group_by_prague_day_and_include_empty_days(cfg):
    t = trades([
        ("2030-01-07 21:30", "2030-01-07 21:45", 1.0, 0.2),  # Mon 22:30 Prague
        ("2030-01-07 23:30", "2030-01-08 01:00", -1.0, 1.0),  # Tue 00:30 Prague -> next server day
        ("2030-01-10 09:00", "2030-01-10 10:00", 2.0, 0.5),  # Thu
    ])
    blocks = prop_sim.day_blocks(t, cfg)
    assert [len(b) for b in blocks] == [1, 1, 0, 1]
    assert blocks[1][0][0] == pytest.approx(30 * 60)  # 00:30 into the Prague day


def test_mae_is_at_least_the_realised_loss(cfg):
    t = trades([("2030-01-07 09:00", "2030-01-07 10:00", -1.5, 0.3)])
    assert prop_sim.day_blocks(t, cfg)[0][0][3] == pytest.approx(1.5)


def test_losing_every_trade_stops_on_drawdown_with_engine_and_breaches_without(cfg):
    rows = [(f"2030-01-{7 + d:02d} {9 + h}:00", f"2030-01-{7 + d:02d} {9 + h}:30", -1.0, 1.0)
            for d in range(5) for h in range(3)]
    t = trades(rows)
    with_engine = prop_sim.simulate_evaluation(t, cfg, n_sims=50, horizon_days=80)
    assert with_engine["p_firm_breach"] == 0.0 and with_engine["p_drawdown_stop"] == 1.0
    without = prop_sim.simulate_evaluation(t, cfg, n_sims=50, horizon_days=80, use_engine=False)
    assert without["p_firm_breach"] == 1.0


def test_winning_every_trade_passes_after_min_days(cfg):
    rows = [(f"2030-01-{7 + d:02d} 09:00", f"2030-01-{7 + d:02d} 10:00", 2.0, 0.1) for d in range(5)]
    res = prop_sim.simulate_evaluation(trades(rows), cfg, n_sims=20, horizon_days=60)
    assert res["p_passed"] == 1.0
    # 10% at 0.8% a day on trading days only; days without trades are sampled too
    assert res["median_days_to_pass"] >= 4


@pytest.mark.parametrize("profile", ["edge_0.20R", "breakeven", "losing_-0.15R"])
def test_reference_profiles_have_the_stated_expectancy(profile):
    t = prop_sim.reference_trades(profile, days=2_000)
    target = {"edge_0.20R": 0.20, "breakeven": 0.0, "losing_-0.15R": -0.15}[profile]
    assert t["r"].mean() == pytest.approx(target, abs=0.05)
    assert (t.loc[t["r"] < 0, "mae_r"] >= 1.0).all()  # losers reached their stop


def test_engine_keeps_breach_under_two_percent_on_a_breakeven_profile(cfg):
    t = prop_sim.reference_trades("busy_breakeven", days=300)
    res = prop_sim.simulate_evaluation(t, cfg, n_sims=300, horizon_days=60)
    assert res["p_firm_breach"] < 0.02
    base = prop_sim.simulate_evaluation(t, cfg, n_sims=300, horizon_days=60, use_engine=False)
    assert base["p_firm_breach"] > res["p_firm_breach"]
