"""The trading engine: the deterministic loop that owns the MT5 account (PRD §1, §12 layer 2, §21, §23).

It holds no LLM and needs no agent: the agent runtime only reads it through
the operations API (atlas_api/ops.py), whose one write is ``disable_trading``.

Each ``step`` (about once a second):

1. Heartbeat, then apply any signed operator commands from the inbox.
2. Read the terminal: connection, account, positions, one tick per symbol.
   Feed the account to the risk engine (T3) and flatten when it says so.
3. Reconcile with the broker at startup and every ``reconcile_interval_s``.
4. Evaluate health (atlas_engine.ops.health, shared with H3's simulator). A
   KILL flattens and disables trading; HALT and DEGRADED only block entries.
5. Run the signal sources on closed bars and send what the risk engine
   allows (``submit``). Apply session exits.
6. Persist the restart state; a failed journal write is a HALT.

Trading starts disabled on a fresh state and only the operator can enable it.
After a restart the previous enable flag is kept, but no entry goes out
until the startup reconciliation is clean and health is NORMAL.
"""

from __future__ import annotations

import datetime as dt
import json
import statistics
import threading
from collections import deque
from pathlib import Path
from typing import Callable

from atlas_engine.adapters.broker import BrokerUnavailable
from atlas_engine.config import EngineConfig
from atlas_engine.execution import EntryOrder, ExecutionSettings, Executor, client_order_id
from atlas_engine.market_data.symbols import spec as symbol_spec
from atlas_engine.operator import ACTIONS, OperatorAuthError, verify
from atlas_engine.ops import health as H
from atlas_engine.positions.book import ManagedPosition, PositionBook, iso
from atlas_engine.reconciliation import diff, is_atlas
from atlas_engine.risk import AccountSnapshot, RiskEngine, RiskState, TradeProposal
from atlas_engine.strategies import Signal

MAX_EVENTS = 500
SPREAD_SAMPLES = 300
DRIFT_WINDOW_S = 60
STATE_KEY = "engine"


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def market_open(t: dt.datetime) -> bool:
    """The FX week: Sunday 22:00 to Friday 21:00 UTC (as atlas_engine.market_data.synthetic)."""
    wd, m = t.weekday(), t.hour * 60 + t.minute
    return not (wd == 5 or (wd == 4 and m >= 21 * 60) or (wd == 6 and m < 22 * 60))


class EngineRefused(RuntimeError):
    """An operator command the engine will not apply in its current state."""


