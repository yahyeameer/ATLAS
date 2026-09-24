"""T2 engine side: EV gate, regimes, leakage-safe decision state, calibration."""

import json
import math

import numpy as np
import pandas as pd
import pytest

from atlas_engine.calibration import Calibrator, brier, brier_skill, fit_isotonic, reliability
from atlas_engine.decisions import PAYLOAD_KEYS, REGIMES, EVGate, breakeven_p, ev_r, payload, regime_label, state_frame
from atlas_engine.features.frame import build_features
from atlas_engine.market_data import synthetic
from atlas_engine.setups import SETUPS


# EV gate (§17)

def test_prd_worked_example_is_accepted():
    d = EVGate(0.15).decide(0.45, 2.0, 0.08)
    assert d.take and d.ev_r == pytest.approx(0.27)


def test_below_min_is_skipped_and_breakeven_is_exact():
    g = EVGate(0.15)
    p0 = breakeven_p(2.0, 0.08, 0.15)
    assert ev_r(p0, 2.0, 0.08) == pytest.approx(0.15)
    assert g.decide(p0 + 1e-9, 2.0, 0.08).take
    assert not g.decide(p0 - 1e-6, 2.0, 0.08).take
    assert g.decide(p0 - 1e-6, 2.0, 0.08).reason == "ev_below_min"


@pytest.mark.parametrize("p", [None, math.nan, math.inf, -0.01, 1.01, "0.6", True])
def test_invalid_probability_is_a_skip_never_a_guess(p):
    d = EVGate().decide(p, 2.0, 0.0)
    assert not d.take and d.reason == "no_valid_probability"


def test_mask_matches_decide():
    p = np.array([0.2, 0.45, 0.6, np.nan, 1.5, -0.1, 0.9])
    g = EVGate(0.15)
    expect = [g.decide(None if not np.isfinite(x) else float(x), 2.0, 0.05).take for x in p]
    assert g.mask(p, 2.0, 0.05).tolist() == expect


# Regimes and state (§16, §17)

def test_regime_labels_are_the_fixed_set():
    labels = regime_label([30, 30, 10, 10, 30, 10], [0.1, 0.5, 0.9, 0.5, 0.95, 0.2])
    assert labels.tolist() == ["trend_low_vol", "trend_normal_vol", "range_high_vol", "range_normal_vol", "trend_high_vol", "range_low_vol"]
    assert set(labels) <= set(REGIMES) and len(REGIMES) == 6


@pytest.fixture(scope="module")
def market():
    m1 = synthetic.random_walk_m1("EURUSD", "2019-01-01", "2019-07-01", seed=3)
    f = build_features(m1)
    sig = SETUPS["trend_pullback"].signals(f)
    assert len(sig) > 10
    return m1, f, sig


def test_state_has_no_absolute_prices_dates_or_symbols(market):
    _, f, sig = market
    st = state_frame(f, sig)
    p = payload(st.iloc[0])
    assert tuple(p) == PAYLOAD_KEYS
    blob = json.dumps(p)
    assert "EURUSD" not in blob and "2019" not in blob
    close = f["close"].median()
    for k, v in p.items():
        if isinstance(v, float):
            assert abs(v - close) > 1e-3 or abs(v) < 5, k  # nothing near a raw price level


def test_state_is_invariant_to_price_level_and_scale(market):
    """Shifting and scaling every price leaves the state unchanged: it holds only relative distances."""
    m1, f, sig = market
    a = state_frame(f, sig)
    m2 = m1.copy()
    for c in m2.columns:
        if c.startswith(("bid_", "ask_")):
            m2[c] = m2[c] * 1.7 + 0.3
    f2 = build_features(m2)
    sig2 = sig.assign(stop=sig["stop"] * 1.7 + 0.3, spread=sig["spread"] * 1.7)
    b = state_frame(f2, sig2)
    num = a.select_dtypes("number").columns
    np.testing.assert_allclose(a[num].to_numpy(float), b[num].to_numpy(float), rtol=1e-6, atol=1e-6)
    assert (a["regime"] == b["regime"]).all() and (a["session"] == b["session"]).all()


def test_state_is_causal(market):
    """Truncating the future does not change the state of a signal already decided."""
    m1, f, sig = market
    cut = sig["decision_time"].iloc[len(sig) // 2]
    full = state_frame(f, sig)
    f_cut = build_features(m1.loc[m1.index < cut])
    early = sig.loc[sig["decision_time"] < cut]
    part = state_frame(f_cut, early)
    num = part.select_dtypes("number").columns
    np.testing.assert_allclose(full.loc[early.index, num].to_numpy(float), part[num].to_numpy(float), rtol=1e-9, equal_nan=True)


def test_direction_signs_distances(market):
    _, f, sig = market
    flipped = sig.assign(direction=-sig["direction"], stop=2 * f.set_index("close_time")["close"].reindex(sig["decision_time"]).to_numpy() - sig["stop"])
    a, b = state_frame(f, sig), state_frame(f, flipped)
    for k in ("dist_ema_atr", "ret_1_atr", "htf_aligned", "h1_slope_atr"):
        np.testing.assert_allclose(a[k], -b[k], equal_nan=True)
    np.testing.assert_allclose(a["stop_atr"], b["stop_atr"])


# Calibration

def test_isotonic_is_monotone_and_pools_violators():
    bx, by = fit_isotonic([1, 2, 3, 4], [0, 1, 0, 1])
    assert list(by) == sorted(by)
    cal = Calibrator.fit([1, 2, 3, 4], [0, 1, 0, 1])
    np.testing.assert_allclose(cal([1, 2, 3, 4]), [0, 0.5, 0.5, 1])


def test_isotonic_matches_sklearn():
    sk = pytest.importorskip("sklearn.isotonic")
    rng = np.random.default_rng(0)
    x = rng.random(400)
    y = (rng.random(400) < x**2).astype(float)
    ref = sk.IsotonicRegression(out_of_bounds="clip").fit(x, y)
    grid = np.linspace(-0.1, 1.1, 200)
    np.testing.assert_allclose(Calibrator.fit(x, y)(grid), ref.predict(grid), atol=1e-12)


def test_calibrator_roundtrips_and_uses_window():
    rng = np.random.default_rng(1)
    raw = rng.random(900)
    y = (rng.random(900) < raw).astype(int)
    cal = Calibrator.fit(raw, y, window=500, source="gbm-1", group="usd_majors")
    assert cal.n == 500
    again = Calibrator.from_json(cal.to_json())
    np.testing.assert_array_equal(cal(raw), again(raw))
    assert np.isnan(cal([np.nan]))[0]


def test_brier_and_skill():
    y = np.array([1, 0, 1, 1])
    assert brier([1, 0, 1, 1], y) == 0
    assert brier_skill([0.75] * 4, y, 0.75) == pytest.approx(0.0)
    assert brier_skill([0.9, 0.1, 0.9, 0.9], y, 0.75) > 0
    rel = reliability(np.array([0.05, 0.15, 0.95]), np.array([0, 0, 1]))
    assert rel["n"].sum() == 3
