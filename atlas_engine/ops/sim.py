"""Simulated trading engine: the operations side of the engine API, with injectable faults (H3).

The real engine (T4) waits on T0 finding an edge (PRD §26). Until then this
stands in for it, so the agent side (the atlas-operations MCP server, the cron
checks, alerts and the dashboard) can be built and drilled end to end. It holds
no strategy, places no orders and talks to no broker. Every response it serves
carries ``"source": "simulated"``.

State is one JSON file guarded by a file lock, so a drill can inject a fault
from one process while the API serves the engine from another. Health is
recomputed from the faults on every read (atlas_engine.ops.health), and each
state change is appended to the event log with a sequence number.

Operator actions (re-enable, manual kill, inject and clear faults) are methods
here and commands of ``atlas-engine-sim``; none of them is reachable through
the API. The only write the API can make is ``disable_trading``.
"""

from __future__ import annotations

import contextlib
import copy
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
from typing import Callable, Iterator

from . import health as H

SOURCE = "simulated"
MAX_EVENTS = 500
RECONCILE_INTERVAL_S = 60  # PRD §21: startup + every 60 s

# Fault name -> default parameters. `atlas-engine-sim inject <fault> key=value ...`
FAULTS: dict[str, dict] = {
    "mt5_disconnect": {"seconds": 90},
    "spread_spike": {"symbol": "EURUSD", "mult": 3.0},
    "tick_gap": {"symbol": "EURUSD", "seconds": 45},
    "recon_mismatch": {"symbol": "EURUSD", "kind": "orphan_position", "ticket": 900001},
    "clock_drift": {"seconds": 3.0},
    "db_write_failure": {},
    "heartbeat_silence": {"component": "watchdog", "seconds": 90},
    "daily_loss": {"frac": 0.80},
    "drawdown": {"frac": 0.65},
    "jev_latency": {"p95_ms": 700},
}

BASE_SPREAD_PIPS = {"EURUSD": 0.2, "GBPUSD": 0.5, "USDJPY": 0.3, "XAUUSD": 2.5}


class SimRefused(RuntimeError):
    """An operator action the simulated engine will not take in its current state."""


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _iso(t: dt.datetime) -> str:
    return t.astimezone(dt.timezone.utc).isoformat(timespec="seconds")


def _parse(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s)


