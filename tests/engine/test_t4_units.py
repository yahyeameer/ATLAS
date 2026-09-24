"""Unit tests for the T4 pieces: server clock, MT5 adapter, executor rules, operator commands,
journal, settings, alerts, the watchdog EA's file contract and the setup signal source."""

from __future__ import annotations

import datetime as dt
import json
import os
import re
from pathlib import Path

import pandas as pd
import pytest
import yaml

from atlas_engine.adapters.broker import BrokerUnavailable
from atlas_engine.adapters.mt5 import MT5Adapter, ServerClock
from atlas_engine.adapters.mt5.fake import FakeMT5
from atlas_engine.alerts import AlertOutbox, SmtpSender
from atlas_engine.config import ConfigError
from atlas_engine.execution import EntryOrder, ExecutionSettings, Executor, client_order_id, load_execution_settings
from atlas_engine.journal import Journal, JournalError
from atlas_engine.market_data.bars import BAR_COLS
from atlas_engine.market_data.synthetic import random_walk_m1
from atlas_engine.operator import OperatorAuthError, keygen, load_key, sign, verify
from atlas_engine.ops import health as H
from atlas_engine.setups import SETUPS
from atlas_engine.strategies import SetupSource, load_strategies, m1_frame

from t4help import KEY, REPO_CONFIG, T0, UTC, Clock, build

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def fake():
    clock = Clock()
    m = FakeMT5(clock)
    a = MT5Adapter(m)
    a.connect()
    return m, a, clock


# ---------------------------------------------------------------------------- server clock


@pytest.mark.parametrize("utc, offset_h", [
    (dt.datetime(2026, 1, 15, 12, 0, tzinfo=UTC), 2),   # New York winter: server UTC+2
    (dt.datetime(2026, 7, 15, 12, 0, tzinfo=UTC), 3),   # New York summer: server UTC+3
    (dt.datetime(2026, 3, 20, 12, 0, tzinfo=UTC), 3),   # US on DST, Europe not yet: still NY + 7
])
def test_server_clock_is_new_york_plus_seven(utc, offset_h):
    c = ServerClock()
    assert c.server_wall(utc) == utc.replace(tzinfo=None) + dt.timedelta(hours=offset_h)
    assert c.to_utc(c.to_server(utc)) == utc


def test_server_day_starts_at_the_new_york_close():
    c = ServerClock()
    ny_close = dt.datetime(2026, 9, 22, 21, 0, tzinfo=UTC)  # 17:00 New York (EDT)
    assert c.server_wall(ny_close).time() == dt.time(0, 0)


def test_server_clock_refuses_naive_times():
    with pytest.raises(ValueError):
        ServerClock().to_server(dt.datetime(2026, 1, 1))


# ---------------------------------------------------------------------------- adapter


def test_adapter_converts_records(fake):
    m, a, clock = fake
    ticket = m.open_external("EURUSD", -1, 0.3, sl=1.1720, tp=1.1650, magic=7, comment="x")
    [p] = a.positions()
    assert (p.ticket, p.direction, p.volume, p.sl, p.magic) == (ticket, -1, 0.3, 1.1720, 7)
    assert p.time == T0
    rules = a.symbol_rules("EURUSD")
    assert rules.spec.tick_value_loss == pytest.approx(1.0) and rules.spec.commission_per_lot == 7.0
    assert rules.trade_mode == "full" and a.filling(rules) == m.ORDER_FILLING_IOC


def test_adapter_filling_falls_back(fake):
    m, a, _ = fake
    m.symbols["EURUSD"].filling_mode = 1
    assert a.filling(a.symbol_rules("EURUSD")) == m.ORDER_FILLING_FOK
    m.symbols["EURUSD"].filling_mode = 0
    assert a.filling(a.symbol_rules("EURUSD")) == m.ORDER_FILLING_RETURN


def test_adapter_deals_skip_balance_rows_and_use_utc(fake):
    m, a, clock = fake
    t = m.open_external("EURUSD", 1, 0.1)
    clock.advance(hours=2)
    m.close_external(t, reason=m.DEAL_REASON_TP)
    deals = a.deals(T0 - dt.timedelta(hours=1), clock())
    assert [d.entry for d in deals] == ["in", "out"] and deals[1].reason == "tp"
    assert deals[1].time == T0 + dt.timedelta(hours=2)
    assert a.deals(T0 + dt.timedelta(hours=3), T0 + dt.timedelta(hours=4)) == []


