"""Engine operations API: scopes, the one write, and MCP -> HTTP -> simulated engine end to end."""

import asyncio
import json
import threading

import pytest
from mcp.client import Client

from atlas_api.auth import SCOPES, Principal, TokenStore, hash_token, issue
from atlas_api.http import ROUTES, dispatch, make_server
from atlas_api.ops import ENGINE_SCOPES, OPS_ROUTES, OpsService, main as sim_main
from atlas_mcp.scopes import SERVER_TOOLS, api_of
from atlas_mcp.servers import SERVERS, ApiClient

from opshelp import ENGINE_TOKEN_SCOPES, clock, engine, engine_token, engine_tokens  # noqa: F401

ROUTE_SCOPE = {"operations/status": "ops:read", "operations/health": "ops:read",
               "operations/reconciliation": "ops:read", "operations/events": "ops:read",
               "operations/disable_trading": "ops:disable_trading"}
ROUTE_ARGS = {"operations/disable_trading": {"reason": "drill: MT5 disconnected for 90 s"},
              "operations/health": {"symbol": "EURUSD"}, "operations/events": {"since": 0, "limit": 10}}


@pytest.fixture()
def service(engine):
    return OpsService(engine)


def call(service, tokens, route, token, args=None):
    return dispatch(service, tokens, route, token, args or ROUTE_ARGS.get(route, {}), OPS_ROUTES)


def test_contract():
    assert set(ROUTE_SCOPE) == set(OPS_ROUTES)
    assert set(ENGINE_SCOPES) == {"ops:read", "ops:disable_trading"}
    # The only write scope stops trading; nothing can enable, flatten, clear a kill or change a limit.
    for route in OPS_ROUTES:
        assert not any(w in route for w in ("enable_trading", "flatten", "kill", "risk", "limit", "order"))
    assert not set(OPS_ROUTES) & set(ROUTES)
    # Research and engine scope sets are disjoint: no token file can carry both kinds.
    assert not set(SCOPES) & set(ENGINE_SCOPES)


@pytest.mark.parametrize("route", sorted(OPS_ROUTES))
@pytest.mark.parametrize("scopes", ENGINE_TOKEN_SCOPES, ids=lambda s: "+".join(s) or "no-scope")
def test_scope_matrix(service, engine_tokens, route, scopes):
    status, body = call(service, engine_tokens, route, engine_token(scopes))
    if ROUTE_SCOPE[route] in scopes:
        assert status == 200, body
        assert body["source"] == "simulated"
    else:
        assert (status, body["code"]) == (403, "forbidden")


def test_refused_disable_changes_nothing(service, engine_tokens, engine):
    status, _ = call(service, engine_tokens, "operations/disable_trading", engine_token(["ops:read"]))
    assert status == 403 and engine.status()["trading"]["enabled"]


@pytest.mark.parametrize("token", [None, "", "atl_research-token", "etok-nope"])
def test_unknown_tokens_are_401(service, engine_tokens, token):
    assert call(service, engine_tokens, "operations/status", token)[0] == 401


def test_research_tokens_do_not_work_here(service, tmp_path):
    """Separate token files: a research token with every research scope is unknown to the engine."""
    research, eng = tmp_path / "api-tokens.yaml", tmp_path / "engine-tokens.yaml"
    rtok = issue(research, "risk-analyst/atlas-backtest", sorted(SCOPES))
    etok = issue(eng, "operations-monitor/atlas-operations", ["ops:read"], known=ENGINE_SCOPES)
    assert call(service, TokenStore.load(eng, ENGINE_SCOPES), "operations/status", rtok)[0] == 401
    assert call(service, TokenStore.load(eng, ENGINE_SCOPES), "operations/status", etok)[0] == 200
    with pytest.raises(ValueError, match="unknown scope"):
        issue(research, "x", ["ops:read"])
    with pytest.raises(ValueError, match="unknown scope"):
        issue(eng, "x", ["backtest:run"], known=ENGINE_SCOPES)


@pytest.mark.parametrize("reason", ["", "short", "x" * 501, None, 42])
def test_disable_needs_a_real_reason(service, engine_tokens, engine, reason):
    status, body = call(service, engine_tokens, "operations/disable_trading", engine_token(["ops:disable_trading"]),
                        {"reason": reason})
    assert status == 400 and body["code"] == "bad_request"
    assert engine.status()["trading"]["enabled"]


def test_disable_records_the_principal(service, engine_tokens, engine):
    status, body = call(service, engine_tokens, "operations/disable_trading", engine_token(["ops:disable_trading"]))
    assert status == 200 and body["trading"]["by"] == "etok-ops:disable_trading"
    assert not engine.status()["trading"]["enabled"]


