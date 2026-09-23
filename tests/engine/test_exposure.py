"""Currency netting and correlation clusters (PRD §20)."""

import datetime as dt

import numpy as np
import pandas as pd
import pytest

from atlas_engine.exposure import CorrelationMatrix, Exposure, correlated_with, correlation_matrix, currency_risk, legs


def test_legs_long_eurusd_is_long_eur_short_usd():
    assert legs(Exposure("EURUSD", 1, 0.4)) == {"EUR": 0.4, "USD": -0.4}
    assert legs(Exposure("USDJPY", -1, 0.4)) == {"USD": -0.4, "JPY": 0.4}


def test_gold_is_a_usd_leg_plus_a_gold_bucket():
    assert legs(Exposure("XAUUSD", 1, 0.3)) == {"XAU": 0.3, "USD": -0.3}


def test_net_risk_adds_same_way_and_cancels_opposite():
    net = currency_risk([Exposure("EURUSD", 1, 0.4), Exposure("GBPUSD", 1, 0.4), Exposure("USDJPY", 1, 0.4)])
    assert net["USD"] == pytest.approx(-0.4)  # -0.4 -0.4 +0.4
    assert net["EUR"] == pytest.approx(0.4) and net["JPY"] == pytest.approx(-0.4)


def matrix(c):
    syms = ["EURUSD", "GBPUSD", "USDJPY"]
    m = pd.DataFrame(np.eye(3), index=syms, columns=syms)
    m.loc["EURUSD", "GBPUSD"] = m.loc["GBPUSD", "EURUSD"] = c
    return CorrelationMatrix(m, dt.date(2026, 9, 21))


def test_correlated_same_direction_is_one_bet():
    new, open_ = Exposure("GBPUSD", 1, 0.4), [Exposure("EURUSD", 1, 0.4)]
    assert correlated_with(new, open_, matrix(0.85), 0.7)[0] == open_
    assert correlated_with(new, open_, matrix(0.6), 0.7)[0] == []


def test_opposite_directions_on_correlated_pairs_are_a_hedge_not_one_bet():
    assert correlated_with(Exposure("GBPUSD", -1, 0.4), [Exposure("EURUSD", 1, 0.4)], matrix(0.85), 0.7)[0] == []


def test_same_symbol_always_correlates():
    new = Exposure("EURUSD", 1, 0.4)
    assert correlated_with(new, [Exposure("EURUSD", 1, 0.4)], matrix(0.0), 0.7)[0]


def test_fallback_without_matrix_uses_shared_currency_direction():
    new = Exposure("GBPUSD", 1, 0.4)  # short USD
    hits, rule = correlated_with(new, [Exposure("EURUSD", 1, 0.4), Exposure("USDJPY", 1, 0.4)], None, 0.7)
    assert rule == "shared_currency"
    assert [e.symbol for e in hits] == ["EURUSD"]  # USDJPY long is long USD: opposite leg


def test_pairs_missing_from_matrix_fall_back_to_shared_currency():
    new = Exposure("XAUUSD", 1, 0.4)
    hits, rule = correlated_with(new, [Exposure("EURUSD", 1, 0.4)], matrix(0.5), 0.7)
    assert rule == "matrix+shared_currency" and hits


def test_correlation_matrix_from_daily_closes():
    rng = np.random.default_rng(1)
    common = rng.normal(0, 0.005, 120)
    closes = pd.DataFrame({
        "EURUSD": 1.1 * np.exp(np.cumsum(common + rng.normal(0, 0.001, 120))),
        "GBPUSD": 1.3 * np.exp(np.cumsum(common + rng.normal(0, 0.001, 120))),
        "USDJPY": 150 * np.exp(np.cumsum(rng.normal(0, 0.005, 120))),
    }, index=pd.bdate_range("2026-04-01", periods=120))
    m = correlation_matrix(closes, 60)
    assert m.get("EURUSD", "GBPUSD") > 0.9
    assert abs(m.get("EURUSD", "USDJPY")) < 0.5
    assert m.as_of == closes.index[-1].date()