def test_adapter_raises_when_the_terminal_is_gone(fake):
    m, a, _ = fake
    m.terminal_alive = False
    assert not a.connected()
    with pytest.raises(BrokerUnavailable):
        a.positions()
    assert a.send({"action": 1}).unknown


def test_adapter_maps_send_statuses(fake):
    m, a, _ = fake
    rules = a.symbol_rules("EURUSD")
    req = a.market_request(rules, 1, 0.1, 1.17002, deviation=2, magic=1, comment="c", sl=1.1685, tp=1.1730)
    for code, status in ((m.TRADE_RETCODE_REQUOTE, "requote"), (m.TRADE_RETCODE_TIMEOUT, "unknown"),
                         (m.TRADE_RETCODE_NO_MONEY, "rejected"), (m.TRADE_RETCODE_POSITION_CLOSED, "gone")):
        m.inject("retcode", code=code)
        assert a.send(req).status == status
    assert a.send(req).status == "done"
    assert a.check(req)[0]


# ---------------------------------------------------------------------------- executor


def _order(**kw):
    base = dict(decision_id="d1", symbol="EURUSD", setup="s", direction=1, volume=0.1, stop=1.1685, target=1.1730,
                magic=1, expected_price=1.17002)
    return EntryOrder(**{**base, **kw})


def test_client_order_id_is_short_stable_and_distinct():
    a, b = client_order_id("d1"), client_order_id("d2")
    assert a == client_order_id("d1") and a != b and len(a) == 20 and a.startswith("ATL-")


@pytest.mark.parametrize("change, problem", [
    ({"stop": 1.1710}, "missing_or_wrong_side_stop"),
    ({"stop": 0.0}, "missing_or_wrong_side_stop"),
    ({"target": 1.1690}, "missing_or_wrong_side_target"),
    ({"volume": 0.015}, "invalid_volume"),
    ({"stop": 1.16999}, "spread_too_wide_for_stop"),
])
def test_executor_validation(fake, change, problem):
    m, a, _ = fake
    ex = Executor(a, ExecutionSettings())
    assert problem in ex.validate_entry(_order(**change), a.symbol_rules("EURUSD"), a.tick("EURUSD"))


def test_executor_respects_symbol_trade_mode(fake):
    m, a, _ = fake
    m.symbols["EURUSD"].trade_mode = 1  # long only
    ex = Executor(a, ExecutionSettings())
    rules, tick = a.symbol_rules("EURUSD"), a.tick("EURUSD")
    assert not ex.validate_entry(_order(), rules, tick)
    assert "symbol_trade_mode_long_only" in ex.validate_entry(
        _order(direction=-1, stop=1.1715, target=1.1670, expected_price=1.17), rules, tick)


def test_stops_only_tighten_and_respect_the_freeze_level(fake):
    m, a, _ = fake
    ex = Executor(a, ExecutionSettings())
    res = ex.open(_order(), T0)
    pos = a.position(res.ticket)
    assert ex.modify_stop(pos, 1.1680).reason == "stops only tighten"
    assert ex.modify_stop(pos, 1.1690).status == "filled"
    pos = a.position(res.ticket)
    m.symbols["EURUSD"].freeze_level = 200
    assert "freeze" in ex.modify_stop(pos, 1.1692).reason


# ---------------------------------------------------------------------------- operator


def test_sign_and_verify():
    now = T0
    cmd = sign(KEY, "enable_trading", "yahye", "all checks reviewed", now)
    assert verify(KEY, cmd, now, set()) == cmd
    with pytest.raises(OperatorAuthError, match="replay"):
        verify(KEY, cmd, now, {cmd["nonce"]})
    with pytest.raises(OperatorAuthError, match="signature"):
        verify(KEY, {**cmd, "action": "flatten"}, now, set())
    with pytest.raises(OperatorAuthError, match="older"):
        verify(KEY, cmd, now + dt.timedelta(minutes=3), set())
    with pytest.raises(OperatorAuthError, match="malformed"):
        verify(KEY, {"action": "kill"}, now, set())
    with pytest.raises(ValueError):
        sign(KEY, "enable_trading", "yahye", "short", now)
    with pytest.raises(ValueError):
        sign(KEY, "raise_risk", "yahye", "not an operator action", now)


