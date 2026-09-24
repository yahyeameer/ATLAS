"""T4 failure-injection scenarios (PRD §26 T4 exit gate: "failure-injection suite passes on demo").

Each scenario builds an engine on the fake MT5 terminal, injects one failure
and asserts what the engine must do. pytest runs them
(test_t4_failure_injection.py) and so does the gate script (t4_gate.py). The
PRD wants them passed on a demo account; here they run against the fake
terminal, and the demo run is still owed (docs/t4-engine-and-mt5.md).
"""

from __future__ import annotations

import datetime as dt
import json
import shutil
from pathlib import Path

import yaml

from atlas_api.auth import TokenStore, hash_token
from atlas_api.http import dispatch
from atlas_api.ops import ENGINE_SCOPES, OPS_ROUTES, OpsService
from atlas_engine.adapters.mt5 import ServerClock
from atlas_engine.adapters.mt5.fake import FakeMT5
from atlas_engine.execution import ExecutionSettings, client_order_id
from atlas_engine.operator import sign

from t4help import KEY, REPO_CONFIG, UTC, Clock, build

M = FakeMT5  # retcode constants


def _open(r, **kw):
    r.enable()
    out = r.engine.submit(r.signal(**kw))
    assert out["outcome"] == "filled", out
    return out


def _requests(r, action=1):
    return [q for q in r.mt5.requests if q["action"] == action]


# ---------------------------------------------------------------------------- orders


def clean_entry(tmp: Path):
    r = build(tmp)
    assert not r.engine.trading["enabled"], "a fresh engine must start with trading disabled"
    out = _open(r)
    [pos] = r.adapter.positions()
    [req] = _requests(r)
    assert pos.sl and pos.tp, "SL and TP must be broker-side from the first request"
    assert req["sl"] == pos.sl and req["tp"] == pos.tp and req["deviation"] == 2
    assert req["type_filling"] == M.ORDER_FILLING_IOC and req["magic"] == 26_090_000
    assert pos.comment == out["client_id"] == client_order_id(out["decision_id"])
    assert r.engine.status()["open_positions"] == 1 and r.engine.risk.state.trades_today == 1


def duplicate_decision(tmp: Path):
    r = build(tmp)
    r.enable()
    sig = r.signal()
    assert r.engine.submit(sig)["outcome"] == "filled"
    again = r.engine.submit(sig)
    assert again["outcome"] == "skipped" and "decision_already_handled" in again["reasons"]
    assert len(r.adapter.positions()) == 1 and len(_requests(r)) == 1


def timeout_after_fill(tmp: Path):
    r = build(tmp)
    r.enable()
    r.mt5.inject("timeout_filled")
    out = r.engine.submit(r.signal())
    assert out["outcome"] == "filled", out
    assert len(_requests(r)) == 1, "must look up the client order ID, not resend"
    assert len(r.adapter.positions()) == 1 and len(r.engine.book) == 1


def timeout_without_fill(tmp: Path):
    r = build(tmp)
    r.enable()
    r.mt5.inject("timeout_lost")
    out = r.engine.submit(r.signal())
    assert out["outcome"] == "filled", out
    assert len(_requests(r)) == 2, "one look, then exactly one retry"
    assert len(r.adapter.positions()) == 1


def terminal_dies_mid_order(tmp: Path):
    r = build(tmp)
    r.enable()
    sig = r.signal()
    real = r.mt5.order_send

    def fill_then_die(req):
        res = real(req)
        r.mt5.terminal_alive = False  # the fill happened; the answer and every lookup are lost
        return res
    r.mt5.order_send = fill_then_die
    out = r.engine.submit(sig)
    assert out["outcome"] == "unknown", out
    r.mt5.order_send = real
    r.mt5.terminal_alive = True
    r.step(61)
    [pos] = r.adapter.positions()
    assert r.engine.book.get(pos.ticket) is not None, "the journaled intent lets reconciliation adopt the fill"
    assert r.engine.reconciliation()["status"] == "clean"


def two_timeouts_give_up(tmp: Path):
    r = build(tmp)
    r.enable()
    r.mt5.inject("timeout_lost")
    r.mt5.inject("timeout_lost")
    out = r.engine.submit(r.signal())
    assert out["outcome"] == "rejected" and len(_requests(r)) == 2 and not r.adapter.positions()


def requote_then_fill(tmp: Path):
    r = build(tmp)
    r.enable()
    r.mt5.inject("retcode", code=M.TRADE_RETCODE_REQUOTE)
    assert r.engine.submit(r.signal())["outcome"] == "filled"
    assert len(_requests(r)) == 2


