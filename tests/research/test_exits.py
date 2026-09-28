"""T1 managed exits (PRD §18): parity with the T0 simulator and each rule on hand-built paths."""

import numpy as np
import pandas as pd
import pytest

from atlas_engine.features.frame import build_features
from atlas_engine.market_data import synthetic
from atlas_engine.setups import SETUPS
from atlas_research.backtest import TRADE_COLS, CostModel, ExitPolicy, simulate
from atlas_research.exits import ExitSpec, ManagementFrame, simulate_managed

T0 = pd.Timestamp("2020-01-07 10:00", tz="UTC")  # a Tuesday, on an M15 close
NO_COSTS = CostModel(spread_mult=1.0, commission_rt=0, entry_slippage=0, stop_slippage=0, swap_per_rollover=0)
SPREAD = 0.0002
ENTRY = 1.1001  # ask at the first open (mid 1.1000)
STOP = 1.0991  # risk 0.0010 = 1R


def path(mids: list[float], start: pd.Timestamp = T0, spread: float = SPREAD, wick: float = 0.00002) -> pd.DataFrame:
    """M1 frame whose minute i opens at the previous close and closes at ``mids[i]``."""
    idx = pd.date_range(start, periods=len(mids), freq="1min", name="time")
    c = np.array(mids, float)
    o = np.concatenate([[c[0]], c[:-1]])
    h, lo = np.maximum(o, c) + wick, np.minimum(o, c) - wick
    df = pd.DataFrame(index=idx)
    for name, mid in zip("ohlc", (o, h, lo, c)):
        df[f"bid_{name}"] = mid - spread / 2
        df[f"ask_{name}"] = mid + spread / 2
    df["volume"] = 1.0
    return df


def feats(m1: pd.DataFrame, trend=1, atr: float = 0.0005) -> pd.DataFrame:
    """Minimal M15 management frame: close_time, ATR, H1 trend and mid high/low. ``trend[0]`` is the signal bar's."""
    mid = (m1["bid_l"] + m1["ask_l"]) / 2, (m1["bid_h"] + m1["ask_h"]) / 2
    g = pd.DataFrame({"low": mid[0], "high": mid[1]}).resample("15min")
    f = pd.DataFrame({"low": g["low"].min(), "high": g["high"].max()})
    # The bar that closed at the first decision time (the setup's signal bar).
    pre = f.iloc[[0]].set_axis([f.index[0] - pd.Timedelta(minutes=15)])
    f = pd.concat([pre, f])
    f["close_time"] = f.index + pd.Timedelta(minutes=15)
    f["atr"] = atr
    f["h1_trend"] = trend if np.isscalar(trend) else np.asarray(trend)[: len(f)]
    return f


def signal(direction: int = 1, stop: float = STOP, at: pd.Timestamp = T0) -> pd.DataFrame:
    return pd.DataFrame({"decision_time": [at], "direction": [direction], "stop": [stop], "atr": [0.0005],
                         "spread": [SPREAD], "setup": ["t"]})


def run(mids, spec, trend=1, sig=None, start=T0, costs=NO_COSTS):
    m1 = path(mids, start)
    return simulate_managed(sig if sig is not None else signal(at=start), m1, feats(m1, trend), costs, spec).iloc[0]


def ramp(a: float, b: float, n: int) -> list[float]:
    return list(np.linspace(a, b, n + 1)[1:])


# ---------------------------------------------------------------- parity with T0

@pytest.mark.parametrize("name", sorted(SETUPS))
def test_fixed_2r_matches_the_t0_simulator_exactly(name):
    m1 = synthetic.random_walk_m1("EURUSD", "2019-01-01", "2019-07-01", seed=3, momentum=0.2)
    f = build_features(m1)
    sig = SETUPS[name].signals(f)
    costs = CostModel()
    a = simulate(sig, m1, costs, ExitPolicy(), "EURUSD")
    b = simulate_managed(sig, m1, f, costs, ExitSpec(), "EURUSD")
    assert len(a) > 20
    pd.testing.assert_frame_equal(a, b[TRADE_COLS], check_dtype=False)
    assert (b["stop_moves"] == 0).all() and b["partial_r"].isna().all()


# ---------------------------------------------------------------- breakeven

def test_breakeven_moves_the_stop_at_the_m15_close_after_plus_1r():
    # +1.2R inside the first M15 bar, then back down through entry: stopped at breakeven, not at -1R.
    mids = [1.1000] + ramp(1.1000, 1.1013, 10) + [1.1013] * 4 + ramp(1.1013, 1.0980, 20)
    t = run(mids, ExitSpec("be", breakeven_at_r=1.0))
    assert t["exit_reason"] == "managed_stop"
    assert t["stop_moves"] == 1
    assert t["r"] == pytest.approx(0.0, abs=1e-9)


