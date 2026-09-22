"""H2 exit gate, part 2: the locked holdout is refused server-side (PRD §6, §13, §22).

No token, window name, custom window, run id or tool reads holdout data. The
spy loader proves refused calls never reach the data, and a sweep of every
allowed route never asks for a row at or after the holdout start.
"""

import json

import pandas as pd
import pytest

from atlas_api.auth import SCOPES, Principal
from atlas_api.http import ROUTES, dispatch
from atlas_api.service import HoldoutRefused

from conftest import HOLDOUT, token_for
from routes import ROUTE_ARGS, RUN_ROUTES, WINDOW_ROUTES, with_run

ALL_TOKEN = token_for(sorted(SCOPES))
ALL = Principal("all", frozenset(SCOPES))

HOLDOUT_WINDOWS = [
    "holdout",
    "HOLDOUT",
    "locked_holdout",
    {"start": "2021-01-01", "end": "2021-02-01"},    # inside the holdout
    {"start": "2020-12-01", "end": "2021-01-02"},    # straddles the boundary by one day
    {"start": "2020-12-31", "end": "2021-01-01T00:01:00"},  # by one minute
    {"start": "2019-01-01", "end": "2030-01-01"},    # everything
    {"start": "2021-01-01T00:00:00+05:00", "end": "2021-01-01T05:01:00+05:00"},  # offset tz, 1 min into holdout
]


@pytest.mark.parametrize("route", sorted(WINDOW_ROUTES))
@pytest.mark.parametrize("window", HOLDOUT_WINDOWS, ids=str)
def test_window_routes_refuse_the_holdout(service, tokens, loader, route, window):
    status, body = dispatch(service, tokens, route, ALL_TOKEN, {**WINDOW_ROUTES[route], "window": window})
    assert (status, body["code"]) == (403, "holdout_refused"), body
    assert loader.calls == []
    assert service.registry.entries() == []


@pytest.mark.parametrize("route", sorted(WINDOW_ROUTES))
def test_window_ending_at_the_holdout_start_is_allowed(service, tokens, loader, route):
    window = {"start": "2020-12-01", "end": "2021-01-01"}
    status, body = dispatch(service, tokens, route, ALL_TOKEN, {**WINDOW_ROUTES[route], "window": window})
    assert status == 200, body
    assert loader.max_end() <= HOLDOUT


@pytest.mark.parametrize("window", HOLDOUT_WINDOWS, ids=str)
def test_window_resolver_refuses_on_its_own(service, window):
    """The first layer: window() refuses before any loader guard is reached."""
    with pytest.raises(HoldoutRefused):
        service.window(window)


def test_named_windows_resolve_before_the_holdout(service):
    for name in ("dev", "validation"):
        start, end = service.window(name)
        assert start < end <= HOLDOUT


def test_loader_is_guarded_even_if_window_check_is_bypassed(service, loader):
    with pytest.raises(HoldoutRefused):
        service._m1("EURUSD", pd.Timestamp("2020-12-01", tz="UTC"), HOLDOUT + pd.Timedelta(minutes=1))
    assert loader.calls == []


def test_sweep_of_every_route_never_loads_holdout_rows(service, tokens, loader):
    run_id = service.run_backtest(ALL, "trend_pullback", "validation")["run_id"]
    for route in sorted(ROUTES):
        status, body = dispatch(service, tokens, route, ALL_TOKEN, with_run(ROUTE_ARGS[route], run_id))
        assert status == 200, (route, body)
    assert loader.calls, "the sweep should have loaded data"
    assert loader.max_end() <= HOLDOUT
    for sym, start, end in loader.calls:
        assert end <= HOLDOUT, (sym, start, end)
    # Every trade any route can return closed before the holdout.
    status, body = dispatch(service, tokens, "journal/trades", ALL_TOKEN, {"run_id": run_id, "limit": 200})
    exit_col = body["columns"].index("exit_time")
    assert all(pd.Timestamp(r[exit_col]) <= HOLDOUT for r in body["rows"])


def _plant_run(service, run_id: str, exit_time: str, summary: dict | None = None, name: str = "trades.csv"):
    d = service.runs_dir / run_id
    d.mkdir(parents=True)
    pd.DataFrame([{
        "symbol": "EURUSD", "direction": 1, "setup": "trend_pullback",
        "decision_time": "2020-12-30 10:00:00+00:00", "entry_time": "2020-12-30 10:00:00+00:00",
        "exit_time": exit_time, "exit_reason": "target", "r": 2.0, "cost_r": 0.1, "mfe_r": 2.1, "mae_r": 0.2,
    }]).to_csv(d / name, index=False)
    (d / "summary.json").write_text(json.dumps(summary or {"run_id": run_id, "kind": "backtest"}))


@pytest.mark.parametrize("route", RUN_ROUTES)
@pytest.mark.parametrize("trades_file", ["trades.csv", "oos_trades.csv"])
def test_runs_with_holdout_trades_are_refused(service, tokens, route, trades_file):
    _plant_run(service, "leaky", "2021-01-04 12:00:00+00:00", name=trades_file)
    status, body = dispatch(service, tokens, route, ALL_TOKEN, with_run(ROUTE_ARGS[route], "leaky"))
    assert (status, body["code"]) == (403, "holdout_refused"), body


@pytest.mark.parametrize("summary", [
    {"kind": "holdout"},
    {"kind": "backtest", "holdout": {"expectancy_r": 0.2}},
    {"kind": "backtest", "window": ["2020-07-01", "2021-03-01"]},
], ids=["kind", "holdout-key", "window"])
def test_holdout_summaries_are_refused(service, tokens, summary):
    _plant_run(service, "t6-result", "2020-12-30 11:00:00+00:00", summary={"run_id": "t6-result", **summary})
    status, body = dispatch(service, tokens, "backtest/summary", ALL_TOKEN, {"run_id": "t6-result"})
    assert (status, body["code"]) == (403, "holdout_refused"), body


def test_holdout_registry_entries_are_not_listed(service, tokens):
    service.registry.append({"experiment_id": "t6-x", "kind": "holdout", "strategy": "trend_pullback",
                             "created_at": "2026-09-01T00:00:00+00:00", "kanban_metadata": {"expectancy_r": 0.3}})
    service.registry.append({"experiment_id": "bt-x", "kind": "backtest", "strategy": "trend_pullback",
                             "created_at": "2026-09-01T00:00:00+00:00", "kanban_metadata": {}})
    status, body = dispatch(service, tokens, "backtest/list_runs", ALL_TOKEN, {})
    assert status == 200
    assert [r["run_id"] for r in body["runs"]] == ["bt-x"]


def test_trade_closing_exactly_at_holdout_start_is_readable(service, tokens):
    _plant_run(service, "edge", "2021-01-01 00:00:00+00:00")
    assert dispatch(service, tokens, "journal/trades", ALL_TOKEN, {"run_id": "edge"})[0] == 200