def requote_price_ran_away(tmp: Path):
    r = build(tmp)
    r.enable()
    sig = r.signal()
    r.mt5.inject("retcode", code=M.TRADE_RETCODE_REQUOTE)
    orig = r.mt5.set_price
    moved = []

    def send_then_move(req):
        res = FakeMT5.order_send(r.mt5, req)
        if not moved:
            moved.append(1)
            orig("EURUSD", 1.17010)  # 10 points away: past the 2-point deviation
        return res
    r.mt5.order_send = send_then_move
    out = r.engine.submit(sig)
    assert out["outcome"] == "rejected" and "deviation" in out["reasons"][0], out
    assert not r.adapter.positions() and len(_requests(r)) == 1


def broker_rejects(tmp: Path):
    for code in (M.TRADE_RETCODE_NO_MONEY, M.TRADE_RETCODE_MARKET_CLOSED, M.TRADE_RETCODE_REJECT):
        r = build(tmp / str(code))
        r.enable()
        r.mt5.inject("retcode", code=code)
        out = r.engine.submit(r.signal())
        assert out["outcome"] == "rejected" and str(code) in out["reasons"][0], out
        assert len(_requests(r)) == 1 and not r.adapter.positions(), "no retry on a hard reject"


def stop_inside_stops_level(tmp: Path):
    r = build(tmp)
    r.mt5.symbols["EURUSD"].stops_level = 200  # 20 pips
    r.enable()
    out = r.engine.submit(r.signal(stop_pips=15))
    assert out["outcome"] == "rejected" and "stop_inside_stops_level" in out["reasons"][0], out
    assert not r.mt5.requests, "caught before order_send"


def partial_fill(tmp: Path):
    r = build(tmp)
    r.enable()
    r.mt5.inject("partial", fraction=0.4)
    out = r.engine.submit(r.signal())
    assert out["outcome"] == "partial", out
    [pos] = r.adapter.positions()
    assert r.engine.book.get(pos.ticket).volume == pos.volume == 0.1
    r.step(61)
    assert r.engine.reconciliation()["status"] == "clean"


def broker_drops_sl(tmp: Path):
    r = build(tmp)
    r.enable()
    r.mt5.inject("drop_sl")
    out = r.engine.submit(r.signal())
    assert out["outcome"] == "filled"
    [pos] = r.adapter.positions()
    assert pos.sl > 0, "SL must be re-attached at once"


def sl_cannot_be_attached(tmp: Path):
    r = build(tmp)
    r.enable()
    r.mt5.inject("drop_sl")
    r.mt5.inject("retcode", code=M.TRADE_RETCODE_INVALID_STOPS)
    r.mt5.inject("retcode", code=M.TRADE_RETCODE_INVALID_STOPS)
    out = r.engine.submit(r.signal())
    assert out["outcome"] == "unprotected_closed", out
    assert not r.adapter.positions(), "never hold a position without a broker-side stop"
    assert r.events("order_not_filled")


def risk_engine_denies(tmp: Path):
    r = build(tmp)
    _open(r)
    second = r.engine.submit(r.signal(setup="session_breakout"))  # same pair, same way: one bet
    assert second["outcome"] == "risk_denied" and second["reasons"], second
    assert len(_requests(r)) == 1


# ---------------------------------------------------------------------------- stops and closes


def gap_through_stop(tmp: Path):
    r = build(tmp)
    _open(r)
    [pos] = r.adapter.positions()
    r.mt5.set_price("EURUSD", pos.sl - 0.0010)  # 10 pips through the stop
    r.step()
    assert not r.adapter.positions() and not r.engine.book.positions
    [trade] = r.journal.rows("trades")
    assert trade["exit_reason"] == "sl" and trade["pnl"] < 0 and trade["r"] < -1.0, trade
    assert r.engine.risk.state.loss_streak == 1


def sl_removed_at_broker(tmp: Path):
    r = build(tmp)
    _open(r)
    [pos] = r.adapter.positions()
    r.mt5.remove_sl(pos.ticket)
    r.step(61)  # next reconciliation
    assert r.adapter.position(pos.ticket).sl == pos.sl
    rep = r.engine.reconciliation()
    assert rep["status"] == "clean" and rep["actions"][0]["kind"] == "stop_missing"


def friday_flatten(tmp: Path):
    fri = dt.datetime(2026, 9, 25, 19, 58, tzinfo=UTC)
    r = build(tmp, Clock(fri))
    _open(r)
    r.step(180)
    assert not r.adapter.positions() and r.events("flattened")[-1]["reason"] == "friday_flatten"


# ---------------------------------------------------------------------------- reconciliation


