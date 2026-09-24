import numpy as np
import pandas as pd
import pytest

from atlas_engine.features import indicators as ind
from atlas_engine.features import sessions
from atlas_engine.features.frame import FeatureConfig, build_features
from atlas_engine.market_data import synthetic
from atlas_engine.setups import SETUPS, EdgeFilters


@pytest.fixture(scope="module")
def m1():
    return synthetic.random_walk_m1(start="2019-01-01", end="2019-05-01", seed=11)


@pytest.fixture(scope="module")
def feats(m1):
    return build_features(m1)


def test_ema_matches_recursive_definition():
    x = pd.Series(np.arange(1.0, 11.0))
    out = ind.ema(x, 3)
    alpha = 2 / 4
    manual = [x[0]]
    for v in x[1:]:
        manual.append(alpha * v + (1 - alpha) * manual[-1])
    assert out.iloc[2:].tolist() == pytest.approx(manual[2:])
    assert out.iloc[:2].isna().all()


def test_atr_of_constant_range_is_that_range():
    n = 50
    close = pd.Series(np.full(n, 1.0))
    out = ind.atr(close + 0.001, close - 0.001, close, 14)
    assert out.iloc[-1] == pytest.approx(0.002)


def test_h1_features_only_use_closed_hours(m1, feats):
    # The M15 bar opening 09:45 closes at 10:00 and may see the 09:00 H1 bar;
    # the 10:00 bar (closes 10:15) must still see 09:00, not the forming 10:00.
    day = feats.index[feats.index.normalize() == pd.Timestamp("2019-02-05", tz="UTC")]
    t0945 = day[(day.hour == 9) & (day.minute == 45)][0]
    t1000 = day[(day.hour == 10) & (day.minute == 0)][0]
    last = m1.loc[pd.Timestamp("2019-02-05 09:59", tz="UTC")]
    h1_close_0900 = (last["bid_c"] + last["ask_c"]) / 2
    assert feats.loc[t0945, "h1_close"] == pytest.approx(h1_close_0900)
    assert feats.loc[t1000, "h1_close"] == pytest.approx(h1_close_0900)


def test_asia_range_only_after_it_forms(feats):
    before = feats["london_min"] < 7 * 60
    assert feats.loc[before, "asia_high"].isna().all()
    assert feats.loc[~before, "asia_high"].notna().any()


def test_fx_day_rolls_at_five_pm_new_york():
    idx = pd.DatetimeIndex(["2019-01-07 21:59", "2019-01-07 22:00"], tz="UTC")  # 16:59 / 17:00 NY
    d = sessions.fx_day(idx)
    assert str(d[0]) == "2019-01-07" and str(d[1]) == "2019-01-08"


def test_rollover_blackout_is_dst_aware():
    summer = pd.DatetimeIndex(["2019-07-10 21:00"], tz="UTC")  # 17:00 EDT
    winter = pd.DatetimeIndex(["2019-01-10 22:00"], tz="UTC")  # 17:00 EST
    assert sessions.rollover_blackout(summer)[0] and sessions.rollover_blackout(winter)[0]
    assert not sessions.rollover_blackout(pd.DatetimeIndex(["2019-01-10 21:00"], tz="UTC"))[0]


@pytest.mark.parametrize("name", sorted(SETUPS))
def test_setups_have_no_lookahead(m1, name):
    """Signals decided before a cut-off must not change when later data is added."""
    cut = pd.Timestamp("2019-03-20", tz="UTC")
    full = SETUPS[name].signals(build_features(m1))
    part = SETUPS[name].signals(build_features(m1.loc[m1.index < cut]))
    full = full.loc[full["decision_time"] <= cut].reset_index(drop=True)
    part = part.loc[part["decision_time"] <= cut].reset_index(drop=True)
    assert len(full) > 5
    pd.testing.assert_frame_equal(full, part)


@pytest.mark.parametrize("name", sorted(SETUPS))
def test_signals_respect_stop_bounds_and_filters(feats, name):
    sig = SETUPS[name].signals(feats)
    assert len(sig)
    close = feats.set_index("close_time")["close"].reindex(pd.DatetimeIndex(sig["decision_time"])).to_numpy()
    dist = sig["direction"].to_numpy() * (close - sig["stop"].to_numpy())
    p = SETUPS[name].defaults
    assert (dist > 0).all()
    assert (dist <= p["sl_max_atr"] * sig["atr"].to_numpy() + 1e-12).all()
    assert (dist >= p["sl_min_atr"] * sig["atr"].to_numpy() - 1e-12).all()
    assert (sig["spread"].to_numpy() <= 0.2 * dist + 1e-12).all()
    assert not sessions.rollover_blackout(pd.DatetimeIndex(sig["decision_time"])).any()


def test_spread_filter_drops_signals(feats):
    loose = SETUPS["trend_pullback"].signals(feats)
    tight = SETUPS["trend_pullback"].signals(feats, filters=EdgeFilters(max_spread_to_stop=0.001))
    assert len(tight) < len(loose)


@pytest.fixture(scope="module")
def feats_h1(m1):
    return build_features(m1, FeatureConfig(bar="1h"))


def test_h1_decision_frame_uses_its_own_closed_bar(m1, feats_h1):
    t0900 = pd.Timestamp("2019-02-05 09:00", tz="UTC")
    last = m1.loc[pd.Timestamp("2019-02-05 09:59", tz="UTC")]
    assert feats_h1.loc[t0900, "close_time"] == pd.Timestamp("2019-02-05 10:00", tz="UTC")
    assert feats_h1.loc[t0900, "h1_close"] == pytest.approx((last["bid_c"] + last["ask_c"]) / 2)
    assert feats_h1.loc[t0900, "close"] == pytest.approx(feats_h1.loc[t0900, "h1_close"])
    assert (feats_h1.index.minute == 0).all()


def test_decision_bar_must_divide_an_hour(m1):
    with pytest.raises(ValueError):
        build_features(m1.iloc[:5000], FeatureConfig(bar="45min"))


@pytest.mark.parametrize("name", sorted(SETUPS))
def test_setups_have_no_lookahead_on_h1_bars(m1, name):
    cut = pd.Timestamp("2019-03-20", tz="UTC")
    cfg = FeatureConfig(bar="1h")
    full = SETUPS[name].signals(build_features(m1, cfg))
    part = SETUPS[name].signals(build_features(m1.loc[m1.index < cut], cfg))
    full = full.loc[full["decision_time"] <= cut].reset_index(drop=True)
    part = part.loc[part["decision_time"] <= cut].reset_index(drop=True)
    assert len(full) > 0
    pd.testing.assert_frame_equal(full, part)
