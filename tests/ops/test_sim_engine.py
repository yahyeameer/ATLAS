"""The simulated engine: faults drive §23 states, KILL flattens and disables, only the operator re-enables."""

import json
import threading

import pytest

from atlas_engine.ops.sim import FAULTS, SimRefused, SimulatedEngine

from opshelp import clock, engine  # noqa: F401


def kinds(engine):
    return [e["kind"] for e in engine.events()["events"]]


def test_initial_state_is_healthy(engine):
    s = engine.status()
    assert s["source"] == "simulated" and s["state"] == "NORMAL" and s["trading"]["enabled"]
    assert s["new_trades_allowed"] == {"EURUSD": True, "GBPUSD": True}
    assert s["mt5_connected"] and s["open_positions"] == 1
    assert engine.reconciliation()["status"] == "clean"


def test_missing_state_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError, match="atlas-engine-sim init"):
        SimulatedEngine(tmp_path / "nope.json").status()


@pytest.mark.parametrize("fault, state", [
    ("mt5_disconnect", "HALT"), ("recon_mismatch", "HALT"), ("clock_drift", "HALT"),
    ("db_write_failure", "HALT"), ("heartbeat_silence", "HALT"), ("spread_spike", "DEGRADED"),
    ("tick_gap", "DEGRADED"), ("jev_latency", "DEGRADED"), ("daily_loss", "KILL"), ("drawdown", "KILL"),
])
def test_every_fault_maps_to_its_state(engine, fault, state):
    engine.inject(fault)
    assert engine.health()["state"] == state
    engine.clear(fault)
    assert engine.health()["state"] == "NORMAL"


def test_fault_list_is_complete():
    assert set(FAULTS) == {"mt5_disconnect", "spread_spike", "tick_gap", "recon_mismatch", "clock_drift",
                           "db_write_failure", "heartbeat_silence", "daily_loss", "drawdown", "jev_latency"}


def test_disconnect_ages_with_time(engine, clock):
    engine.inject("mt5_disconnect", seconds=30)
    assert engine.health()["state"] == "NORMAL"  # 30 s is under the 60 s budget
    clock.advance(seconds=31)
    h = engine.health()
    assert h["state"] == "HALT" and h["telemetry"]["mt5_disconnected_s"] == 61.0


def test_transitions_are_logged_once(engine):
    engine.inject("mt5_disconnect")
    engine.status()
    engine.health()
    changes = [e for e in engine.events()["events"] if e["kind"] == "state_change"]
    assert [(c["previous"], c["state"]) for c in changes] == [("NORMAL", "HALT")]


def test_halt_does_not_disable_but_blocks_new_trades(engine):
    engine.inject("recon_mismatch", symbol="GBPUSD")
    s = engine.status()
    assert s["state"] == "HALT" and s["trading"]["enabled"]
    assert s["new_trades_allowed"] == {"EURUSD": False, "GBPUSD": False}
    r = engine.reconciliation()
    assert r["status"] == "mismatch" and r["mismatches"][0]["symbol"] == "GBPUSD"
    assert r["broker_positions"] == r["engine_positions"] + 1


def test_kill_flattens_and_disables(engine):
    engine.inject("daily_loss", frac=0.9)
    s = engine.status()
    assert s["state"] == "KILL" and s["open_positions"] == 0
    assert not s["trading"]["enabled"] and s["trading"]["by"] == "engine"
    assert "flattened" in kinds(engine) and "trading_disabled" in kinds(engine)


def test_disable_is_idempotent_and_keeps_the_first_reason(engine):
    first = engine.disable_trading("operations-monitor/atlas-operations", "MT5 dropped at 14:00")
    again = engine.disable_trading("someone-else", "second reason")
    assert not first["already_disabled"] and again["already_disabled"]
    assert again["trading"]["reason"] == "MT5 dropped at 14:00"
    assert kinds(engine).count("trading_disabled") == 1


def test_operator_enable_is_refused_while_halt_or_kill(engine):
    engine.inject("mt5_disconnect")
    engine.disable_trading("ops", "mt5 down for drill")
    with pytest.raises(SimRefused, match="HALT"):
        engine.enable("yahye", "back up")
    engine.clear()
    assert engine.enable("yahye", "back up")["enabled"]
    assert engine.status()["new_trades_allowed"] == {"EURUSD": True, "GBPUSD": True}


def test_manual_kill_needs_operator_enable(engine):
    engine.kill("yahye", "drill")
    s = engine.status()
    assert s["state"] == "KILL" and s["reasons"] == ["manual_kill"] and not s["trading"]["enabled"]
    engine.enable("yahye", "drill over")  # no other fault: the operator may clear their own kill
    assert engine.status()["state"] == "NORMAL"


def test_events_since_and_limit(engine):
    for _ in range(3):
        engine.inject("spread_spike")
        engine.clear()
    all_events = engine.events(0, 200)["events"]
    tail = engine.events(all_events[-3]["seq"], 200)["events"]
    assert [e["seq"] for e in tail] == [e["seq"] for e in all_events[-2:]]
    page = engine.events(0, 2)
    assert len(page["events"]) == 2 and page["more"]


def test_concurrent_writers_do_not_lose_updates(engine):
    """Two engine objects on one file (API process and drill CLI) serialise through the lock."""
    other = SimulatedEngine(engine.path, now=engine._now)

    def spam(e, n):
        for _ in range(n):
            e.inject("spread_spike")
            e.clear("spread_spike")

    threads = [threading.Thread(target=spam, args=(e, 20)) for e in (engine, other)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    data = json.loads(engine.path.read_text())
    injected = [e for e in data["events"] if e["kind"] == "sim_fault_injected"]
    assert len(injected) == 40
    assert [e["seq"] for e in data["events"]] == sorted({e["seq"] for e in data["events"]})