def orphan_with_known_intent_adopted(tmp: Path):
    r = build(tmp)
    r.enable()
    sig = r.signal()
    cid = client_order_id(sig.decision_id)
    # The engine crashed after recording the intent and before recording the fill.
    r.engine.intents[cid] = {"decision_id": sig.decision_id, "setup": sig.setup, "stop": sig.stop,
                             "target": 1.17302, "risk_amount": 39.25, "status": "sending"}
    r.engine._persist()
    ticket = r.mt5.open_external("EURUSD", 1, 0.25, sl=sig.stop, tp=1.17302, magic=26_090_000, comment=cid)
    r2 = r.restart()
    assert r2.engine.book.get(ticket) is not None
    assert r2.engine.reconciliation()["status"] == "clean" and r2.adapter.position(ticket) is not None


def orphan_unknown_closed(tmp: Path):
    r = build(tmp)
    ticket = r.mt5.open_external("EURUSD", 1, 0.10, magic=26_090_000, comment="ATL-UNKNOWN000000000")
    r.step(61)
    assert r.adapter.position(ticket) is None, "orphan policy closes unknown ATLAS positions"
    rep = r.engine.reconciliation()
    assert rep["status"] == "clean" and rep["actions"][0]["kind"] == "orphan_position"


def foreign_position_halts(tmp: Path):
    r = build(tmp)
    ticket = r.mt5.open_external("GBPUSD", -1, 0.05)
    r.step(61)
    assert r.state() == "HALT" and "reconciliation_mismatch" in r.engine.health()["reasons"]
    assert r.adapter.position(ticket) is not None, "never touch a position ATLAS didn't open"
    r.mt5.close_external(ticket, reason=M.DEAL_REASON_CLIENT)
    r.step(61)
    assert r.state() == "NORMAL"


def closed_while_engine_down(tmp: Path):
    r = build(tmp)
    _open(r)
    [pos] = r.adapter.positions()
    r.journal.close()
    r.clock.advance(minutes=30)
    r.mt5.close_external(pos.ticket, reason=M.DEAL_REASON_TP, price=pos.tp)
    r2 = r.restart()
    [trade] = r2.journal.rows("trades")
    assert trade["exit_reason"] == "tp" and trade["pnl"] > 0 and not r2.engine.book.positions
    assert r2.engine.reconciliation()["status"] == "clean"


def restart_keeps_state(tmp: Path):
    r = build(tmp)
    r.enable()
    sig = r.signal()
    r.engine.submit(sig)
    before = r.engine.risk.state.to_dict()
    r2 = r.restart()
    assert r2.engine.trading["enabled"] and r2.engine.risk.state.to_dict() == before
    assert r2.engine.submit(sig)["outcome"] == "skipped", "a restarted engine must not re-take a decision"
    assert len(r2.adapter.positions()) == 1


# ---------------------------------------------------------------------------- health states


def mt5_disconnect(tmp: Path):
    r = build(tmp)
    r.enable()
    r.mt5.broker_connected = False
    r.step(30)
    assert r.state() == "NORMAL", "under the 60 s budget"
    r.step(31)
    assert r.state() == "HALT" and "mt5_disconnected" in r.engine.health()["reasons"]
    assert r.engine.submit(r.signal())["outcome"] == "skipped"
    r.mt5.broker_connected = True
    r.step(61)
    assert r.state() == "NORMAL" and r.engine.trading["enabled"], "HALT blocks entries but does not disable"


def terminal_dies_and_restarts(tmp: Path):
    r = build(tmp)
    r.mt5.terminal_alive = False
    r.step(61)
    h = r.engine.health()
    assert h["state"] == "HALT" and "mt5_disconnected" in h["reasons"] and "heartbeat_silent:adapter" in h["reasons"]
    r.mt5.terminal_alive = True
    r.step(1)
    r.step(61)
    assert r.state() == "NORMAL"


def clock_drift(tmp: Path):
    r = build(tmp)
    r.mt5.tick_skew_s = 5.0
    r.step()
    assert r.state() == "HALT" and "clock_drift" in r.engine.health()["reasons"]


def wrong_server_timezone(tmp: Path):
    r = build(tmp, adapter_clock=ServerClock(offset_hours=8))
    r.step()
    assert r.state() == "HALT" and "clock_drift" in r.engine.health()["reasons"]
    assert abs(r.engine.health()["telemetry"]["clock_drift_s"]) >= 3599