def test_keygen_writes_an_owner_only_key(tmp_path):
    p = tmp_path / "op.key"
    keygen(p)
    assert len(load_key(p)) == 32
    if os.name == "posix":
        assert p.stat().st_mode & 0o077 == 0
        p.chmod(0o644)
        with pytest.raises(OperatorAuthError, match="chmod 600"):
            load_key(p)
    with pytest.raises(FileExistsError):
        keygen(p)


def test_cli_signs_into_the_inbox_and_the_engine_applies_it(tmp_path, monkeypatch):
    from atlas_api import engine_cli
    rig = build(tmp_path)
    key = tmp_path / "op.key"
    key.write_text(KEY.hex())
    key.chmod(0o600)
    monkeypatch.setattr("atlas_engine.operator.dt", _FrozenDt(rig.clock()))
    engine_cli.main(["operator", "enable_trading", "--key", str(key), "--inbox", str(rig.engine.inbox),
                     "--operator", "yahye", "--reason", "enable after the review"])
    rig.step()
    assert rig.engine.trading["enabled"] and rig.engine.trading["by"] == "yahye"
    assert list((rig.engine.inbox / "applied").glob("*.json"))


class _FrozenDt:
    """``datetime`` module stand-in whose now() is the rig's clock."""

    def __init__(self, t):
        self.t = t
        self.timezone = dt.timezone
        self.timedelta = dt.timedelta

    @property
    def datetime(self):
        t = self.t

        class D(dt.datetime):
            @classmethod
            def now(cls, tz=None):
                return t
        return D


# ---------------------------------------------------------------------------- journal


def test_journal_records_and_state(tmp_path):
    j = Journal(tmp_path / "j.db", "run-1")
    j.write("orders", {"x": 1}, T0, decision_id="d1")
    j.write("orders", {"x": 2}, T0, decision_id="d2")
    assert [r["x"] for r in j.rows("orders")] == [1, 2]
    assert j.rows("orders", decision_id="d2")[0]["run_id"] == "run-1"
    j.save_state("engine", {"a": 1})
    j.save_state("engine", {"a": 2})
    assert Journal(tmp_path / "j.db", "run-2").load_state("engine") == {"a": 2}
    j.fail_writes = True
    with pytest.raises(JournalError):
        j.write("orders", {}, T0)
    with pytest.raises(ValueError):
        j.write("not_a_table", {}, T0)


# ---------------------------------------------------------------------------- settings


def test_repo_config_uses_prd_defaults():
    s = load_execution_settings(REPO_CONFIG)
    assert s.max_deviation_points == 2 and s.reconcile_interval_s == 60 and s.orphan_policy == "close"
    assert "max_deviation_points" in s.defaults_used and s.friday_flatten_utc == "20:00"


def test_settings_refuse_unknown_and_bad_values(tmp_path):
    (tmp_path / "atlas.yaml").write_text(yaml.safe_dump({"execution": {"max_deviaton_points": 2}}))
    with pytest.raises(ConfigError, match="unknown"):
        load_execution_settings(tmp_path)
    (tmp_path / "atlas.yaml").write_text(yaml.safe_dump({"execution": {"orphan_policy": "ignore"}}))
    with pytest.raises(ConfigError, match="orphan_policy"):
        load_execution_settings(tmp_path)
    (tmp_path / "atlas.yaml").write_text(yaml.safe_dump({"execution": {"max_deviation_points": 5},
                                                         "filters": {"rollover_blackout_ny": ["16:40", "18:20"]}}))
    s = load_execution_settings(tmp_path)
    assert s.max_deviation_points == 5 and s.rollover_blackout_ny == ("16:40", "18:20")


def test_no_strategies_configured_means_no_sources():
    assert load_strategies(REPO_CONFIG) == []


