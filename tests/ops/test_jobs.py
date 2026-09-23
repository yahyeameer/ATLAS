"""The cron scripts: health check, alert relay, token budgets, reconciliation report, scheduled cards."""

import datetime as dt
import json
import threading

import pytest

from atlas_api.auth import TokenStore, hash_token
from atlas_api.http import make_server
from atlas_api.ops import ENGINE_SCOPES, OPS_ROUTES, OpsService
from atlas_plugins import jobs, store

from opshelp import T0, clock, engine, ops_dir  # noqa: F401


class Engine:
    """The cron client, straight onto the simulated engine (HTTP is covered in test_http_client)."""

    def __init__(self, engine):
        self.engine, self.down = engine, False

    def __call__(self, route, **args):
        if self.down:
            raise jobs.EngineError(f"{route}: engine API unreachable at http://engine: connection refused")
        return {"operations/status": self.engine.status, "operations/health": self.engine.health,
                "operations/reconciliation": self.engine.reconciliation}[route]()


class Cards:
    def __init__(self):
        self.calls, self.by_key = [], {}

    def __call__(self, board, title, assignee, body, key, tenant=None, skills=(), created_by=""):
        self.calls.append(dict(board=board, title=title, assignee=assignee, body=body, key=key, tenant=tenant,
                               skills=skills, created_by=created_by))
        return self.by_key.setdefault(key, f"t_{len(self.by_key) + 1}")


@pytest.fixture()
def client(engine):
    return Engine(engine)


@pytest.fixture()
def cards():
    return Cards()


def check(client, clock, cards):
    return jobs.health_check(client, clock(), cards)


def test_healthy_engine_is_silent(ops_dir, client, clock, cards):
    assert check(client, clock, cards) == "" and cards.calls == []
    clock.advance(minutes=15)
    assert check(client, clock, cards) == ""
    assert store.read(store.ALERTS) == []


def test_halt_alerts_once_and_opens_one_incident_card(ops_dir, engine, client, clock, cards):
    engine.inject("mt5_disconnect")
    out = check(client, clock, cards)
    assert out.startswith("HALT: mt5_disconnected.") and "New trades: enabled." in out
    assert "Incident card t_1 opened on atlas-ops" in out
    [card] = cards.calls
    assert card["board"] == "atlas-ops" and card["assignee"] == "operations-monitor" and card["tenant"] == "paper"
    assert card["skills"] == ("incident-triage",) and card["key"].startswith("incident-2026-09-23-")
    assert "disable_trading" in card["body"] and "Never try to re-enable" in card["body"]
    # Same incident 15 minutes later: silent, no second card.
    clock.advance(minutes=15)
    assert check(client, clock, cards) == "" and len(cards.calls) == 1
    # The health-check output is delivered by cron itself, so the relay must not repeat it.
    [a] = store.read(store.ALERTS)
    assert a["severity"] == "critical" and a["delivered_by"] == "health-check"
    assert jobs.alert_relay(clock()) == ""


def test_reminder_while_incident_stays_open(ops_dir, engine, client, clock, cards):
    engine.inject("recon_mismatch")
    check(client, clock, cards)
    clock.advance(minutes=119)
    assert check(client, clock, cards) == ""
    clock.advance(minutes=1)
    out = check(client, clock, cards)
    assert out.startswith("STILL HALT since 2026-09-23T14:00:00+00:00") and len(cards.calls) == 1


def test_escalation_and_recovery(ops_dir, engine, client, clock, cards):
    engine.inject("mt5_disconnect")
    check(client, clock, cards)
    engine.inject("daily_loss")
    clock.advance(minutes=15)
    out = check(client, clock, cards)
    assert out.startswith("KILL: daily_loss_hard_limit, mt5_disconnected.")
    assert "New trades: DISABLED by engine" in out and len(cards.calls) == 2
    engine.clear()
    clock.advance(minutes=15)
    out = check(client, clock, cards)
    assert out.startswith("RECOVERED: engine health back to NORMAL (was KILL).")
    assert "DISABLED" in out  # recovery does not re-enable: the operator does
    assert store.read(store.ALERTS)[-1]["severity"] == "info"


def test_degraded_alerts_without_a_card(ops_dir, engine, client, clock, cards):
    engine.inject("spread_spike", symbol="GBPUSD")
    out = check(client, clock, cards)
    assert out.startswith("DEGRADED: spread_wide. (GBPUSD: spread_wide)") and cards.calls == []


def test_unreachable_engine_is_an_incident(ops_dir, client, clock, cards):
    client.down = True
    out = check(client, clock, cards)
    assert out.startswith("UNREACHABLE: engine_api_unreachable.") and "connection refused" in out
    assert cards.calls[0]["tenant"] is None  # mode unknown while unreachable
    client.down = False
    clock.advance(minutes=15)
    assert check(client, clock, cards).startswith("RECOVERED")