def test_breakeven_does_not_act_inside_the_bar_it_saw_plus_1r():
    # +1.2R and straight back to the stop within one M15 bar: the move only happens at the close, too late.
    mids = [1.1000] + ramp(1.1000, 1.1013, 5) + ramp(1.1013, 1.0985, 8)
    t = run(mids, ExitSpec("be", breakeven_at_r=1.0))
    assert t["exit_reason"] == "stop"
    assert t["r"] == pytest.approx(-1.0)


def test_breakeven_stop_covers_commission_and_slippage():
    costs = CostModel(spread_mult=1.0, commission_rt=0.00007, entry_slippage=0, stop_slippage=0.00002, swap_per_rollover=0)
    mids = [1.1000] + ramp(1.1000, 1.1013, 10) + [1.1013] * 4 + ramp(1.1013, 1.0980, 20)
    t = run(mids, ExitSpec("be", breakeven_at_r=1.0), costs=costs)
    assert t["exit_reason"] == "managed_stop"
    assert t["r"] == pytest.approx(0.0, abs=1e-9)  # gross gain pays the commission and the stop slippage


# ---------------------------------------------------------------- partial close

def test_partial_then_initial_stop_nets_to_zero():
    mids = [1.1000] + ramp(1.1000, 1.1012, 6) + ramp(1.1012, 1.0985, 12)
    t = run(mids, ExitSpec("pc", rr=2.0, partial_at_r=1.0, partial_frac=0.5))
    assert t["exit_reason"] == "stop"
    assert t["partial_r"] == pytest.approx(1.0)
    assert t["r"] == pytest.approx(0.5 * 1.0 + 0.5 * -1.0)


def test_partial_then_target():
    mids = [1.1000] + ramp(1.1000, 1.1025, 30)
    t = run(mids, ExitSpec("pc", rr=2.0, partial_at_r=1.0, partial_frac=0.5))
    assert t["exit_reason"] == "target"
    assert t["r"] == pytest.approx(0.5 * 1.0 + 0.5 * 2.0)


def test_same_minute_stop_and_partial_counts_as_stop():
    m1 = path([1.1000, 1.1000])
    m1.iloc[1, m1.columns.get_loc("bid_h")] = 1.1030
    m1.iloc[1, m1.columns.get_loc("bid_l")] = 1.0980
    t = simulate_managed(signal(), m1, feats(m1), NO_COSTS, ExitSpec("pc", partial_at_r=1.0)).iloc[0]
    assert t["exit_reason"] == "stop" and np.isnan(t["partial_r"])


# ---------------------------------------------------------------- time stop

def test_time_stop_closes_a_dead_trade_after_12_bars():
    mids = [1.1000] * (15 * 14)
    t = run(mids, ExitSpec("ts", time_stop_bars=12, time_stop_min_r=0.5))
    assert t["exit_reason"] == "time_stop"
    assert t["exit_time"] == T0 + pd.Timedelta(minutes=15 * 12)
    assert t["bars_held"] == 13  # the exit minute opens the 13th bar


def test_time_stop_spares_a_trade_that_reached_half_r():
    mids = [1.1000] + ramp(1.1000, 1.1007, 10) + [1.1003] * (15 * 14)
    t = run(mids, ExitSpec("ts", time_stop_bars=12, time_stop_min_r=0.5))
    assert t["exit_reason"] == "end_of_data"


# ---------------------------------------------------------------- invalidation

def test_invalidation_closes_when_the_h1_trend_flips():
    mids = [1.1000] * (15 * 6)
    trend = [1, 1, -1, -1, -1, -1, -1]  # signal bar, then the bars closing 10:15, 10:30, ...
    t = run(mids, ExitSpec("inv", invalidation=True), trend=trend)
    assert t["exit_reason"] == "invalidation"
    assert t["exit_time"] == T0 + pd.Timedelta(minutes=30)  # first M15 close that shows the flip


def test_invalidation_ignores_a_trend_the_entry_was_already_fading():
    mids = [1.1000] * (15 * 6)
    t = run(mids, ExitSpec("inv", invalidation=True), trend=-1)
    assert t["exit_reason"] == "end_of_data"


# ---------------------------------------------------------------- trails

