"""Engine health states from one telemetry snapshot (PRD §19, §23). Pure and deterministic.

    NORMAL    all checks green                          trade normally
    DEGRADED  Jev p95 > 500 ms; spread > 2x median;     no new trades on the affected symbol
              tick gap > 30 s in session
    HALT      MT5 disconnected > 60 s; reconciliation   no new trades on any symbol; alert
              mismatch; clock drift > 2 s; DB write
              failure; a heartbeat silent > 60 s
    KILL      internal hard daily-loss or drawdown      flatten, disable, human re-enable
              limit; manual kill

A silent heartbeat is a HALT here: §23 says 60 s of silence pages the operator
but leaves the state open, and a dead engine, adapter or watchdog is not a
state to keep opening trades in.

Telemetry (every key optional; a missing key counts as healthy):

    mt5_disconnected_s        seconds since MT5 dropped, None when connected
    reconciliation_mismatches number of open mismatches from the last reconcile
    clock_drift_s             |engine clock - broker clock|
    db_write_ok               False after a failed journal write
    heartbeat_age_s           {"engine": s, "adapter": s, "watchdog": s}
    jev_p95_ms                None when Jev is not the decision provider
    daily_loss_frac_of_firm   today's equity loss as a fraction of the firm's daily limit
    drawdown_frac_of_firm     drawdown as a fraction of the firm's max drawdown
    manual_kill               the operator's reason, or None
    symbols                   {"EURUSD": {"spread_pips", "spread_median_pips", "tick_gap_s", "in_session"}}
"""

from __future__ import annotations

from dataclasses import dataclass

NORMAL, DEGRADED, HALT, KILL = "NORMAL", "DEGRADED", "HALT", "KILL"
SEVERITY = {NORMAL: 0, DEGRADED: 1, HALT: 2, KILL: 3}


@dataclass(frozen=True)
class Thresholds:
    jev_p95_ms: float = 500.0
    spread_to_median: float = 2.0
    tick_gap_s: float = 30.0
    mt5_disconnect_s: float = 60.0
    clock_drift_s: float = 2.0
    heartbeat_silence_s: float = 60.0
    daily_loss_hard_frac: float = 0.75   # §19: hard stop at 75% of the firm's daily limit
    drawdown_stop_frac: float = 0.60     # §19: stop at 60% of the firm's max drawdown


def worst(*states: str) -> str:
    return max(states, key=SEVERITY.__getitem__, default=NORMAL)


def evaluate(t: dict, th: Thresholds = Thresholds()) -> dict:
    """{"state", "reasons", "symbols": {sym: {"state", "reasons"}}, "page_operator"}."""
    kill, halt, degraded_all = [], [], []

    if t.get("manual_kill"):
        kill.append("manual_kill")
    if (t.get("daily_loss_frac_of_firm") or 0) >= th.daily_loss_hard_frac:
        kill.append("daily_loss_hard_limit")
    if (t.get("drawdown_frac_of_firm") or 0) >= th.drawdown_stop_frac:
        kill.append("drawdown_limit")

    if (t.get("mt5_disconnected_s") or 0) > th.mt5_disconnect_s:
        halt.append("mt5_disconnected")
    if (t.get("reconciliation_mismatches") or 0) > 0:
        halt.append("reconciliation_mismatch")
    if abs(t.get("clock_drift_s") or 0) > th.clock_drift_s:
        halt.append("clock_drift")
    if t.get("db_write_ok") is False:
        halt.append("db_write_failure")
    for component, age in sorted((t.get("heartbeat_age_s") or {}).items()):
        if age is None or age > th.heartbeat_silence_s:
            halt.append(f"heartbeat_silent:{component}")

    jev = t.get("jev_p95_ms")
    if jev is not None and jev > th.jev_p95_ms:
        degraded_all.append("jev_latency")

    symbols = {}
    for sym, s in sorted((t.get("symbols") or {}).items()):
        reasons = list(degraded_all)
        median = s.get("spread_median_pips")
        if median and (s.get("spread_pips") or 0) > th.spread_to_median * median:
            reasons.append("spread_wide")
        if s.get("in_session", True) and (s.get("tick_gap_s") or 0) > th.tick_gap_s:
            reasons.append("tick_gap")
        symbols[sym] = {"state": DEGRADED if reasons else NORMAL, "reasons": reasons}

    if kill:
        state, reasons = KILL, kill + halt
    elif halt:
        state, reasons = HALT, halt
    else:
        state = worst(NORMAL, *(s["state"] for s in symbols.values()))
        reasons = sorted({r for s in symbols.values() for r in s["reasons"]}) or list(degraded_all)
        if degraded_all and not symbols:
            state = DEGRADED
    # A global state overrides every symbol's own.
    if state in (HALT, KILL):
        for s in symbols.values():
            s["state"] = state
    return {"state": state, "reasons": reasons, "symbols": symbols,
            "page_operator": state in (HALT, KILL)}


def new_trades_allowed(health: dict, trading_enabled: bool) -> dict[str, bool]:
    """Per symbol: may the engine open a new trade right now?"""
    return {sym: trading_enabled and s["state"] == NORMAL for sym, s in health["symbols"].items()}