def tick_gap(tmp: Path):
    r = build(tmp)
    r.enable()
    r.mt5.symbols["EURUSD"].frozen = True
    r.step(46)
    h = r.engine.health()
    assert h["state"] == "DEGRADED" and h["symbols"]["EURUSD"]["reasons"] == ["tick_gap"]
    assert h["symbols"]["GBPUSD"]["state"] == "NORMAL"
    assert r.engine.submit(r.signal("EURUSD"))["outcome"] == "skipped"
    assert r.engine.submit(r.signal("GBPUSD"))["outcome"] == "filled"


def spread_spike(tmp: Path):
    r = build(tmp)
    r.enable()
    for _ in range(5):
        r.step()
    r.mt5.set_price("EURUSD", 1.17000, 1.17010)  # 1.0 pip vs a 0.2 pip median
    r.step()
    assert r.engine.health()["symbols"]["EURUSD"]["reasons"] == ["spread_wide"]
    assert r.engine.submit(r.signal("EURUSD"))["outcome"] == "skipped"


def db_write_failure(tmp: Path):
    r = build(tmp)
    r.enable()
    r.journal.fail_writes = True
    r.step()
    assert r.state() == "HALT" and "db_write_failure" in r.engine.health()["reasons"]
    assert r.engine.submit(r.signal())["outcome"] == "skipped"
    r.journal.fail_writes = False
    r.step()
    r.step()
    assert r.state() == "NORMAL"


def watchdog_silent(tmp: Path):
    r = build(tmp)
    r.clock.advance(seconds=61)
    r.mt5.touch()
    r.engine.step()  # no beat
    assert r.state() == "HALT" and "heartbeat_silent:watchdog" in r.engine.health()["reasons"]


def watchdog_flattened(tmp: Path):
    r = build(tmp)
    _open(r)
    r.clock.advance(seconds=1)
    r.beat("FLATTENED")
    r.engine.step()
    assert r.state() == "KILL" and not r.engine.trading["enabled"]
    assert not r.adapter.positions()


def config_changed_at_runtime(tmp: Path):
    cfg = tmp / "config"
    shutil.copytree(REPO_CONFIG, cfg)
    r = build(tmp / "run", config_root=cfg)
    risk = cfg / "risk.yaml"
    risk.write_text(risk.read_text().replace("max_trades_per_day: 6", "max_trades_per_day: 60"))
    r.step()
    assert r.state() == "HALT" and "config_changed" in r.engine.health()["reasons"]


def algo_trading_off(tmp: Path):
    r = build(tmp)
    r.mt5.algo_trading = False
    r.step()
    assert r.state() == "HALT" and "broker_trade_disabled" in r.engine.health()["reasons"]


def request_budget(tmp: Path):
    r = build(tmp)
    r.engine.requests["count"] = 1_800  # FTMO: 2,000 per day; stop entries at 90%
    r.step()
    assert r.state() == "HALT" and "request_budget" in r.engine.health()["reasons"]


# ---------------------------------------------------------------------------- loss limits


def daily_hard_loss_kill(tmp: Path):
    r = build(tmp)
    _open(r)
    r.mt5.balance -= 400  # 80% of the firm's $500 daily loss, past the 75% hard stop
    r.step()
    assert r.state() == "KILL" and "daily_loss_hard_limit" in r.engine.health()["reasons"]
    assert not r.adapter.positions() and not r.engine.trading["enabled"]
    r.operator("enable_trading")
    assert not r.engine.trading["enabled"], "enable must be refused while KILL holds"
    assert r.events("operator_command")[-1]["outcome"] == "rejected"


def drawdown_kill(tmp: Path):
    r = build(tmp)
    r.enable()
    r.mt5.balance -= 300
    r.step()
    assert r.state() == "NORMAL" and r.engine.assessment.status == "day_stopped"
    r.clock.advance(days=1)
    r.mt5.balance -= 300  # 6% below the initial balance: the internal drawdown stop
    r.step()
    assert r.state() == "KILL" and "drawdown_limit" in r.engine.health()["reasons"]
    assert r.engine.risk.state.drawdown_stopped and not r.engine.trading["enabled"]


# ---------------------------------------------------------------------------- operator and API


def operator_commands_are_authenticated(tmp: Path):
    r = build(tmp)
    r.operator("enable_trading", key=bytes(32))
    assert not r.engine.trading["enabled"] and "bad signature" in r.events("operator_command")[-1]["detail"]
    r.operator("enable_trading", at=r.clock() - dt.timedelta(minutes=5))
    assert not r.engine.trading["enabled"] and "older" in r.events("operator_command")[-1]["detail"]
    cmd = r.operator("enable_trading")
    assert r.engine.trading["enabled"]
    r.operator("kill", reason="drill: manual kill from the operator")
    (r.engine.inbox / "replay.json").write_text(json.dumps(cmd))
    r.step()
    assert "replay" in r.events("operator_command")[-1]["detail"] and not r.engine.trading["enabled"]