def test_atr_trail_locks_profit_and_only_tightens():
    # Rally to +3R, then a pullback deeper than 2 x ATR: exit on the trailed stop, in profit.
    mids = [1.1000] + ramp(1.1000, 1.1030, 30) + [1.1030] * 15 + ramp(1.1030, 1.0990, 40)
    t = run(mids, ExitSpec("atr", rr=None, atr_trail_mult=2.0, atr_trail_after_r=1.5))
    assert t["exit_reason"] == "managed_stop"
    assert t["stop_moves"] >= 1
    best = 1.1030 + 0.00002 - SPREAD / 2  # highest bid
    assert t["exit"] == pytest.approx(best - 2 * 0.0005)
    assert t["r"] > 1.0


def test_trail_never_moves_the_stop_to_where_price_already_is():
    # A huge ATR puts the trail candidate below the current stop: no move, initial stop stays.
    mids = [1.1000] + ramp(1.1000, 1.1020, 20) + ramp(1.1020, 1.0985, 30)
    m1 = path(mids)
    t = simulate_managed(signal(), m1, feats(m1, atr=0.01), NO_COSTS, ExitSpec("atr", rr=None, atr_trail_mult=2.0)).iloc[0]
    assert t["stop_moves"] == 0 and t["exit_reason"] == "stop"


def test_swing_levels_are_causal():
    m1 = synthetic.random_walk_m1("EURUSD", "2019-01-07", "2019-01-12", seed=1)
    f = build_features(m1)
    full = ManagementFrame(f, 2)
    cut = ManagementFrame(f.iloc[:200], 2)
    np.testing.assert_array_equal(full.swing_low[:200], cut.swing_low)
    np.testing.assert_array_equal(full.swing_high[:200], cut.swing_high)


def test_structure_trail_exits_below_the_last_swing_low():
    # Up to 1.1020, a two-bar pullback to 1.1012 (the swing low), up to 1.1030, then a fall.
    mids = ([1.1000] + ramp(1.1000, 1.1020, 30) + [1.1020] * 15 + ramp(1.1020, 1.1015, 15) + ramp(1.1015, 1.1012, 15)
            + ramp(1.1012, 1.1025, 15) + ramp(1.1025, 1.1030, 15) + [1.1030] * 15 + ramp(1.1030, 1.0990, 40))
    m1 = path(mids)
    f = feats(m1)
    t = simulate_managed(signal(), m1, f, NO_COSTS, ExitSpec("st", rr=None, structure_trail=True, structure_after_r=1.0,
                                                             structure_buffer_atr=0.0)).iloc[0]
    swing = f.loc[T0 + pd.Timedelta(minutes=60) : T0 + pd.Timedelta(minutes=75), "low"].min()
    assert t["exit_reason"] == "managed_stop"
    assert t["exit"] == pytest.approx(swing)  # stop at the swing low (mid prices), triggered on the bid
    assert t["r"] == pytest.approx((swing - ENTRY) / 0.0010)


# ---------------------------------------------------------------- session exits

def test_rollover_exit_closes_before_the_new_york_rollover():
    start = pd.Timestamp("2020-01-07 21:00", tz="UTC")  # 16:00 New York (EST)
    t = run([1.1000] * 90, ExitSpec("sess", rollover_exit_ny="16:45"), start=start)
    assert t["exit_reason"] == "session_exit"
    assert t["exit_time"] == pd.Timestamp("2020-01-07 21:45", tz="UTC")


def test_friday_flatten_applies_to_every_variant():
    start = pd.Timestamp("2020-01-10 19:00", tz="UTC")
    t = run([1.1000] * 90, ExitSpec("atr", rr=None, atr_trail_mult=2.0), start=start)
    assert t["exit_reason"] == "friday_flatten"


# ---------------------------------------------------------------- spec validation

@pytest.mark.parametrize("d, msg", [
    ({"partial_at_r": 2.0}, "below the target"),
    ({"partial_at_r": 1.0, "partial_frac": 1.0}, "partial_frac"),
    ({"rr": None, "friday_flatten_utc": None}, "nothing else"),
    ({"trailing": True}, "unknown key"),
])
def test_bad_variants_are_rejected(d, msg):
    with pytest.raises(ValueError, match=msg):
        ExitSpec.from_dict("x", d)


def test_managed_variant_needs_features():
    m1 = path([1.1000] * 20)
    with pytest.raises(ValueError, match="feature frame"):
        simulate_managed(signal(), m1, None, NO_COSTS, ExitSpec("be", breakeven_at_r=1.0))
