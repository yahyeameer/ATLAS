import numpy as np
import pandas as pd
import pytest

from atlas_research.backtest import CostModel, ExitPolicy, count_rollovers, simulate

T0 = pd.Timestamp("2020-01-07 10:00", tz="UTC")  # a Tuesday
NO_COSTS = CostModel(spread_mult=1.0, commission_rt=0, entry_slippage=0, stop_slippage=0, swap_per_rollover=0)
SPREAD = 0.0002


def path(mids: list[tuple[float, float, float, float]], spread: float = SPREAD) -> pd.DataFrame:
    """M1 frame from (open, high, low, close) mids starting at T0."""
    idx = pd.date_range(T0, periods=len(mids), freq="1min", name="time")
    df = pd.DataFrame(index=idx)
    for i, c in enumerate("ohlc"):
        mid = np.array([m[i] for m in mids])
        df[f"bid_{c}"] = mid - spread / 2
        df[f"ask_{c}"] = mid + spread / 2
    df["volume"] = 1.0
    return df


def signal(direction: int, stop: float) -> pd.DataFrame:
    return pd.DataFrame({"decision_time": [T0], "direction": [direction], "stop": [stop], "atr": [0.001], "spread": [SPREAD], "setup": ["t"]})


def test_long_fills_at_ask_and_hits_target_on_bid():
    # Entry ask 1.1001, stop 1.0991 -> risk 0.0010, target 1.1021 (on the bid).
    m1 = path([(1.1000, 1.1005, 1.0998, 1.1004), (1.1004, 1.1023, 1.1003, 1.1020)])
    t = simulate(signal(1, 1.0991), m1, NO_COSTS).iloc[0]
    assert t["entry"] == pytest.approx(1.1001)
    assert t["exit_reason"] == "target"
    assert t["r"] == pytest.approx(2.0)


def test_short_stop_is_triggered_on_the_ask():
    # Entry bid 1.0999, stop 1.1009. Mid high 1.1008 puts the ask at 1.1009.
    m1 = path([(1.1000, 1.1002, 1.0995, 1.1000), (1.1000, 1.1008, 1.0999, 1.1003)])
    t = simulate(signal(-1, 1.1009), m1, NO_COSTS).iloc[0]
    assert t["exit_reason"] == "stop"
    assert t["r"] == pytest.approx(-1.0)


def test_same_bar_stop_and_target_counts_as_stop():
    m1 = path([(1.1000, 1.1001, 1.0999, 1.1000), (1.1000, 1.1030, 1.0980, 1.1000)])
    t = simulate(signal(1, 1.0991), m1, NO_COSTS).iloc[0]
    assert t["exit_reason"] == "stop"


def test_gap_through_stop_fills_at_the_open():
    m1 = path([(1.1000, 1.1002, 1.0999, 1.1000), (1.0980, 1.0985, 1.0975, 1.0982)])
    t = simulate(signal(1, 1.0991), m1, NO_COSTS).iloc[0]
    assert t["exit_reason"] == "gap_stop"
    assert t["exit"] == pytest.approx(1.0979)  # bid open
    assert t["r"] < -1.0


def test_spread_stress_widens_around_mid():
    m1 = path([(1.1000, 1.1005, 1.0998, 1.1004)] * 3)
    stressed = CostModel(spread_mult=2.0, commission_rt=0, entry_slippage=0, stop_slippage=0, swap_per_rollover=0)
    t = simulate(signal(1, 1.0980), m1, stressed).iloc[0]
    assert t["entry"] == pytest.approx(1.1002)  # mid + spread (0.0002 * 2 / 2)


def test_costs_are_charged_in_r():
    m1 = path([(1.1000, 1.1005, 1.0998, 1.1004), (1.1004, 1.1023, 1.1003, 1.1020)])
    costs = CostModel(spread_mult=1.0, commission_rt=0.0001, entry_slippage=0, stop_slippage=0, swap_per_rollover=0)
    t = simulate(signal(1, 1.0991), m1, costs).iloc[0]
    assert t["cost_r"] == pytest.approx(0.1)
    assert t["r"] == pytest.approx(1.9)


def test_one_position_at_a_time():
    m1 = path([(1.1000, 1.1001, 1.0999, 1.1000)] * 30)
    sig = pd.concat([signal(1, 1.0990), signal(1, 1.0990).assign(decision_time=T0 + pd.Timedelta(minutes=5))])
    t = simulate(sig, m1, NO_COSTS)
    assert len(t) == 1
    assert t.iloc[0]["exit_reason"] == "end_of_data"


def test_stop_already_crossed_is_skipped():
    m1 = path([(1.1000, 1.1001, 1.0999, 1.1000)] * 3)
    assert simulate(signal(1, 1.1005), m1, NO_COSTS).empty


def test_friday_flatten():
    fri = pd.Timestamp("2020-01-10 19:50", tz="UTC")
    idx = pd.date_range(fri, periods=20, freq="1min", name="time")
    m1 = path([(1.1000, 1.1001, 1.0999, 1.1000)] * 20).set_axis(idx)
    sig = signal(1, 1.0990).assign(decision_time=fri)
    t = simulate(sig, m1, NO_COSTS, ExitPolicy(friday_flatten_utc="20:00")).iloc[0]
    assert t["exit_reason"] == "friday_flatten"
    assert t["exit_time"] == pd.Timestamp("2020-01-10 20:00", tz="UTC")


@pytest.mark.parametrize(
    "entry,exit_,nights",
    [
        ("2020-01-06 08:00", "2020-01-06 12:00", 0),
        ("2020-01-06 08:00", "2020-01-07 12:00", 1),
        ("2020-01-08 08:00", "2020-01-09 12:00", 3),  # Wednesday rollover is triple
        ("2020-01-10 15:00", "2020-01-13 12:00", 1),  # over the weekend
    ],
)
def test_count_rollovers(entry, exit_, nights):
    assert count_rollovers(pd.Timestamp(entry, tz="UTC"), pd.Timestamp(exit_, tz="UTC"), 2) == nights


def test_times_are_unit_safe():
    """pandas 3 can infer microsecond indexes; decision times and entry delays must not be misread."""
    m1 = path([(1.1000, 1.1005, 1.0998, 1.1004), (1.1004, 1.1023, 1.1003, 1.1020)])
    m1.index = m1.index.as_unit("us")
    sig = signal(1, 1.0991)
    sig["decision_time"] = pd.DatetimeIndex(sig["decision_time"]).as_unit("us")
    t = simulate(sig, m1, NO_COSTS).iloc[0]
    assert t["decision_time"] == T0
    # A signal more than 5 minutes before the next bar (e.g. a weekend gap) is skipped.
    late = signal(1, 1.0991).assign(decision_time=T0 - pd.Timedelta(hours=1))
    assert simulate(late, m1, NO_COSTS).empty