def test_incident_tenant_follows_engine_mode(ops_dir, engine, client, clock, cards):
    engine.init(mode="evaluation")
    engine.inject("clock_drift")
    check(client, clock, cards)
    assert cards.calls[0]["tenant"] == "eval-ftmo"


# --------------------------------------------------------------------------- relay and budgets


def test_relay_delivers_each_alert_once(ops_dir, clock):
    store.alert("critical", "hooks", "operations-monitor DISABLED new trading: drill")
    store.alert("info", "kanban", "atlas-ops card t1 done")
    out = jobs.alert_relay(clock())
    assert out.splitlines()[0] == "ATLAS: 2 alerts"
    assert "[CRITICAL]" in out and "DISABLED new trading" in out and "[INFO]" in out
    assert jobs.alert_relay(clock()) == ""
    store.alert("warning", "hooks", "strategy-researcher was refused atlas-backtest.run_backtest (holdout_refused)")
    assert jobs.alert_relay(clock()).splitlines()[0] == "ATLAS: 1 alert"


def test_relay_caps_message_length(ops_dir, clock):
    for i in range(100):
        store.alert("warning", "hooks", f"alert {i} " + "x" * 80)
    out = jobs.alert_relay(clock())
    assert len(out) <= jobs.MAX_RELAY_CHARS + 100 and "more on the ATLAS dashboard" in out


def test_relay_waits_for_a_torn_last_line(ops_dir, clock):
    store.alert("info", "kanban", "whole line")
    with open(store.path(*store.ALERTS), "a") as fh:
        fh.write('{"at": "2026-09-23T14:00:00+00:00", "severity": "info", "te')  # a writer mid-append
    assert jobs.alert_relay(clock()).count("\n") == 1
    with open(store.path(*store.ALERTS), "a") as fh:
        fh.write('xt": "second half"}\n')
    # Once the writer finishes the line, the next run delivers it: nothing is lost or repeated.
    assert jobs.alert_relay(clock()).endswith("second half")
    assert jobs.alert_relay(clock()) == ""


def usage(board, tokens, at="2026-09-23T10:00:00+00:00"):
    store.append(store.USAGE, {"at": at, "board": board, "input_tokens": tokens, "output_tokens": 0})


def test_budget_alerts_at_80_and_100_percent_once_per_month(ops_dir, clock):
    store.path("ops.yaml").parent.mkdir(parents=True, exist_ok=True)
    store.path("ops.yaml").write_text("budgets: {atlas-research: 1000, other: 1000}\n")
    usage("atlas-research", 700)
    usage(None, 100)
    assert jobs.budget_check(clock()) == []
    usage("atlas-research", 150)
    [a] = jobs.budget_check(clock())
    assert a["severity"] == "warning" and "atlas-research has used 85%" in a["text"]
    assert jobs.budget_check(clock()) == []
    usage("atlas-research", 200)
    [a] = jobs.budget_check(clock())
    assert a["severity"] == "critical" and "105%" in a["text"]
    # New month: totals and alerts reset; old-month lines never count again.
    usage("atlas-research", 100, at="2026-10-01T00:05:00+00:00")
    october = dt.datetime(2026, 10, 1, 1, tzinfo=dt.timezone.utc)
    assert jobs.budget_check(october) == []
    assert jobs.usage_totals(october)["used"] == {"atlas-research": 100}
    assert "2 alerts" in jobs.alert_relay(october)
    # Jumping past both thresholds at once is one critical alert, not a warning and a critical.
    usage("atlas-research", 2000, at="2026-10-02T00:05:00+00:00")
    [a] = jobs.budget_check(october)
    assert a["severity"] == "critical" and "210%" in a["text"]
    assert jobs.budget_check(october) == []


# --------------------------------------------------------------------------- reconciliation report


def test_reconciliation_report(ops_dir, engine, client, clock):
    assert jobs.reconciliation_report(client, clock()) == ""
    assert store.path("reports", "reconciliation", "2026-09-23", "14.json").exists()
    engine.inject("recon_mismatch", symbol="GBPUSD", ticket=777)
    out = jobs.reconciliation_report(client, clock())
    assert out.startswith("Reconciliation MISMATCH: engine 1 positions, broker 2.")
    assert "orphan_position GBPUSD ticket 777" in out
    client.down = True
    assert "unavailable" in jobs.reconciliation_report(client, clock())


# --------------------------------------------------------------------------- scheduled cards


