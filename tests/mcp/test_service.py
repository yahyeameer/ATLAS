"""Research API behaviour behind the MCP tools: experiment budget, registry records, limits."""

import pytest

from atlas_api.auth import SCOPES, Principal
from atlas_api.http import dispatch

from conftest import token_for

ALL = Principal("risk-analyst/atlas-backtest", frozenset(SCOPES))
TOK = token_for(sorted(SCOPES))
Q4 = {"start": "2020-10-01", "end": "2021-01-01"}


def test_every_backtest_is_recorded_with_its_requester(service):
    res = service.run_backtest(ALL, "session_breakout", Q4, params={"mode": "retest"})
    (entry,) = service.registry.entries("session_breakout")
    assert entry["experiment_id"] == res["run_id"]
    assert entry["kind"] == "backtest" and entry["requested_by"] == ALL.name
    assert entry["final_params"]["mode"] == "retest"
    assert len(entry["trial_sharpes"]) == 1  # counts toward the deflated Sharpe
    meta = res["kanban_metadata"]
    assert {"experiment_id", "strategy_version", "data_window", "trades", "expectancy_r", "pf"} <= set(meta)
    assert (service.runs_dir / res["run_id"] / "trades.csv").exists()


def test_runs_are_recorded_whatever_the_result(service):
    res = service.run_backtest(ALL, "trend_pullback", Q4)  # a random walk: no edge to find
    assert [e["experiment_id"] for e in service.registry.entries()] == [res["run_id"]]


def test_budget_exhaustion_is_429_and_does_no_work(service, tokens, loader):
    args = {"strategy": "session_breakout", "window": Q4}
    for _ in range(3):  # fixture budget: 3 per strategy per month
        assert dispatch(service, tokens, "backtest/run", TOK, args)[0] == 200
    calls = len(loader.calls)
    status, body = dispatch(service, tokens, "backtest/run", TOK, args)
    assert (status, body["code"]) == (429, "budget_exceeded")
    assert dispatch(service, tokens, "backtest/walk_forward", TOK, {"strategy": "session_breakout"})[0] == 429
    assert len(loader.calls) == calls
    assert len(service.registry.entries("session_breakout")) == 3
    # Other strategies keep their own budget.
    assert dispatch(service, tokens, "backtest/run", TOK, {**args, "strategy": "trend_pullback"})[0] == 200


@pytest.mark.parametrize("args", [
    {"strategy": "martingale"},
    {"strategy": "liquidity_sweep", "symbols": ["GBPUSD"]},   # validated for EURUSD only
    {"strategy": "trend_pullback", "symbols": ["XAUUSD"]},
    {"strategy": "trend_pullback", "params": {"risk_pct": 2.0}},
    {"strategy": "trend_pullback", "spread_mult": 0.5},        # costs can only be stressed, never cut
    {"strategy": "trend_pullback", "spread_mult": 10},
    {"strategy": "trend_pullback", "window": "last_week"},
    {"strategy": "trend_pullback", "window": {"start": "2020-06-01", "end": "2020-05-01"}},
])
def test_bad_backtest_requests_are_400_and_not_recorded(service, tokens, args):
    status, body = dispatch(service, tokens, "backtest/run", TOK, {"window": Q4, **args})
    assert (status, body["code"]) == (400, "bad_request"), body
    assert service.registry.entries() == []


def test_walk_forward_returns_gates_and_is_readable(service, tokens):
    status, wf = dispatch(service, tokens, "backtest/walk_forward", TOK, {"strategy": "liquidity_sweep"})
    assert status == 200
    assert wf["passed"] in (True, False) and wf["gates"]
    assert all({"gate", "scope", "value", "rule", "passed"} <= set(g) for g in wf["gates"])
    (entry,) = service.registry.entries("liquidity_sweep")
    assert entry["experiment_id"] == wf["run_id"]
    for route in ("backtest/summary", "performance/summary", "journal/mfe_mae", "journal/loss_clusters"):
        assert dispatch(service, tokens, route, TOK, {"run_id": wf["run_id"]})[0] == 200, route


def test_outputs_are_bounded(service, tokens):
    run = service.run_backtest(ALL, "session_breakout", "validation")["run_id"]
    _, bars = dispatch(service, tokens, "market/bars", TOK,
                       {"symbol": "EURUSD", "timeframe": "M15", "window": "dev", "limit": 100000})
    assert len(bars["rows"]) == 500
    _, trades = dispatch(service, tokens, "journal/trades", TOK, {"run_id": run, "limit": 100000})
    assert trades["returned"] <= 200
    _, mc = dispatch(service, tokens, "backtest/monte_carlo", TOK, {"run_id": run, "sims": 10**9})
    assert mc["sims"] == 20000


def test_results_are_in_r_after_costs(service, tokens):
    run = service.run_backtest(ALL, "session_breakout", "validation")["run_id"]
    _, perf = dispatch(service, tokens, "performance/summary", TOK, {"run_id": run, "by": "year"})
    assert {"expectancy_r", "profit_factor", "avg_cost_r", "trades"} <= set(perf["overall"])
    assert perf["overall"]["avg_cost_r"] > 0