class TradingEngine:
    def __init__(self, cfg: EngineConfig, settings: ExecutionSettings, broker, journal, state_dir: str | Path, *,
                 sources: list | None = None, now: Callable[[], dt.datetime] = utc_now, alerts=None,
                 operator_key: bytes | None = None, watchdog: Callable[[], str | None] | None = None,
                 broker_label: str = "mt5"):
        self.cfg, self.settings, self.broker, self.journal = cfg, settings, broker, journal
        self.state_dir = Path(state_dir)
        self.inbox = self.state_dir / "operator-inbox"
        self.inbox.mkdir(parents=True, exist_ok=True)
        self.sources = list(sources or [])
        self.now = now
        self.alerts = alerts
        self.operator_key = operator_key
        self.watchdog = watchdog
        self.source = f"engine:{broker_label}"
        self.executor = Executor(broker, settings)
        self.lock = threading.RLock()

        started = now()
        saved = self._load()
        self.risk = RiskEngine(cfg, RiskState.from_dict(saved["risk"]) if saved.get("risk") else None)
        self.book = PositionBook.from_dict(saved.get("book"))
        self.trading = saved.get("trading") or {"enabled": False, "by": "engine", "at": iso(started),
                                                 "reason": "fresh state: the operator must enable trading"}
        self.kill = saved.get("kill")
        self.seq = int(saved.get("seq", 0))
        self.event_log: list[dict] = saved.get("events", [])
        self.nonces: list[str] = saved.get("nonces", [])
        self.intents: dict[str, dict] = saved.get("intents", {})
        self.mismatch_since: dict[str, str] = saved.get("mismatch_since", {})
        self.requests: dict = saved.get("requests", {"day": None, "count": 0})
        self.last_state, self.last_reasons = saved.get("last_state", H.NORMAL), saved.get("last_reasons", [])
        self.friday_flattened: str | None = saved.get("friday_flattened")

        self.db_ok = True
        self.connected = False
        self.disconnected_since: dt.datetime | None = started  # until the first successful read
        self.heartbeat = {"engine": started, "adapter": None}
        self.account = None
        self.broker_positions: list = []
        self.ticks: dict = {}
        self.rules: dict = {}
        self.spreads = {s: deque(maxlen=SPREAD_SAMPLES) for s in cfg.symbols}
        self.drift: deque = deque()
        self.drift_estimate = 0.0
        self.last_tick_time: dict = {}
        self.assessment = None
        self.unresolved: list[dict] = []
        self.recon_report: dict | None = None
        self.last_reconcile: dt.datetime | None = None
        self.health_now: dict = H.evaluate({})
        self.telemetry: dict = {}
        self.requests_seen = getattr(broker, "requests_sent", 0)
        self.halt_extra: list[str] = []
        self.last_step: dt.datetime | None = None
        self.limits_failed = False

    # ================================================================== lifecycle

    def start(self) -> None:
        with self.lock:
            try:
                self.broker.connect()
            except BrokerUnavailable as e:
                self._event("broker_unavailable", detail=str(e))
            self._event("engine_started", mode=self.cfg.mode, config=self.cfg.checksum[:12],
                        trading_enabled=self.trading["enabled"], positions=len(self.book))
            self.step(reconcile=True)

    def step(self, reconcile: bool = False) -> dict:
        with self.lock:
            now = self.now()
            self.heartbeat["engine"] = now
            self.last_step = now
            self._operator_inbox(now)
            self._collect(now)
            due = self.last_reconcile is None or \
                (now - self.last_reconcile).total_seconds() >= self.settings.reconcile_interval_s
            if self.connected and (reconcile or due):
                self.reconcile(now)
            h = self._evaluate(now)
            self._write_watchdog_limits(now)
            if self.connected:
                self._session_exits(now)
                if self.trading["enabled"] and h["state"] in (H.NORMAL, H.DEGRADED):
                    for source in self.sources:
                        try:
                            signals = source.poll(self.broker, now)
                        except BrokerUnavailable as e:
                            self._event("source_failed", source=source.name, detail=str(e))
                            continue
                        for sig in signals:
                            self.submit(sig)
            self._persist()
            if not self.db_ok and h["state"] not in (H.HALT, H.KILL):
                h = self._evaluate(now)  # a failed write this step is a HALT now, not next step
            return h

    def run_forever(self, poll_s: float = 1.0, stop: threading.Event | None = None) -> None:
        stop = stop or threading.Event()
        self.start()
        last_error = None
        while not stop.wait(poll_s):
            try:
                self.step()
                last_error = None
            except Exception as e:  # noqa: BLE001 - the loop must keep its heartbeat going
                if repr(e) != last_error:  # one alert per distinct failure, not one per second
                    last_error = repr(e)
                    self._event("step_failed", detail=last_error)
                    self._alert("critical", f"engine step failed: {last_error}")

    # ================================================================== reading the broker

    def _collect(self, now: dt.datetime) -> None:
        try:
            if not self.connected:
                self.broker.connect()  # re-attach after a terminal restart; harmless when attached
            self.connected = self.broker.connected()
            if not self.connected:
                raise BrokerUnavailable("terminal not connected to the trade server")
            self.account = self.broker.account()
            self.broker_positions = self.broker.positions()
            for sym in self.cfg.symbols:
                if sym not in self.rules:
                    self.rules[sym] = self.broker.symbol_rules(sym)
                self._observe_tick(self.broker.tick(sym), now)
            self.heartbeat["adapter"] = now
            self.disconnected_since = None
        except BrokerUnavailable as e:
            if self.connected or self.disconnected_since is None:
                self._event("broker_disconnected", detail=str(e))
            self.connected = False
            # Measured from the last good read, not from when this step noticed.
            self.disconnected_since = self.disconnected_since or self.heartbeat["adapter"] or now
            return
        # Requests against the firm's daily cap (FTMO: 2,000 per day).
        day = str(self.cfg.prop.server_day(now))
        if self.requests.get("day") != day:
            self.requests = {"day": day, "count": 0}
        sent = getattr(self.broker, "requests_sent", 0)
        self.requests["count"] += max(sent - self.requests_seen, 0)
        self.requests_seen = sent

        self._close_missing(now)  # SL/TP fills and outside closes since the last step
        snap = self._snapshot(now)
        a = self.risk.observe(snap)
        self.assessment = a
        self._drain_risk_events()
        if a.flatten and self._atlas_positions():
            self._flatten(now, f"risk engine: {a.status}")

    def _observe_tick(self, tick, now: dt.datetime) -> None:
        sym = tick.symbol
        self.ticks[sym] = tick
        self.spreads[sym].append(tick.spread)
        if market_open(now) and self.last_tick_time.get(sym) != tick.time:
            self.drift.append((now, (now - tick.time).total_seconds()))  # a fresh tick: its age is ~latency + drift
        self.last_tick_time[sym] = tick.time
        while self.drift and (now - self.drift[0][0]).total_seconds() > DRIFT_WINDOW_S:
            self.drift.popleft()
        if self.drift:
            # The freshest tick bounds the drift: its age is latency plus drift, and a negative age
            # (a tick from our future) can only be drift.
            self.drift_estimate = min(a for _, a in self.drift)

    def _rates(self) -> dict[str, float]:
        return {s: (t.bid + t.ask) / 2 for s, t in self.ticks.items()}

    def _spec(self, symbol: str):
        if symbol not in self.rules:
            self.rules[symbol] = self.broker.symbol_rules(symbol)
        return self.rules[symbol].spec

    def _risk_positions(self) -> tuple:
        return tuple(p.to_risk(self._spec(p.symbol), self.cfg.currency, self._rates()) for p in self.book)

    def _snapshot(self, now: dt.datetime) -> AccountSnapshot:
        return AccountSnapshot(now, self.account.balance, self.account.equity, self._risk_positions())

    def _atlas_positions(self) -> list:
        return [p for p in self.broker_positions if self.book.get(p.ticket) is not None
                or is_atlas(p, self.settings.magic_base)]

    # ================================================================== health

    def _loss_fractions(self) -> tuple[float, float]:
        if self.account is None:
            return 0.0, 0.0
        s, p = self.risk.state, self.cfg.prop
        eq = self.account.equity
        daily_amt = p.daily_loss_amount(s.initial_balance, s.day_start_balance, s.day_start_equity)
        ref = max(s.day_start_balance, s.day_start_equity)
        max_amt = p.max_loss_amount(s.initial_balance)
        dd_ref = p.max_loss_reference(s.initial_balance, s.high_water)
        return max((ref - eq) / daily_amt, 0.0) if daily_amt else 0.0, max((dd_ref - eq) / max_amt, 0.0)

    def _telemetry(self, now: dt.datetime) -> dict:
        daily, dd = self._loss_fractions()
        halt = []
        if self.cfg.changed_files():
            halt.append("config_changed")
        if self.account is not None and not self.account.trade_allowed:
            halt.append("broker_trade_disabled")
        cap = self.cfg.prop.max_server_requests_per_day
        if cap and self.requests.get("count", 0) >= self.settings.request_budget_share * cap:
            halt.append("request_budget")
        if self.last_reconcile is None:
            halt.append("reconciliation_pending")
        if self.assessment is not None and self.assessment.status == "firm_breached":
            halt.append("firm_breached")
        hb = {"engine": (now - self.heartbeat["engine"]).total_seconds(),
              "adapter": None if self.heartbeat["adapter"] is None
              else (now - self.heartbeat["adapter"]).total_seconds()}
        wd = self._watchdog(now)
        if wd is not None or self.settings.require_watchdog:
            hb["watchdog"] = wd
        symbols = {}
        open_ = market_open(now)
        for sym in self.cfg.symbols:
            t = self.ticks.get(sym)
            pip = symbol_spec(sym).pip
            med = statistics.median(self.spreads[sym]) if self.spreads[sym] else None
            symbols[sym] = {"spread_pips": round(t.spread / pip, 3) if t else None,
                            "spread_median_pips": round(med / pip, 3) if med else None,
                            "tick_gap_s": round((now - t.time).total_seconds(), 1) if t else None,
                            "in_session": open_}
        return {
            "mt5_disconnected_s": None if self.disconnected_since is None
            else round((now - self.disconnected_since).total_seconds(), 1),
            "reconciliation_mismatches": len(self.unresolved),
            "clock_drift_s": round(self.drift_estimate, 3),
            "db_write_ok": self.db_ok,
            "heartbeat_age_s": {k: None if v is None else round(v, 1) for k, v in hb.items()},
            "jev_p95_ms": None,
            "daily_loss_frac_of_firm": round(daily, 4),
            "drawdown_frac_of_firm": round(dd, 4),
            "manual_kill": (self.kill or {}).get("reason"),
            "halt_reasons": halt,
            "symbols": symbols,
        }

    def _watchdog(self, now: dt.datetime) -> float | None:
        """Age of the watchdog EA's heartbeat; a FLATTENED state from the EA becomes a kill."""
        if self.watchdog is None:
            return None
        try:
            line = self.watchdog()
        except OSError:
            return None
        if not line:
            return None
        parts = line.split()
        if len(parts) < 4 or parts[0] != "ATLAS-WD":
            return None
        age = (now - dt.datetime.fromtimestamp(int(parts[2]), dt.timezone.utc)).total_seconds()
        if parts[4:5] == ["FLATTENED"] and not self.kill:
            self.kill = {"by": "watchdog", "reason": "watchdog EA flattened on a hard equity breach",
                         "at": iso(now)}
            self._event("watchdog_flattened", equity=parts[3])
            self._alert("critical", "The watchdog EA flattened the account on a hard equity breach.")
        return age

    def _write_watchdog_limits(self, now: dt.datetime) -> None:
        """The firm floors for the watchdog EA (watchdog/AtlasWatchdog.mq5), next to its heartbeat file."""
        hb = self.settings.watchdog_heartbeat_file
        if not hb or self.account is None:
            return
        s, p = self.risk.state, self.cfg.prop
        lines = self.risk.lines()
        server_date = self.broker.clock.server_wall(now).strftime("%Y.%m.%d")
        text = (f"ATLAS-LIMITS 1 {server_date} {lines['firm_daily_floor']:.2f} "
                f"{p.daily_loss_amount(s.initial_balance, s.day_start_balance, s.day_start_equity):.2f} "
                f"{lines['firm_max_loss_floor']:.2f} {p.max_loss_amount(s.initial_balance):.2f} {int(now.timestamp())}\r\n")
        path = Path(hb).with_name("atlas_limits.txt")
        try:
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(text, encoding="ascii")
            tmp.replace(path)
            self.limits_failed = False
        except OSError as e:
            if not self.limits_failed:
                self.limits_failed = True
                self._event("watchdog_limits_write_failed", detail=str(e))

    def _evaluate(self, now: dt.datetime) -> dict:
        tel = self._telemetry(now)
        h = H.evaluate(tel)
        if h["state"] != self.last_state or h["reasons"] != self.last_reasons:
            self._event("state_change", previous=self.last_state, state=h["state"], reasons=h["reasons"])
            if H.SEVERITY[h["state"]] >= H.SEVERITY[H.HALT] or self.last_state in (H.HALT, H.KILL):
                self._alert("critical" if h["state"] in (H.HALT, H.KILL) else "info",
                            f"Engine state {self.last_state} -> {h['state']}: {', '.join(h['reasons']) or 'all checks green'}")
            self.last_state, self.last_reasons = h["state"], h["reasons"]
        if h["state"] == H.KILL:
            if self._atlas_positions():
                self._flatten(now, "KILL: " + ", ".join(h["reasons"]))
            if self.trading["enabled"]:
                self._set_trading(False, "engine", "KILL: " + ", ".join(h["reasons"]))
        self.telemetry, self.health_now = tel, h
        return h

    # ================================================================== entries

    def submit(self, sig: Signal) -> dict:
        """Take one signal through health, the risk engine and execution. Returns the journal record."""
        with self.lock:
            now = self.now()
            did = sig.decision_id
            cid = client_order_id(did)
            rec = {"decision_id": did, "client_id": cid, "symbol": sig.symbol, "setup": sig.setup,
                   "version": sig.version, "direction": sig.direction, "stop": sig.stop,
                   "decision_time": sig.decision_time.isoformat()}
            reasons = []
            if not self.trading["enabled"]:
                reasons.append("trading_disabled")
            allowed = H.new_trades_allowed(self.health_now, self.trading["enabled"])
            if sig.symbol not in allowed:
                reasons.append("symbol_not_traded")
            elif not allowed[sig.symbol] and self.trading["enabled"]:
                reasons.append(f"health_{self.health_now['symbols'][sig.symbol]['state'].lower()}")
            if not self.connected or self.account is None:
                reasons.append("broker_unavailable")
            if not self.db_ok:
                reasons.append("journal_unavailable")
            if self.book.by_client_id(cid) is not None or cid in self.intents:
                reasons.append("decision_already_handled")
            if reasons:
                return self._decided(rec, "skipped", reasons, now)

            try:
                tick = self.broker.tick(sig.symbol)
                rules = self.broker.symbol_rules(sig.symbol)
            except BrokerUnavailable as e:
                return self._decided(rec, "skipped", [f"broker_unavailable: {e}"], now)
            self.rules[sig.symbol] = rules
            entry = tick.price(sig.direction)
            proposal = TradeProposal(sig.symbol, sig.setup, sig.direction, entry, sig.stop, sig.ev_scale,
                                     self._rates(), spec=rules.spec)
            decision = self.risk.check_entry(proposal, self._snapshot(now))
            self._drain_risk_events()
            self._journal("risk_checks", decision.to_dict(), now, decision_id=did)
            if not decision.allowed:
                return self._decided(rec, "risk_denied", list(decision.reasons), now)

            risk_dist = abs(entry - sig.stop)
            target = rules.round_price(entry + sig.direction * sig.rr * risk_dist)
            order = EntryOrder(did, sig.symbol, sig.setup, sig.direction, decision.volume, rules.round_price(sig.stop),
                               target, self.settings.magic_base + sig.magic_offset, entry)
            # Recorded before sending, so a fill lost to a crash or timeout is adopted, not closed as an orphan.
            self.intents[cid] = {**rec, "volume": decision.volume, "target": target, "magic": order.magic,
                                 "risk_amount": decision.risk_amount, "at": iso(now), "status": "sending"}
            self._journal("trade_intents", self.intents[cid], now, decision_id=did)
            self._persist()
            if not self.db_ok:  # never send an order the journal can't record
                self.intents[cid]["status"] = "not_sent"
                return self._decided(rec, "skipped", ["journal_unavailable"], now)
            try:
                res = self.executor.open(order, now)
            except BrokerUnavailable as e:
                # Outcome unknown: the intent stays journaled, so reconciliation adopts a fill if one happened.
                self.intents[cid]["status"] = "unknown"
                self._event("order_outcome_unknown", symbol=sig.symbol, setup=sig.setup, detail=str(e))
                self._persist()
                return self._decided(rec, "unknown", [f"broker_unavailable: {e}"], now)
            self.intents[cid]["status"] = res.status
            self._journal("orders", res.to_dict(), now, decision_id=did)
            if res.opened and res.ticket is not None and self.book.get(res.ticket) is None:
                self.book.add(ManagedPosition(res.ticket, cid, did, sig.symbol, sig.setup, sig.direction, res.volume,
                                              res.price or entry, res.sl or order.stop, res.tp or target, order.magic,
                                              iso(now)))
                self.risk.record_open(now)
                self._journal("fills", res.to_dict(), now, decision_id=did, trade_id=str(res.ticket))
                self._event("order_filled", ticket=res.ticket, symbol=sig.symbol, setup=sig.setup, volume=res.volume,
                            price=res.price, slippage_points=res.slippage_points, status=res.status)
            elif not res.opened:
                self._event("order_not_filled", symbol=sig.symbol, setup=sig.setup, status=res.status,
                            reason=res.reason)
                if res.status == "unprotected_closed":
                    self._alert("critical", f"{sig.symbol} fill had no SL at the broker and was closed: {res.reason}")
            self.broker_positions = self._safe_positions()
            self._persist()
            return self._decided(rec, res.status, [res.reason] if res.reason else [], now)

    def _decided(self, rec: dict, outcome: str, reasons: list[str], now: dt.datetime) -> dict:
        out = {**rec, "outcome": outcome, "reasons": reasons}
        self._journal("decisions", out, now, decision_id=rec["decision_id"])
        return out

    def _safe_positions(self) -> list:
        try:
            return self.broker.positions()
        except BrokerUnavailable:
            return self.broker_positions

    def move_stop(self, ticket: int, new_sl: float, bar: dt.datetime) -> dict:
        """Tighten a managed position's stop, at most once per bar (PRD §18). For exit policies (T1)."""
        with self.lock:
            mine = self.book.get(ticket)
            if mine is None:
                return {"status": "rejected", "reason": "not an engine position"}
            if mine.last_stop_bar == bar.isoformat():
                return {"status": "rejected", "reason": "stop already moved this bar"}
            pos = self.broker.position(ticket)
            if pos is None:
                return {"status": "rejected", "reason": "position is gone at the broker"}
            res = self.executor.modify_stop(pos, new_sl)
            if res.status == "filled":
                self.book.update(ticket, stop=new_sl, last_stop_bar=bar.isoformat())
                self._event("stop_moved", ticket=ticket, sl=new_sl)
                self._persist()
            return res.to_dict()

    # ================================================================== exits

    def _session_exits(self, now: dt.datetime) -> None:
        cutoff = self.settings.friday_flatten_utc
        if not cutoff or now.weekday() != 4:
            return
        h, m = (int(x) for x in cutoff.split(":"))
        today = now.date().isoformat()
        if (now.hour, now.minute) >= (h, m) and self.friday_flattened != today:
            if self._atlas_positions():
                self._flatten(now, "friday_flatten")
            self.friday_flattened = today

    def _flatten(self, now: dt.datetime, reason: str) -> list[dict]:
        targets = self._atlas_positions()
        results = self.executor.flatten(targets, reason)
        for pos, res in zip(targets, results):
            self._journal("orders", {**res.to_dict(), "action": "close"}, now, trade_id=str(pos.ticket))
        failed = [r for r in results if r.status != "closed"]
        self._event("flattened", positions=len(targets), failed=len(failed), reason=reason)
        self._alert("critical" if failed else "warning",
                    f"Flattened {len(targets) - len(failed)} of {len(targets)} ATLAS positions: {reason}"
                    + (f"; {len(failed)} failed and will be retried" if failed else ""))
        self.broker_positions = self._safe_positions()
        self._close_missing(now)
        return [r.to_dict() for r in results]

    def _close_missing(self, now: dt.datetime) -> list[dict]:
        """Book positions gone at the broker: record them closed from the deal history."""
        live = {p.ticket for p in self.broker_positions}
        closed, gone = [], [p for p in self.book if p.ticket not in live]
        if not gone:
            return closed
        try:
            deals = self.broker.recent_deals(now)
        except BrokerUnavailable:
            return closed
        for mine in gone:
            outs = [d for d in deals if d.position_id == mine.ticket and d.entry in ("out", "out_by")]
            if not outs:
                continue
            ins = [d for d in deals if d.position_id == mine.ticket and d.entry == "in"]
            pnl = sum(d.net for d in outs) + sum(d.commission for d in ins)
            exit_px = outs[-1].price
            spec = self._spec(mine.symbol)
            risk0 = self.intents.get(mine.client_id, {}).get("risk_amount") or mine.risk_amount(spec, self.cfg.currency,
                                                                                               self._rates())
            trade = {**mine.to_dict(), "exit": exit_px, "exit_time": outs[-1].time.isoformat(),
                     "exit_reason": outs[-1].reason, "pnl": round(pnl, 2),
                     "r": round(pnl / risk0, 3) if risk0 else None}
            self.book.remove(mine.ticket)
            self.risk.record_close(now, pnl)
            self._drain_risk_events()
            self._journal("trades", trade, now, decision_id=mine.decision_id, trade_id=str(mine.ticket))
            self._event("trade_closed", ticket=mine.ticket, symbol=mine.symbol, pnl=round(pnl, 2),
                        reason=outs[-1].reason)
            closed.append(trade)
        return closed

    # ================================================================== reconciliation

    def reconcile(self, now: dt.datetime | None = None) -> dict:
        with self.lock:
            now = now or self.now()
            self.broker_positions = self._safe_positions()
            self._close_missing(now)
            points = {s: r.point for s, r in self.rules.items()}
            actions, unresolved = [], []
            for f in diff(self.book, self.broker_positions, self.settings.magic_base, points):
                done, what = self._resolve(f, now)
                entry = {"kind": f.kind, "symbol": f.symbol, "ticket": f.ticket, "detail": f.detail, "action": what}
                (actions if done else unresolved).append(entry)
            self.broker_positions = self._safe_positions()
            keys = set()
            for u in unresolved:
                key = f"{u['kind']}:{u['ticket']}"
                keys.add(key)
                if key not in self.mismatch_since:
                    self.mismatch_since[key] = iso(now)
                    self._alert("critical", f"Reconciliation mismatch on {u['symbol']} ticket {u['ticket']}: "
                                            f"{u['kind']} ({u['detail']}). {u['action']}")
                u["since"] = self.mismatch_since[key]
            self.mismatch_since = {k: v for k, v in self.mismatch_since.items() if k in keys}
            for a in actions:
                self._event("reconcile_action", finding=a["kind"], **{k: v for k, v in a.items() if k != "kind"})
                self._journal("system_events", {"event": "reconcile_action", **a}, now)
            self.unresolved = unresolved
            self.last_reconcile = now
            self.recon_report = {
                "reconciled_at": iso(now), "interval_s": self.settings.reconcile_interval_s,
                "engine_positions": len(self.book), "broker_positions": len(self.broker_positions),
                "status": "mismatch" if unresolved else "clean", "mismatches": unresolved, "actions": actions,
            }
            return self.recon_report

    def _resolve(self, f, now: dt.datetime) -> tuple[bool, str]:
        b = f.broker
        if f.kind == "foreign_position":
            return False, "not an ATLAS position; the operator must close or explain it"
        if f.kind == "missing_position":
            return False, "no closing deal in the broker history; check the terminal's history"
        if f.kind == "orphan_position":
            intent = self.intents.get(b.comment)
            if intent is not None:
                self.book.add(ManagedPosition(b.ticket, b.comment, intent["decision_id"], b.symbol, intent["setup"],
                                              b.direction, b.volume, b.price_open, b.sl or intent["stop"],
                                              b.tp or intent["target"], b.magic, iso(b.time)))
                self.risk.record_open(now)
                if not b.sl or not b.tp:
                    self.executor.restore_stops(b, intent["stop"], intent["target"])
                intent["status"] = "adopted"
                return True, "adopted: the journal has its order"
            if self.settings.orphan_policy == "attach_sl" and b.sl:
                return False, "unknown ATLAS position with an SL; left for the operator (attach_sl policy)"
            res = self.executor.close(b, "orphan")
            return (res.status == "closed"), f"closed as an orphan ({res.status}: {res.reason})"
        mine = self.book.get(f.ticket)
        if f.kind == "stop_tightened":
            self.book.update(f.ticket, stop=b.sl)
            return True, "adopted the broker's tighter stop"
        if f.kind == "volume_mismatch":
            self.book.update(f.ticket, volume=b.volume)
            return True, "adopted the broker's volume"
        if f.kind in ("stop_missing", "stop_loosened", "target_mismatch"):
            ok = self.executor.restore_stops(b, mine.stop, mine.target)
            if ok:
                return True, "restored the book's SL/TP at the broker"
            res = self.executor.close(b, f"{f.kind}_unfixable")
            if res.status == "closed":
                return True, "could not restore the SL/TP, so the position was closed"
            return False, "could not restore the SL/TP or close the position"
        return False, "no rule for this finding"

    # ================================================================== operator

    def _operator_inbox(self, now: dt.datetime) -> None:
        files = sorted(self.inbox.glob("*.json"))
        if not files:
            return
        for path in files:
            outcome, detail, cmd, raw = "rejected", "", {}, None
            try:
                raw = json.loads(path.read_text())
                if self.operator_key is None:
                    raise OperatorAuthError("no operator key configured on the engine host")
                cmd = verify(self.operator_key, raw, now, set(self.nonces))
                self.nonces = (self.nonces + [cmd["nonce"]])[-1000:]
                detail = self.apply_operator(cmd["action"], cmd["operator"], cmd["reason"])
                outcome = "applied"
            except (OperatorAuthError, EngineRefused, ValueError) as e:
                detail = str(e)
                if isinstance(raw, dict):
                    cmd = raw
            done = self.inbox / ("applied" if outcome == "applied" else "rejected")
            done.mkdir(exist_ok=True)
            path.replace(done / path.name)
            rec = {"action": cmd.get("action"), "operator": cmd.get("operator"), "outcome": outcome, "detail": detail}
            self._journal("operator_commands", rec, now)
            self._event("operator_command", **rec)
            self._alert("warning" if outcome == "applied" else "critical",
                        f"Operator command {rec['action']} by {rec['operator']}: {outcome} ({detail})")

    def apply_operator(self, action: str, operator: str, reason: str) -> str:
        """Apply an operator action. Only reachable through a verified signed command."""
        if action not in ACTIONS:
            raise EngineRefused(f"unknown action {action!r}")
        now = self.now()
        if action == "enable_trading":
            h = H.evaluate({**self._telemetry(now), "manual_kill": None})
            if h["state"] in (H.HALT, H.KILL):
                raise EngineRefused(f"refusing to enable trading in {h['state']}: {', '.join(h['reasons'])}")
            if self.kill:
                raise EngineRefused("a manual kill is active; clear_kill first")
            self._set_trading(True, operator, reason)
            return "trading enabled"
        if action == "kill":
            self.kill = {"by": operator, "reason": reason, "at": iso(now)}
            self._event("manual_kill", by=operator, reason=reason)
            self._evaluate(now)  # flattens and disables
            return "killed: flattened and disabled"
        if action == "clear_kill":
            self.kill = None
            self._event("kill_cleared", by=operator)
            return "kill cleared; trading stays disabled until enable_trading"
        if action == "flatten":
            res = self._flatten(now, f"operator {operator}: {reason}")
            return f"flatten sent for {len(res)} positions"
        if action == "reenable_drawdown":
            try:
                self.risk.operator_reenable(now, operator)
            except PermissionError as e:
                raise EngineRefused(str(e)) from None
            self._drain_risk_events()
            return "drawdown stop cleared"
        raise EngineRefused(f"unhandled action {action!r}")

    def _set_trading(self, enabled: bool, by: str, reason: str) -> None:
        self.trading = {"enabled": enabled, "by": by, "reason": reason, "at": iso(self.now())}
        self._event("trading_enabled" if enabled else "trading_disabled", by=by, reason=reason)

    # ================================================================== operations API (EngineOps, atlas_api/ops.py)

    def status(self) -> dict:
        with self.lock:
            h, tel = self.health_now, self.telemetry
            a = self.account
            open_risk = sum(p.risk_amount for p in self._risk_positions()) if a else 0.0
            return {
                "source": self.source, "as_of": iso(self.now()), "mode": self.cfg.mode,
                "state": h["state"], "reasons": h["reasons"], "trading": dict(self.trading),
                "new_trades_allowed": H.new_trades_allowed(h, self.trading["enabled"]),
                "decision_provider": "rules_only",
                "strategies": [s.name for s in self.sources],
                "mt5_connected": self.connected,
                "clock_drift_s": tel.get("clock_drift_s"),
                "heartbeat_age_s": tel.get("heartbeat_age_s", {}),
                "open_positions": len(self.book),
                "open_risk_pct": round(open_risk / a.equity * 100, 2) if a and a.equity else 0.0,
                "daily_loss_frac_of_firm": tel.get("daily_loss_frac_of_firm"),
                "drawdown_frac_of_firm": tel.get("drawdown_frac_of_firm"),
                "risk_status": self.assessment.status if self.assessment else None,
                "account": {"balance": a.balance, "equity": a.equity, "demo": a.demo, "server": a.server}
                if a else None,
                "requests_today": self.requests.get("count", 0),
                "settings_defaults_in_use": list(self.settings.defaults_used),
                "last_event_seq": self.seq,
            }

    def health(self, symbol: str | None = None) -> dict:
        with self.lock:
            h = self.health_now
            if symbol is not None:
                if symbol not in h["symbols"]:
                    raise KeyError(f"engine does not trade {symbol}; symbols: {', '.join(self.cfg.symbols)}")
                h = {**h, "symbols": {symbol: h["symbols"][symbol]}}
            return {"source": self.source, "as_of": iso(self.now()), **h,
                    "telemetry": {k: v for k, v in self.telemetry.items() if k != "symbols"}}

    def reconciliation(self) -> dict:
        with self.lock:
            base = self.recon_report or {"reconciled_at": None, "interval_s": self.settings.reconcile_interval_s,
                                         "engine_positions": len(self.book), "broker_positions": None,
                                         "status": "mismatch", "actions": [],
                                         "mismatches": [{"kind": "not_reconciled", "symbol": None, "ticket": None,
                                                         "detail": "no reconciliation has run yet", "since": None}]}
            return {"source": self.source, "as_of": iso(self.now()), **base}

    def events(self, since: int = 0, limit: int = 50) -> dict:
        with self.lock:
            new = [e for e in self.event_log if e["seq"] > since]
            return {"source": self.source, "last_seq": self.seq, "events": new[:limit], "more": len(new) > limit}

    def disable_trading(self, by: str, reason: str) -> dict:
        with self.lock:
            already = not self.trading["enabled"]
            if not already:
                self._set_trading(False, by, reason)
                self._alert("warning", f"Trading disabled by {by}: {reason}")
                self._persist()
            return {"source": self.source, "trading_enabled": False, "already_disabled": already,
                    "trading": dict(self.trading),
                    "note": "New trades are disabled. Open positions keep their broker-side stops. "
                            "Only the operator can re-enable, with a signed command on the engine host."}

    # ================================================================== journal, events, state

    def _event(self, kind: str, **detail) -> None:
        self.seq += 1
        self.event_log.append({"seq": self.seq, "at": iso(self.now()), "kind": kind, **detail})
        del self.event_log[:-MAX_EVENTS]
        self._journal("system_events", {"event": kind, **detail}, self.now())

    def _alert(self, severity: str, text: str) -> None:
        if self.alerts is not None:
            self.alerts.alert(severity, text, self.now())

    def _drain_risk_events(self) -> None:
        events, self.risk.events = self.risk.events, []
        for e in events:
            self._event("risk_" + e.pop("event"), **{k: v for k, v in e.items() if k != "time"})

    def _journal(self, table: str, record: dict, at: dt.datetime, **ids) -> None:
        from atlas_engine.journal import JournalError
        try:
            self.journal.write(table, record, at, **ids)
        except JournalError:
            self._db_failed()

    def _db_failed(self) -> None:
        if self.db_ok:
            self.db_ok = False
            self.seq += 1
            self.event_log.append({"seq": self.seq, "at": iso(self.now()), "kind": "db_write_failure"})

    def _persist(self) -> None:
        from atlas_engine.journal import JournalError
        state = {"risk": self.risk.state.to_dict(), "book": self.book.to_dict(), "trading": self.trading,
                 "kill": self.kill, "seq": self.seq, "events": self.event_log[-MAX_EVENTS:], "nonces": self.nonces,
                 "intents": dict(list(self.intents.items())[-500:]), "mismatch_since": self.mismatch_since,
                 "requests": self.requests, "last_state": self.last_state, "last_reasons": self.last_reasons,
                 "friday_flattened": self.friday_flattened}
        try:
            if not self.db_ok:
                self.journal.probe()
            self.journal.save_state(STATE_KEY, state)
            if not self.db_ok:
                self.db_ok = True
                self._event("db_write_recovered")
        except JournalError:
            self._db_failed()

    def _load(self) -> dict:
        return self.journal.load_state(STATE_KEY) or {}