@pytest.mark.parametrize("period, when, key", [
    ("day", T0, "daily-x-2026-09-23"), ("week", T0, "weekly-x-2026-W39"), ("month", T0, "monthly-x-2026-09"),
    ("week", dt.datetime(2027, 1, 1, tzinfo=dt.timezone.utc), "weekly-x-2026-W53"),
])
def test_card_job_keys(period, when, key, cards):
    prefix = {"day": "daily-x", "week": "weekly-x", "month": "monthly-x"}[period]
    spec = {"name": "atlas-x", "board": "atlas-research", "assignee": "jev-analyst", "period": period, "key": prefix,
            "title": "Review", "body": "Do it.\n", "skills": ["backtest-analysis"]}
    assert jobs.card_job(spec, when, cards) == ""
    assert cards.calls[0]["key"] == key and cards.calls[0]["title"].startswith("Review (")
    assert cards.calls[0]["created_by"] == "atlas-cron/atlas-x" and cards.calls[0]["skills"] == ("backtest-analysis",)


def test_calibration_key_matches_the_prd_example(cards):
    """PRD §4: ATLAS cron uses idempotency keys like weekly-calibration-2026-W39."""
    import yaml
    from pathlib import Path
    spec = yaml.safe_load((Path(jobs.__file__).parents[1] / "deploy" / "hermes" / "cron.yaml").read_text())
    card = {"name": "atlas-calibration-review", **spec["jobs"]["atlas-calibration-review"]["card"]}
    jobs.card_job(card, T0, cards)
    assert cards.calls[0]["key"] == "weekly-calibration-2026-W39"


# --------------------------------------------------------------------------- HTTP client and kanban CLI


def test_http_client_against_the_ops_api(ops_dir, engine, clock, cards):
    tokens = TokenStore([{"name": "operations-monitor/cron", "sha256": hash_token("cron-tok"), "scopes": ["ops:read"]}],
                        ENGINE_SCOPES)
    httpd = make_server(OpsService(engine), tokens, "127.0.0.1", 0, routes=OPS_ROUTES)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        c = jobs.OpsClient(url, "cron-tok")
        assert c("operations/status")["state"] == "NORMAL"
        with pytest.raises(jobs.EngineError, match="403 forbidden"):
            c("operations/disable_trading", reason="the cron token must not be able to do this")
        with pytest.raises(jobs.EngineError, match="401"):
            jobs.OpsClient(url, "wrong")("operations/status")
        engine.inject("db_write_failure")
        assert jobs.health_check(c, clock(), cards).startswith("HALT: db_write_failure.")
    finally:
        httpd.shutdown()
    with pytest.raises(jobs.EngineError, match="unreachable"):
        jobs.OpsClient(url, "cron-tok", timeout=2)("operations/status")


def test_client_needs_url_and_token(monkeypatch):
    monkeypatch.delenv("ATLAS_ENGINE_URL", raising=False)
    monkeypatch.delenv("ATLAS_TOKEN_OPS_CRON", raising=False)
    with pytest.raises(SystemExit, match="ATLAS_TOKEN_OPS_CRON"):
        jobs.OpsClient()


def test_kanban_create_command(monkeypatch, tmp_path):
    seen = {}

    class Proc:
        returncode, stdout, stderr = 0, json.dumps({"id": "t_abc"}), ""

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        return Proc()

    monkeypatch.setenv("ATLAS_HERMES_BIN", "/venv/bin/hermes")
    monkeypatch.setattr(jobs.subprocess, "run", fake_run)
    assert jobs.kanban_create("atlas-ops", "Incident", "operations-monitor", "body", key="incident-k",
                              tenant="paper", skills=("incident-triage",), created_by="om/hc") == "t_abc"
    cmd = seen["cmd"]
    assert cmd[:5] == ["/venv/bin/hermes", "kanban", "--board", "atlas-ops", "create"]
    for flag, value in [("--assignee", "operations-monitor"), ("--idempotency-key", "incident-k"),
                        ("--tenant", "paper"), ("--skill", "incident-triage"), ("--created-by", "om/hc")]:
        assert cmd[cmd.index(flag) + 1] == value
    Proc.returncode, Proc.stderr = 2, "no such board"
    with pytest.raises(RuntimeError, match="no such board"):
        jobs.kanban_create("atlas-ops", "x", "y", "z", key="k")


def test_main_prints_only_when_there_is_something(ops_dir, capsys, monkeypatch):
    monkeypatch.setitem(jobs.JOBS, "alert-relay", lambda spec: jobs.alert_relay(T0))
    jobs.main("alert-relay")
    assert capsys.readouterr().out == ""
    store.alert("info", "kanban", "hello")
    jobs.main("alert-relay")
    assert "hello" in capsys.readouterr().out
