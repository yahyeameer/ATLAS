"""The ATLAS dashboard tab: overview data and the FastAPI route (atlas_plugins/dashboard.py)."""

import json
from pathlib import Path

import pytest

from atlas_plugins import dashboard, hooks, jobs, store

from opshelp import T0, clock, engine, ops_dir  # noqa: F401

PLUGIN = Path(dashboard.__file__).parent / "hermes-plugin"


class Client:
    def __init__(self, engine):
        self.engine = engine

    def __call__(self, route, **a):
        return {"operations/status": self.engine.status, "operations/health": self.engine.health,
                "operations/reconciliation": self.engine.reconciliation}[route]()


def test_overview_panels(ops_dir, engine, tmp_path, monkeypatch):
    registry = tmp_path / "experiments.jsonl"
    registry.write_text("\n".join(json.dumps(e) for e in [
        {"strategy": "trend_pullback", "created_at": "2026-09-02T10:00:00+00:00"},
        {"strategy": "trend_pullback", "created_at": "2026-09-20T10:00:00+00:00"},
        {"strategy": "session_breakout", "created_at": "2026-08-31T23:00:00+00:00"},
    ]) + "\n")
    store.path("ops.yaml").parent.mkdir(parents=True, exist_ok=True)
    store.path("ops.yaml").write_text(f"research_registry: {registry}\nbudgets: {{atlas-research: 1000}}\n")
    monkeypatch.setattr(store, "iso", lambda t=None: "2026-09-23T14:00:00+00:00")  # hooks stamp "now"
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "atlas-research")
    hooks.on_post_api_request(usage={"input_tokens": 400, "output_tokens": 100})
    hooks.on_post_tool_call(tool_name="mcp__atlas_operations__disable_trading", args={"reason": "drill"},
                            result=json.dumps({"result": json.dumps({"trading_enabled": False})}))
    engine.inject("mt5_disconnect")
    engine.disable_trading("operations-monitor/atlas-operations", "drill")

    o = dashboard.overview(Client(engine), T0)
    assert o["engine"]["ok"] and o["engine"]["health"]["state"] == "HALT"
    assert o["engine"]["status"]["trading"]["enabled"] is False
    assert o["actions"][0]["tool"] == "disable_trading" and o["writes_today"] == 1
    assert o["alerts"][0]["severity"] == "critical"
    assert o["budget"] == {"month": "2026-09", "boards": [
        {"board": "atlas-research", "used": 500, "budget": 1000, "frac": 0.5}]}
    assert o["experiments"]["strategies"] == [{"strategy": "trend_pullback", "runs": 2}]
    assert set(o["deferred"]) == {"equity", "risk", "calibration", "cost"}
    json.dumps(o)  # the route returns it as JSON


def test_overview_survives_a_dead_engine_and_empty_ops_dir(ops_dir):
    def down(route, **a):
        raise jobs.EngineError("engine API unreachable")
    o = dashboard.overview(down, T0)
    assert o["engine"] == {"ok": False, "error": "engine API unreachable"}
    assert o["actions"] == [] and o["alerts"] == [] and not o["experiments"]["available"]


def test_dashboard_token_comes_from_the_profile_env(ops_dir, tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".env").write_text("ATLAS_ENGINE_URL=http://127.0.0.1:9\nATLAS_TOKEN_DASHBOARD=dash-tok\n")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("ATLAS_TOKEN_DASHBOARD", raising=False)
    monkeypatch.delenv("ATLAS_ENGINE_URL", raising=False)
    assert dashboard._env_value("ATLAS_TOKEN_DASHBOARD") == "dash-tok"
    panel = dashboard.engine_panels()
    assert not panel["ok"] and "unreachable" in panel["error"]


def test_route_serves_the_overview(ops_dir):
    pytest.importorskip("fastapi")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(dashboard.router, prefix="/api/plugins/atlas")
    r = TestClient(app).get("/api/plugins/atlas/overview")
    assert r.status_code == 200 and set(r.json()) >= {"engine", "actions", "alerts", "budget", "experiments"}


def test_plugin_directory_is_complete():
    manifest = json.loads((PLUGIN / "dashboard" / "manifest.json").read_text())
    assert manifest["name"] == "atlas" and manifest["tab"]["path"] == "/atlas"
    for key in ("entry", "css", "api"):
        assert (PLUGIN / "dashboard" / manifest[key]).is_file(), key
    js = (PLUGIN / "dashboard" / manifest["entry"]).read_text()
    assert 'register("atlas"' in js and "/api/plugins/atlas/overview" in js
    assert "router" in (PLUGIN / "dashboard" / "plugin_api.py").read_text()
    assert "register" in (PLUGIN / "__init__.py").read_text()