def test_strategy_files_are_strict(tmp_path):
    d = tmp_path / "strategies"
    d.mkdir()
    (d / "a.yaml").write_text(yaml.safe_dump({"setup": "trend_pullback", "enabled": True, "symbols": ["eurusd"],
                                              "magic_offset": 3, "exit": {"rr": 1.5}}))
    [s] = load_strategies(tmp_path)
    assert s.symbols == ("EURUSD",) and s.rr == 1.5 and s.magic_offset == 3
    (d / "b.yaml").write_text(yaml.safe_dump({"setup": "trend_pullback", "enabled": True, "magic_offset": 3}))
    with pytest.raises(ConfigError, match="magic_offset"):
        load_strategies(tmp_path)
    (d / "b.yaml").write_text(yaml.safe_dump({"setup": "grid_martingale", "enabled": True}))
    with pytest.raises(ConfigError, match="setup"):
        load_strategies(tmp_path)


# ---------------------------------------------------------------------------- alerts


def test_outbox_keeps_alerts_when_delivery_fails(tmp_path):
    class Broken:
        def send(self, alerts):
            raise OSError("smtp down")

    box = AlertOutbox(tmp_path / "a.jsonl", senders=[Broken()])
    box.alert("critical", "engine HALT")
    lines = [json.loads(x) for x in (tmp_path / "a.jsonl").read_text().splitlines()]
    assert lines[0]["text"] == "engine HALT" and "delivery failed" in lines[1]["text"]
    assert len(box.pending) == 1


def test_smtp_sender_builds_one_message(monkeypatch):
    sent = []

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            self.host = host

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self):
            pass

        def login(self, u, p):
            pass

        def send_message(self, msg):
            sent.append(msg)

    for k, v in {"ATLAS_ALERT_SMTP_HOST": "smtp.example", "ATLAS_ALERT_TO": "ops@example"}.items():
        monkeypatch.setenv(k, v)
    s = SmtpSender.from_env()
    s.smtp = FakeSMTP
    s.send([{"at": "t", "severity": "warning", "text": "a"}, {"at": "t", "severity": "critical", "text": "b"}])
    assert "critical" in sent[0]["Subject"] and sent[0]["To"] == "ops@example"


def test_no_smtp_without_config(monkeypatch):
    monkeypatch.delenv("ATLAS_ALERT_SMTP_HOST", raising=False)
    assert SmtpSender.from_env() is None


# ---------------------------------------------------------------------------- health extension


def test_engine_halt_reasons_extend_the_h3_state_machine():
    h = H.evaluate({"halt_reasons": ["config_changed"]})
    assert h["state"] == "HALT" and h["reasons"] == ["config_changed"]
    assert H.evaluate({})["state"] == "NORMAL"


# ---------------------------------------------------------------------------- watchdog EA contract


EA = (ROOT / "watchdog" / "AtlasWatchdog.mq5").read_text()


def test_ea_heartbeat_format_matches_the_engine_reader(tmp_path):
    from atlas_api.engine_cli import _watchdog_reader
    assert '"ATLAS-WD 1 "' in EA and '"FLATTENED"' in EA
    assert '"ATLAS-LIMITS"' in EA and "parts[7]" in EA
    p = tmp_path / "atlas_watchdog.txt"
    p.write_bytes("ATLAS-WD 1 1790000000 9990.00 OK\r\n".encode("utf-16"))
    assert _watchdog_reader(str(p))() == "ATLAS-WD 1 1790000000 9990.00 OK"


def test_ea_defaults_match_config():
    num = lambda name: float(re.search(rf"input \w+\s+{name}\s*=\s*([\d.]+)", EA).group(1))  # noqa: E731
    assert int(num("InpMagicBase")) == ExecutionSettings().magic_base
    atlas = yaml.safe_load((REPO_CONFIG / "atlas.yaml").read_text())
    prop = yaml.safe_load((REPO_CONFIG / atlas["prop_rules"]).read_text())
    assert num("InpInitialBalance") == atlas["account"]["initial_balance"]
    assert num("InpDailyLossPct") == prop["daily_loss"]["pct"] and num("InpMaxLossPct") == prop["max_loss"]["pct"]
    risk = yaml.safe_load((REPO_CONFIG / "risk.yaml").read_text())
    # The EA's lines sit between the engine's KILL lines and the firm's floors.
    assert risk["daily_loss_hard_pct_of_firm"] < num("InpDailyHardShare") < 1
    assert risk["drawdown_stop_pct_of_firm"] < num("InpMaxLossHardShare") < 1