def test_bad_arguments(service, engine_tokens):
    tok = engine_token(sorted(ENGINE_SCOPES))
    assert call(service, engine_tokens, "operations/health", tok, {"symbol": "XAGUSD"})[0] == 400
    assert call(service, engine_tokens, "operations/events", tok, {"since": -1})[0] == 400
    assert call(service, engine_tokens, "operations/events", tok, {"limit": 1000})[0] == 400
    assert call(service, engine_tokens, "operations/status", tok, {"extra": 1})[0] == 400
    assert call(service, engine_tokens, "operations/enable_trading", tok)[0] == 404


def test_engine_down_is_503(tmp_path, engine_tokens):
    from atlas_engine.ops.sim import SimulatedEngine
    svc = OpsService(SimulatedEngine(tmp_path / "missing.json"))
    status, body = call(svc, engine_tokens, "operations/status", engine_token(["ops:read"]))
    assert status == 503 and body["code"] == "engine_unavailable"


def test_principal_helper(service):
    with pytest.raises(PermissionError):
        service.system_status(Principal("x", frozenset({"backtest:read"})))


# --------------------------------------------------------------------------- MCP end to end


@pytest.fixture()
def api(engine):
    grants = {"operations-monitor/atlas-operations": ["ops:read", "ops:disable_trading"],
              "execution-engineer/atlas-operations": ["ops:read"]}
    tokens = TokenStore([{"name": n, "sha256": hash_token("t-" + n), "scopes": s} for n, s in grants.items()],
                        ENGINE_SCOPES)
    httpd = make_server(OpsService(engine), tokens, "127.0.0.1", 0, routes=OPS_ROUTES)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield lambda name: ApiClient(url, "t-" + name, timeout=30)
    httpd.shutdown()


async def _call(server, name, args):
    async with Client(server) as c:
        r = await c.call_tool(name, args)
        return r.is_error, r.content[0].text


def test_operations_server_routes_match_scopes():
    assert api_of("atlas-operations") == "engine"
    calls = []
    srv = SERVERS["atlas-operations"](lambda route, **a: calls.append(route) or {"ok": True})
    for tool in SERVER_TOOLS["atlas-operations"]:
        args = {"reason": "drill reason text"} if tool == "disable_trading" else {}
        assert not asyncio.run(_call(srv, tool, args))[0]
    assert calls == ["operations/status", "operations/health", "operations/reconciliation",
                     "operations/disable_trading"]
    for tool, route in zip(SERVER_TOOLS["atlas-operations"], calls):
        assert SERVER_TOOLS["atlas-operations"][tool] == ROUTE_SCOPE[route]


def test_incident_flow_through_mcp(api, engine):
    monitor = SERVERS["atlas-operations"](api("operations-monitor/atlas-operations"))
    engineer = SERVERS["atlas-operations"](api("execution-engineer/atlas-operations"))
    engine.inject("mt5_disconnect")

    err, text = asyncio.run(_call(monitor, "health_state", {}))
    assert not err and json.loads(text)["state"] == "HALT"
    err, text = asyncio.run(_call(engineer, "reconciliation_report", {}))
    assert not err and json.loads(text)["status"] == "clean"

    # execution-engineer's token reads but cannot disable, even if it were offered the tool.
    err, text = asyncio.run(_call(engineer, "disable_trading", {"reason": "not mine to do, drill"}))
    assert err and "refused (forbidden)" in text and "ops:disable_trading" in text
    assert engine.status()["trading"]["enabled"]

    err, text = asyncio.run(_call(monitor, "disable_trading", {"reason": "HALT: mt5_disconnected for 90 s (drill)"}))
    assert not err and json.loads(text)["trading_enabled"] is False
    s = engine.status()
    assert s["trading"]["by"] == "operations-monitor/atlas-operations" and not any(s["new_trades_allowed"].values())

    err, text = asyncio.run(_call(monitor, "disable_trading", {"reason": "short"}))
    assert err and "refused (bad_request)" in text


# --------------------------------------------------------------------------- CLI


def test_cli_inject_enable_and_token(tmp_path, capsys):
    state, tokens = str(tmp_path / "sim.json"), str(tmp_path / "engine-tokens.yaml")
    sim_main(["--state", state, "init"])
    sim_main(["--state", state, "inject", "mt5_disconnect", "seconds=120"])
    sim_main(["--state", state, "show"])
    assert '"state": "HALT"' in capsys.readouterr().out
    with pytest.raises(SystemExit, match="refusing to re-enable"):
        sim_main(["--state", state, "enable", "--operator", "yahye", "--reason", "try"])
    sim_main(["--state", state, "clear"])
    sim_main(["--state", state, "kill", "--operator", "yahye", "--reason", "drill"])
    sim_main(["--state", state, "enable", "--operator", "yahye", "--reason", "drill over"])
    capsys.readouterr()
    sim_main(["--state", state, "token", "--tokens", tokens, "--name", "x", "--scopes", "ops:read"])
    tok = capsys.readouterr().out.strip()
    assert TokenStore.load(tmp_path / "engine-tokens.yaml", ENGINE_SCOPES).authenticate(tok).scopes == {"ops:read"}
