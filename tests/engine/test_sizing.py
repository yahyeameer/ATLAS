"""Lot sizing (PRD §20)."""

import math

import numpy as np
import pytest

from atlas_engine.sizing import ContractSpec, contract, size_position
from atlas_engine.sizing.lots import conversion_rate, risk_per_lot

EURUSD_NO_COMM = ContractSpec("EURUSD", "EUR", "USD", 0.00001, 100_000)


def test_formula_on_eurusd():
    # $10,000 x 0.4% = $40; 15 pips = 150 ticks x $1 = $150 per lot -> 0.266 -> 0.26 lots.
    r = size_position(10_000, 0.40, 1.0, 0.0015, EURUSD_NO_COMM)
    assert r.ok and r.volume == pytest.approx(0.26)
    assert r.risk_amount == pytest.approx(39.0)
    assert r.budget == pytest.approx(40.0)


def test_commission_counts_against_the_budget():
    r = size_position(10_000, 0.40, 1.0, 0.0015, contract("EURUSD"))
    assert r.volume == pytest.approx(0.25)  # $157 per lot with $7 commission
    assert r.risk_amount <= r.budget


def test_multiplier_halves_the_budget():
    r = size_position(10_000, 0.40, 0.5, 0.0015, EURUSD_NO_COMM)
    assert r.budget == pytest.approx(20.0) and r.volume == pytest.approx(0.13)


def test_jpy_quote_converts_through_usdjpy():
    spec = contract("USDJPY")
    # 0.001 x 100,000 = 100 JPY per tick = $0.6667 at 150.
    assert spec.tick_value("USD", {"USDJPY": 150.0}) == pytest.approx(100 / 150)
    r = size_position(10_000, 0.40, 1.0, 0.15, spec, "USD", {"USDJPY": 150.0})
    assert r.ok and r.risk_amount <= 40.0


def test_missing_conversion_rate_is_an_error_not_a_guess():
    with pytest.raises(KeyError, match="USDJPY"):
        size_position(10_000, 0.40, 1.0, 0.15, contract("USDJPY"), "USD", {})


def test_conversion_rate_both_directions():
    assert conversion_rate("EUR", "USD", {"EURUSD": 1.1}) == pytest.approx(1.1)
    assert conversion_rate("USD", "EUR", {"EURUSD": 1.1}) == pytest.approx(1 / 1.1)
    assert conversion_rate("USD", "USD", {}) == 1.0


def test_tick_value_loss_overrides_computed_value():
    spec = ContractSpec("EURUSD", "EUR", "USD", 0.00001, 100_000, tick_value_loss=1.02)
    assert risk_per_lot(spec, 0.0015, "USD") == pytest.approx(153.0)


def test_volume_min_allowed_within_110_percent_of_budget():
    # Budget $1.40 (tiny account); 0.01 lot at 15 pips costs $1.50 = 107% -> allowed.
    r = size_position(350, 0.40, 1.0, 0.0015, EURUSD_NO_COMM)
    assert r.ok and r.volume == pytest.approx(0.01)


def test_volume_min_rejected_beyond_110_percent_of_budget():
    r = size_position(300, 0.40, 1.0, 0.0015, EURUSD_NO_COMM)  # $1.20 budget, $1.50 minimum = 125%
    assert not r.ok and r.reason == "min_volume_exceeds_budget" and r.volume == 0


def test_clamped_to_prop_max_lot_and_volume_max():
    r = size_position(10_000_000, 0.5, 1.0, 0.0005, EURUSD_NO_COMM, max_lot=3.0)
    assert r.volume == pytest.approx(3.0)
    r = size_position(10_000_000, 0.5, 1.0, 0.0005, EURUSD_NO_COMM)
    assert r.volume == pytest.approx(EURUSD_NO_COMM.volume_max)


@pytest.mark.parametrize("equity, pct, m, sl", [(10_000, 0.4, 1.0, 0.0), (10_000, 0.4, 1.0, -0.001),
                                               (0, 0.4, 1.0, 0.001), (10_000, 0.0, 1.0, 0.001),
                                               (10_000, 0.4, 0.0, 0.001), (10_000, 0.4, 1.0, math.nan)])
def test_invalid_inputs_are_rejected(equity, pct, m, sl):
    assert not size_position(equity, pct, m, sl, EURUSD_NO_COMM).ok


def test_property_rounding_never_exceeds_budget_above_min_volume():
    rng = np.random.default_rng(7)
    for _ in range(5_000):
        equity = rng.uniform(1_000, 500_000)
        sl = rng.uniform(0.0002, 0.0100)
        spec = contract(rng.choice(["EURUSD", "GBPUSD", "XAUUSD"]))
        if spec.symbol == "XAUUSD":
            sl *= 1_000
        r = size_position(equity, 0.40, rng.choice([0.5, 1.0]), sl, spec)
        if not r.ok:
            assert r.reason == "min_volume_exceeds_budget"
            continue
        assert r.volume >= spec.volume_min - 1e-12
        assert abs(r.volume / spec.volume_step - round(r.volume / spec.volume_step)) < 1e-6
        if r.volume > spec.volume_min + 1e-12:
            assert r.risk_amount <= r.budget + 1e-6
            # and one more step would have overshot, unless volume_max capped it
            assert r.volume >= spec.volume_max - 1e-9 or (r.volume + spec.volume_step) * risk_per_lot(spec, sl, "USD") > r.budget - 1e-6
        else:
            assert r.risk_amount <= r.budget * 1.10 + 1e-6