class SimulatedEngine:
    def __init__(self, path: Path, now: Callable[[], dt.datetime] | None = None):
        self.path = Path(path)
        self._now = now or _utc_now
        self.source = SOURCE

    # ------------------------------------------------------------------ state file

    @staticmethod
    def initial_state(mode: str = "paper", symbols: tuple[str, ...] = ("EURUSD", "GBPUSD"),
                      now: dt.datetime | None = None) -> dict:
        at = _iso(now or _utc_now())
        return {
            "version": 1, "mode": mode, "symbols": list(symbols), "decision_provider": "rules_only",
            "faults": {},
            "trading": {"enabled": True, "by": "operator", "reason": "initial state", "at": at},
            "kill": None,
            "positions": [{"ticket": 100001, "symbol": symbols[0], "direction": 1, "risk_pct": 0.40}],
            "last_state": H.NORMAL, "last_reasons": [],
            "seq": 0, "events": [],
        }

    def init(self, mode: str = "paper", symbols: tuple[str, ...] = ("EURUSD", "GBPUSD")) -> dict:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._tx(create=True) as st:
            st.clear()
            st.update(self.initial_state(mode, symbols, self._now()))
            self._event(st, "sim_reset", mode=mode)
            return copy.deepcopy(st)

    @contextlib.contextmanager
    def _tx(self, create: bool = False) -> Iterator[dict]:
        lock = self.path.with_name(self.path.name + ".lock")
        with open(lock, "a") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            try:
                if self.path.exists():
                    st = json.loads(self.path.read_text())
                elif create:
                    st = {}
                else:
                    raise FileNotFoundError(f"no simulated engine state at {self.path}; run `atlas-engine-sim init`")
                before = json.dumps(st, sort_keys=True)
                yield st
                if json.dumps(st, sort_keys=True) != before:
                    tmp = self.path.with_name(self.path.name + ".tmp")
                    tmp.write_text(json.dumps(st, indent=1))
                    os.replace(tmp, self.path)
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)

    def _event(self, st: dict, kind: str, **detail) -> None:
        st["seq"] = st.get("seq", 0) + 1
        st.setdefault("events", []).append({"seq": st["seq"], "at": _iso(self._now()), "kind": kind, **detail})
        del st["events"][:-MAX_EVENTS]

    # ------------------------------------------------------------------ telemetry and health

    def _telemetry(self, st: dict, now: dt.datetime) -> dict:
        f = st["faults"]

        def aged(name: str, key: str = "seconds") -> float:
            fault = f[name]
            return float(fault[key]) + (now - _parse(fault["since"])).total_seconds()

        hb = {"engine": 2.0, "adapter": 2.0, "watchdog": 5.0}
        if "heartbeat_silence" in f:
            hb[f["heartbeat_silence"]["component"]] = aged("heartbeat_silence")
        symbols = {}
        for sym in st["symbols"]:
            base = BASE_SPREAD_PIPS.get(sym, 1.0)
            spike = f.get("spread_spike")
            gap = f.get("tick_gap")
            symbols[sym] = {
                "spread_pips": round(base * (float(spike["mult"]) if spike and spike["symbol"] == sym else 1.0), 3),
                "spread_median_pips": base,
                "tick_gap_s": round(aged("tick_gap"), 1) if gap and gap["symbol"] == sym else 1.0,
                "in_session": True,
            }
        return {
            "mt5_disconnected_s": round(aged("mt5_disconnect"), 1) if "mt5_disconnect" in f else None,
            "reconciliation_mismatches": 1 if "recon_mismatch" in f else 0,
            "clock_drift_s": float(f["clock_drift"]["seconds"]) if "clock_drift" in f else 0.05,
            "db_write_ok": "db_write_failure" not in f,
            "heartbeat_age_s": {k: round(v, 1) for k, v in hb.items()},
            "jev_p95_ms": float(f["jev_latency"]["p95_ms"]) if "jev_latency" in f else None,
            "daily_loss_frac_of_firm": float(f["daily_loss"]["frac"]) if "daily_loss" in f else 0.10,
            "drawdown_frac_of_firm": float(f["drawdown"]["frac"]) if "drawdown" in f else 0.05,
            "manual_kill": (st.get("kill") or {}).get("reason"),
            "symbols": symbols,
        }

    def _refresh(self, st: dict) -> tuple[dict, dict]:
        """Evaluate health, log a transition, and apply KILL (flatten + disable) like the engine would."""
        now = self._now()
        tel = self._telemetry(st, now)
        h = H.evaluate(tel)
        if h["state"] != st.get("last_state") or h["reasons"] != st.get("last_reasons"):
            self._event(st, "state_change", previous=st.get("last_state"), state=h["state"], reasons=h["reasons"])
            st["last_state"], st["last_reasons"] = h["state"], h["reasons"]
        if h["state"] == H.KILL:
            if st["positions"]:
                self._event(st, "flattened", positions=len(st["positions"]), reason=", ".join(h["reasons"]))
                st["positions"] = []
            if st["trading"]["enabled"]:
                self._set_trading(st, False, "engine", "KILL: " + ", ".join(h["reasons"]))
        return tel, h

    def _set_trading(self, st: dict, enabled: bool, by: str, reason: str) -> None:
        st["trading"] = {"enabled": enabled, "by": by, "reason": reason, "at": _iso(self._now())}
        self._event(st, "trading_enabled" if enabled else "trading_disabled", by=by, reason=reason)

    # ------------------------------------------------------------------ API-facing (EngineOps)

    def status(self) -> dict:
        with self._tx() as st:
            tel, h = self._refresh(st)
            return {
                "source": SOURCE, "as_of": _iso(self._now()), "mode": st["mode"],
                "state": h["state"], "reasons": h["reasons"],
                "trading": dict(st["trading"]),
                "new_trades_allowed": H.new_trades_allowed(h, st["trading"]["enabled"]),
                "decision_provider": st["decision_provider"],
                "mt5_connected": tel["mt5_disconnected_s"] is None,
                "clock_drift_s": tel["clock_drift_s"],
                "heartbeat_age_s": tel["heartbeat_age_s"],
                "open_positions": len(st["positions"]),
                "open_risk_pct": round(sum(p["risk_pct"] for p in st["positions"]), 2),
                "daily_loss_frac_of_firm": tel["daily_loss_frac_of_firm"],
                "drawdown_frac_of_firm": tel["drawdown_frac_of_firm"],
                "last_event_seq": st["seq"],
            }

    def health(self, symbol: str | None = None) -> dict:
        with self._tx() as st:
            tel, h = self._refresh(st)
            if symbol is not None:
                if symbol not in h["symbols"]:
                    raise KeyError(f"engine does not trade {symbol}; symbols: {', '.join(st['symbols'])}")
                h = {**h, "symbols": {symbol: h["symbols"][symbol]}}
            return {"source": SOURCE, "as_of": _iso(self._now()), **h,
                    "telemetry": {k: v for k, v in tel.items() if k != "symbols"}}

    def reconciliation(self) -> dict:
        with self._tx() as st:
            self._refresh(st)
            now = self._now()
            fault = st["faults"].get("recon_mismatch")
            mismatches = []
            broker = len(st["positions"])
            if fault:
                mismatches.append({"kind": fault["kind"], "symbol": fault["symbol"], "ticket": fault["ticket"],
                                   "detail": "position at the broker with no engine record"
                                   if fault["kind"] == "orphan_position" else "see engine journal",
                                   "since": fault["since"]})
                broker += 1 if fault["kind"] == "orphan_position" else 0
            return {"source": SOURCE, "as_of": _iso(now),
                    "reconciled_at": _iso(now - dt.timedelta(seconds=20)), "interval_s": RECONCILE_INTERVAL_S,
                    "engine_positions": len(st["positions"]), "broker_positions": broker,
                    "status": "mismatch" if mismatches else "clean", "mismatches": mismatches}

    def disable_trading(self, by: str, reason: str) -> dict:
        with self._tx() as st:
            self._refresh(st)
            already = not st["trading"]["enabled"]
            if not already:
                self._set_trading(st, False, by, reason)
            return {"source": SOURCE, "trading_enabled": False, "already_disabled": already,
                    "trading": dict(st["trading"]),
                    "note": "New trades are disabled. Open positions keep their broker-side stops. "
                            "Only the operator can re-enable, outside Hermes."}

    def events(self, since: int = 0, limit: int = 50) -> dict:
        with self._tx() as st:
            self._refresh(st)
            new = [e for e in st["events"] if e["seq"] > since]
            return {"source": SOURCE, "last_seq": st["seq"], "events": new[:limit], "more": len(new) > limit}

    # ------------------------------------------------------------------ operator and drill controls (not in the API)

    def inject(self, fault: str, **params) -> dict:
        if fault not in FAULTS:
            raise ValueError(f"unknown fault {fault!r}; known: {', '.join(FAULTS)}")
        with self._tx() as st:
            spec = {**FAULTS[fault], **params, "since": _iso(self._now())}
            st["faults"][fault] = spec
            self._event(st, "sim_fault_injected", fault=fault, params={k: v for k, v in spec.items() if k != "since"})
            self._refresh(st)
            return spec

    def clear(self, fault: str | None = None) -> list[str]:
        with self._tx() as st:
            cleared = [fault] if fault else sorted(st["faults"])
            for name in cleared:
                if st["faults"].pop(name, None) is not None:
                    self._event(st, "sim_fault_cleared", fault=name)
            self._refresh(st)
            return cleared

    def kill(self, operator: str, reason: str) -> None:
        with self._tx() as st:
            st["kill"] = {"by": operator, "reason": reason, "at": _iso(self._now())}
            self._event(st, "manual_kill", by=operator, reason=reason)
            self._refresh(st)

    def enable(self, operator: str, reason: str) -> dict:
        """Operator re-enable. Refused while the engine would still be in HALT or KILL without the manual kill."""
        with self._tx() as st:
            tel = self._telemetry(st, self._now())
            h = H.evaluate({**tel, "manual_kill": None})
            if h["state"] in (H.HALT, H.KILL):
                raise SimRefused(f"refusing to re-enable in {h['state']}: {', '.join(h['reasons'])}")
            if st.get("kill"):
                self._event(st, "kill_cleared", by=operator)
                st["kill"] = None
            self._set_trading(st, True, operator, reason)
            self._refresh(st)
            return dict(st["trading"])