def agents_can_only_disable(tmp: Path):
    r = build(tmp)
    r.enable()
    names = {"read": ["ops:read"], "ops": ["ops:read", "ops:disable_trading"]}
    tokens = TokenStore([{"name": n, "sha256": hash_token(n), "scopes": s} for n, s in names.items()], ENGINE_SCOPES)
    svc = OpsService(r.engine)
    code, body = dispatch(svc, tokens, "operations/status", "read", {}, OPS_ROUTES)
    assert code == 200 and body["source"] == "engine:fake-mt5" and body["trading"]["enabled"]
    assert dispatch(svc, tokens, "operations/disable_trading", "read", {"reason": "agent tries it"}, OPS_ROUTES)[0] == 403
    code, body = dispatch(svc, tokens, "operations/disable_trading", "ops",
                          {"reason": "incident triage: disabling while HALT is investigated"}, OPS_ROUTES)
    assert code == 200 and not r.engine.trading["enabled"]
    assert not any(w in route for route in OPS_ROUTES for w in ("enable", "flatten", "kill", "risk", "order"))
    code, _ = dispatch(svc, tokens, "operations/enable_trading", "ops", {}, OPS_ROUTES)
    assert code == 404
    rep = dispatch(svc, tokens, "operations/reconciliation", "read", {}, OPS_ROUTES)[1]
    assert rep["status"] == "clean" and "interval_s" in rep and "reconciled_at" in rep


SCENARIOS = [
    ("clean entry: SL/TP broker-side, magic, client ID", "§21", clean_entry),
    ("duplicate decision sends nothing", "§21", duplicate_decision),
    ("timeout after fill: look, don't resend", "§21", timeout_after_fill),
    ("timeout without fill: one retry", "§21", timeout_without_fill),
    ("terminal dies mid-order: fill adopted later", "§21", terminal_dies_mid_order),
    ("two timeouts: give up, no position", "§21", two_timeouts_give_up),
    ("requote then fill", "§21", requote_then_fill),
    ("requote and price ran past deviation", "§21", requote_price_ran_away),
    ("hard broker rejects: no retry", "§21", broker_rejects),
    ("stop inside stops level: refused before send", "§21", stop_inside_stops_level),
    ("partial fill: book and risk use the filled volume", "§21", partial_fill),
    ("broker drops the SL: re-attached", "§12 L3", broker_drops_sl),
    ("SL can't be attached: position closed", "§12 L3", sl_cannot_be_attached),
    ("risk engine denial sends nothing", "§19", risk_engine_denies),
    ("gap through the stop: loss beyond 1R journaled", "§22", gap_through_stop),
    ("SL removed at the broker: restored", "§21", sl_removed_at_broker),
    ("Friday 20:00 UTC flatten", "§18", friday_flatten),
    ("orphan from a crash mid-order: adopted", "§21", orphan_with_known_intent_adopted),
    ("unknown ATLAS orphan: closed", "§21", orphan_unknown_closed),
    ("foreign position: HALT, untouched", "§21", foreign_position_halts),
    ("closed while the engine was down: from deal history", "§21", closed_while_engine_down),
    ("restart keeps risk state and idempotency", "§21", restart_keeps_state),
    ("MT5 disconnected > 60 s: HALT, then recovers", "§23", mt5_disconnect),
    ("terminal dies and restarts", "§23", terminal_dies_and_restarts),
    ("clock drift > 2 s: HALT", "§23", clock_drift),
    ("wrong server timezone: HALT", "§21", wrong_server_timezone),
    ("tick gap > 30 s: that symbol DEGRADED", "§23", tick_gap),
    ("spread > 2x median: that symbol DEGRADED", "§23", spread_spike),
    ("journal write failure: HALT, then recovers", "§23", db_write_failure),
    ("watchdog heartbeat silent: HALT", "§23", watchdog_silent),
    ("watchdog flattened: KILL", "§23", watchdog_flattened),
    ("config changed at runtime: HALT", "§12 L2", config_changed_at_runtime),
    ("algo trading switched off: HALT", "§21", algo_trading_off),
    ("firm request cap near: HALT", "§19", request_budget),
    ("daily hard loss: KILL, flatten, enable refused", "§19, §23", daily_hard_loss_kill),
    ("drawdown stop: KILL", "§19, §23", drawdown_kill),
    ("operator commands: signature, age, replay", "§11 L4", operator_commands_are_authenticated),
    ("agents: read and disable only", "§11", agents_can_only_disable),
]