def test_engine_writes_limits_for_the_ea(tmp_path):
    hb = tmp_path / "common" / "atlas_watchdog.txt"
    hb.parent.mkdir()
    rig = build(tmp_path / "run", settings=ExecutionSettings(watchdog_heartbeat_file=str(hb)))
    parts = (hb.parent / "atlas_limits.txt").read_text().split()
    assert parts[:3] == ["ATLAS-LIMITS", "1", "2026.09.22"]
    assert [float(x) for x in parts[3:7]] == [9500.0, 500.0, 9000.0, 1000.0]
    assert rig.engine.health()["state"] == "NORMAL"


# ---------------------------------------------------------------------------- signal source


def test_setup_source_decides_like_the_backtest_on_broker_bars():
    """Live signals come from the same feature and setup code as research, on closed bars only."""
    m1 = random_walk_m1("EURUSD", "2026-07-20", "2026-09-22", seed=3, momentum=0.2)
    clock = Clock(dt.datetime(2026, 9, 22, tzinfo=UTC))
    m = FakeMT5(clock)
    m.load_rates("EURUSD", m1)
    a = MT5Adapter(m)
    a.connect()
    setup = SETUPS["trend_pullback"]
    # Reference: the research path over the same bars the broker serves (one spread per bar).
    rates = m.rates["EURUSD"]
    ref = m1_frame(rates, a.clock)
    for c in "ohlc":
        ref[f"ask_{c}"] = ref[f"bid_{c}"].to_numpy() + rates["spread"] * 1e-5
    from atlas_engine.features.frame import build_features
    sig = setup.signals(build_features(ref[BAR_COLS]))
    recent = sig[pd.DatetimeIndex(sig["decision_time"]) > pd.Timestamp("2026-09-10", tz="UTC")]
    assert len(recent), "the planted momentum should give some recent signals"
    t = pd.Timestamp(recent["decision_time"].iloc[-1]).to_pydatetime()
    src = SetupSource(setup, "1", ("EURUSD",), history_bars=len(rates))
    clock.t = t - dt.timedelta(seconds=30)  # the bar that closes at t is still forming
    assert all(s.decision_time != t for s in src.poll(a, clock()))
    clock.t = t + dt.timedelta(seconds=20)
    got = src.poll(a, clock())
    want = recent[pd.DatetimeIndex(recent["decision_time"]) == t]
    assert [(s.direction, round(s.stop, 6)) for s in got] == \
        [(int(d), round(float(st), 6)) for d, st in zip(want["direction"], want["stop"])]
    assert src.poll(a, clock()) == [], "a bar is decided once"


# ---------------------------------------------------------------------------- H3 compatibility


def test_h3_cron_jobs_read_the_real_engine(tmp_path, monkeypatch):
    """The H3 health check and reconciliation report work unchanged against the T4 engine."""
    from atlas_plugins import jobs
    monkeypatch.setenv("ATLAS_OPS_DIR", str(tmp_path / "ops"))
    for var in ("HERMES_PROFILE", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK"):
        monkeypatch.delenv(var, raising=False)
    rig = build(tmp_path / "run")
    e = rig.engine
    client = lambda route, **a: {"operations/status": e.status, "operations/health": e.health,  # noqa: E731
                                 "operations/reconciliation": e.reconciliation}[route]()
    cards = []
    create = lambda *a, **k: cards.append((a, k)) or "t_1"  # noqa: E731
    assert jobs.health_check(client, rig.clock(), create) == ""
    assert jobs.reconciliation_report(client, rig.clock()) == ""
    rig.mt5.open_external("GBPUSD", 1, 0.05)
    rig.step(61)
    out = jobs.health_check(client, rig.clock(), create)
    assert "HALT" in out and "reconciliation_mismatch" in out and len(cards) == 1
    assert "foreign_position" in jobs.reconciliation_report(client, rig.clock())
