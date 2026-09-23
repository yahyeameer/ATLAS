"""PRD §23 health states from telemetry (atlas_engine/ops/health.py)."""

import pytest

from atlas_engine.ops import health as H

SYMS = {"EURUSD": {"spread_pips": 0.2, "spread_median_pips": 0.2, "tick_gap_s": 1, "in_session": True},
        "GBPUSD": {"spread_pips": 0.5, "spread_median_pips": 0.5, "tick_gap_s": 1, "in_session": True}}
GREEN = {"mt5_disconnected_s": None, "reconciliation_mismatches": 0, "clock_drift_s": 0.1, "db_write_ok": True,
         "heartbeat_age_s": {"engine": 2, "adapter": 2, "watchdog": 5}, "jev_p95_ms": None,
         "daily_loss_frac_of_firm": 0.1, "drawdown_frac_of_firm": 0.05, "manual_kill": None, "symbols": SYMS}


def ev(**changes):
    t = {**GREEN, **changes}
    return H.evaluate(t)


def test_all_green_is_normal():
    h = ev()
    assert h["state"] == H.NORMAL and h["reasons"] == [] and not h["page_operator"]
    assert all(s["state"] == H.NORMAL for s in h["symbols"].values())
    assert H.new_trades_allowed(h, True) == {"EURUSD": True, "GBPUSD": True}
    assert H.new_trades_allowed(h, False) == {"EURUSD": False, "GBPUSD": False}


def test_empty_telemetry_is_normal():
    assert H.evaluate({})["state"] == H.NORMAL


@pytest.mark.parametrize("changes, reason", [
    ({"mt5_disconnected_s": 61}, "mt5_disconnected"),
    ({"reconciliation_mismatches": 1}, "reconciliation_mismatch"),
    ({"clock_drift_s": 2.5}, "clock_drift"),
    ({"clock_drift_s": -2.5}, "clock_drift"),
    ({"db_write_ok": False}, "db_write_failure"),
    ({"heartbeat_age_s": {"engine": 2, "adapter": 2, "watchdog": 61}}, "heartbeat_silent:watchdog"),
    ({"heartbeat_age_s": {"engine": None, "adapter": 2, "watchdog": 5}}, "heartbeat_silent:engine"),
])
def test_halt_triggers(changes, reason):
    h = ev(**changes)
    assert h["state"] == H.HALT and h["reasons"] == [reason] and h["page_operator"]
    # HALT stops new trades on every symbol, whatever the symbol's own state.
    assert set(s["state"] for s in h["symbols"].values()) == {H.HALT}
    assert not any(H.new_trades_allowed(h, True).values())


@pytest.mark.parametrize("changes", [
    {"mt5_disconnected_s": 60}, {"clock_drift_s": 2.0}, {"heartbeat_age_s": {"engine": 60}}])
def test_halt_thresholds_are_strict(changes):
    assert ev(**changes)["state"] == H.NORMAL


@pytest.mark.parametrize("changes, reason", [
    ({"daily_loss_frac_of_firm": 0.75}, "daily_loss_hard_limit"),
    ({"drawdown_frac_of_firm": 0.60}, "drawdown_limit"),
    ({"manual_kill": "operator test"}, "manual_kill"),
])
def test_kill_triggers(changes, reason):
    h = ev(**changes)
    assert h["state"] == H.KILL and h["reasons"] == [reason]


def test_kill_below_limits_is_not_kill():
    assert ev(daily_loss_frac_of_firm=0.74, drawdown_frac_of_firm=0.59)["state"] == H.NORMAL


def test_kill_outranks_halt_and_keeps_both_reasons():
    h = ev(daily_loss_frac_of_firm=0.8, mt5_disconnected_s=120)
    assert h["state"] == H.KILL and h["reasons"] == ["daily_loss_hard_limit", "mt5_disconnected"]


def test_spread_degrades_only_that_symbol():
    syms = {**SYMS, "GBPUSD": {**SYMS["GBPUSD"], "spread_pips": 1.1}}
    h = ev(symbols=syms)
    assert h["state"] == H.DEGRADED and h["reasons"] == ["spread_wide"]
    assert h["symbols"]["GBPUSD"] == {"state": H.DEGRADED, "reasons": ["spread_wide"]}
    assert h["symbols"]["EURUSD"]["state"] == H.NORMAL
    assert H.new_trades_allowed(h, True) == {"EURUSD": True, "GBPUSD": False}


def test_spread_at_exactly_twice_median_is_normal():
    syms = {**SYMS, "EURUSD": {**SYMS["EURUSD"], "spread_pips": 0.4}}
    assert ev(symbols=syms)["state"] == H.NORMAL


def test_tick_gap_counts_only_in_session():
    gap = {**SYMS["EURUSD"], "tick_gap_s": 45}
    assert ev(symbols={**SYMS, "EURUSD": gap})["symbols"]["EURUSD"]["reasons"] == ["tick_gap"]
    assert ev(symbols={**SYMS, "EURUSD": {**gap, "in_session": False}})["state"] == H.NORMAL


def test_jev_latency_degrades_every_symbol():
    h = ev(jev_p95_ms=501)
    assert h["state"] == H.DEGRADED and h["reasons"] == ["jev_latency"]
    assert all(s["reasons"] == ["jev_latency"] for s in h["symbols"].values())
    assert ev(jev_p95_ms=500)["state"] == H.NORMAL
    assert H.evaluate({"jev_p95_ms": 900})["state"] == H.DEGRADED  # no symbols known yet


def test_custom_thresholds():
    assert H.evaluate({"mt5_disconnected_s": 20}, H.Thresholds(mt5_disconnect_s=10))["state"] == H.HALT
