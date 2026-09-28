"""The MCP servers: tool surface, and MCP -> HTTP API end to end with real tokens."""

import asyncio
import json
import threading

import pytest
from mcp.client import Client

from atlas_api.auth import TokenStore, hash_token
from atlas_api.http import make_server
from atlas_mcp.servers import SERVERS, ApiClient

EXPECTED_TOOLS = {
    "atlas-market": {"collect_market_state", "get_bars", "get_spread_stats"},
    "atlas-backtest": {"run_backtest", "run_walk_forward", "run_exit_research", "monte_carlo", "get_run_summary", "list_runs"},
    "atlas-journal": {"query_trades", "mfe_mae", "loss_clusters"},
    "atlas-performance": {"performance_summary"},
    "atlas-operations": {"system_status", "health_state", "reconciliation_report", "disable_trading"},
}

# PRD §6: none of these exists on any MCP server. flatten_all belongs to the
# operator-only atlas-emergency server; submit_trade_intent to atlas-trading (T7).
FORBIDDEN = {"send_raw_order", "modify_risk", "enable_trading", "access_holdout", "flatten_all",
             "submit_trade_intent", "set_risk", "close_position", "place_order", "clear_kill", "reenable_trading"}
# The one safe write (PRD §6, §23) exists on atlas-operations only.
ONLY_ON_OPERATIONS = {"disable_trading"}


def _run(coro):
    return asyncio.run(coro)


async def _tools(server) -> dict:
    async with Client(server) as c:
        return {t.name: t for t in (await c.list_tools()).tools}


async def _call(server, name, args) -> tuple[bool, str]:
    async with Client(server) as c:
        r = await c.call_tool(name, args)
        return r.is_error, r.content[0].text


def _unused_api(route, **args):
    raise AssertionError("not called")


@pytest.mark.parametrize("name", sorted(SERVERS))
def test_tool_surface(name):
    tools = _run(_tools(SERVERS[name](_unused_api)))
    assert set(tools) == EXPECTED_TOOLS[name]
    for tool_name, tool in tools.items():
        assert tool_name not in FORBIDDEN
        assert "holdout" not in tool_name and "enable" not in tool_name.replace("disable", "")
        assert tool.description
        if name != "atlas-operations":
            assert tool_name not in ONLY_ON_OPERATIONS


def test_no_server_has_a_forbidden_tool():
    names = set().union(*EXPECTED_TOOLS.values())
    assert not names & FORBIDDEN
    assert set(SERVERS) == set(EXPECTED_TOOLS)


def test_servers_only_forward_to_their_own_routes():
    """Each tool maps to one API route of its own server's area."""
    seen = {}
    for name, factory in SERVERS.items():
        calls = []
        server = factory(lambda route, **a: calls.append(route) or {"ok": True})
        tools = _run(_tools(server))
        for tool, t in tools.items():
            required = t.input_schema.get("required", [])
            args = {k: {"symbol": "EURUSD", "symbols": ["EURUSD"], "timeframe": "H1", "window": "dev",
                        "strategy": "trend_pullback", "run_id": "r", "reason": "drill reason text"}[k]
                    for k in required}
            err, text = _run(_call(server, tool, args))
            assert not err, (tool, text)
        area = name.removeprefix("atlas-")
        assert calls and all(r.startswith(area + "/") for r in calls), (name, calls)
        seen[name] = calls
    assert len(seen) == len(EXPECTED_TOOLS)


@pytest.fixture()
def api(service, tmp_path):
    """The real HTTP API on a free port, with one token per (profile, server) like bootstrap issues."""
    grants = {
        "strategy-researcher/atlas-backtest": ["backtest:run", "backtest:read"],
        "risk-analyst/atlas-backtest": ["backtest:read"],
        "risk-analyst/atlas-journal": ["journal:read"],
        "market-researcher/atlas-market": ["market:read"],
    }
    tokens = TokenStore([{"name": n, "sha256": hash_token("t-" + n), "scopes": s} for n, s in grants.items()])
    httpd = make_server(service, tokens, "127.0.0.1", 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield lambda name: ApiClient(url, "t-" + name, timeout=60)
    httpd.shutdown()


def test_end_to_end_research_flow(api):
    researcher = SERVERS["atlas-backtest"](api("strategy-researcher/atlas-backtest"))
    err, text = _run(_call(researcher, "run_backtest", {"strategy": "session_breakout", "window": "validation"}))
    assert not err, text
    run_id = json.loads(text)["run_id"]

    risk = SERVERS["atlas-backtest"](api("risk-analyst/atlas-backtest"))
    err, text = _run(_call(risk, "monte_carlo", {"run_id": run_id, "sims": 500}))
    assert not err, text
    assert "dd_p95_pct" in json.loads(text)
    err, text = _run(_call(risk, "list_runs", {}))
    assert not err and run_id in text

    journal = SERVERS["atlas-journal"](api("risk-analyst/atlas-journal"))
    err, text = _run(_call(journal, "mfe_mae", {"run_id": run_id, "group_by": "session"}))
    assert not err, text


def test_scope_refusal_reaches_the_agent_as_a_tool_error(api):
    risk = SERVERS["atlas-backtest"](api("risk-analyst/atlas-backtest"))
    err, text = _run(_call(risk, "run_backtest", {"strategy": "trend_pullback"}))
    assert err and "refused (forbidden)" in text and "backtest:run" in text

    # A token lifted from one server is still limited to its own scopes elsewhere.
    wrong = SERVERS["atlas-market"](api("risk-analyst/atlas-journal"))
    err, text = _run(_call(wrong, "get_bars", {"symbol": "EURUSD", "timeframe": "H1", "window": "dev"}))
    assert err and "refused (forbidden)" in text


@pytest.mark.parametrize("window", ["holdout", {"start": "2020-12-01", "end": "2021-02-01"}], ids=str)
def test_holdout_refusal_reaches_the_agent_as_a_tool_error(api, loader, window):
    market = SERVERS["atlas-market"](api("market-researcher/atlas-market"))
    err, text = _run(_call(market, "get_bars", {"symbol": "EURUSD", "timeframe": "H1", "window": window}))
    assert err and "refused (holdout_refused)" in text
    backtest = SERVERS["atlas-backtest"](api("strategy-researcher/atlas-backtest"))
    err, text = _run(_call(backtest, "run_backtest", {"strategy": "trend_pullback", "window": window}))
    assert err and "refused (holdout_refused)" in text
    assert loader.calls == []


def test_bad_token_and_unreachable_api():
    err, text = _run(_call(SERVERS["atlas-performance"](ApiClient("http://127.0.0.1:9", "x", timeout=2)),
                           "performance_summary", {"run_id": "r"}))
    assert err and "unreachable" in text
