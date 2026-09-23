"""The ATLAS MCP servers: atlas-market, atlas-backtest, atlas-journal, atlas-performance (H2)
and atlas-operations (H3).

Run one per stdio process, e.g. ``atlas-mcp-backtest``. Tool results are
compact JSON computed by the API; an API refusal (scope, holdout, budget)
comes back to the agent as a tool error carrying the API's message.
atlas-operations talks to the engine API instead of the research API; its
profile config points ATLAS_API_URL at the engine.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Callable, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

DEFAULT_URL = "http://127.0.0.1:8741"

Window = str | dict[str, str]
WINDOW_HELP = ("'dev' (2019-2023), 'validation' (Jan 2024-Jun 2025), or {\"start\": \"YYYY-MM-DD\", \"end\": \"YYYY-MM-DD\"} "
               "(end exclusive). The holdout is refused.")


class ApiClient:
    """POSTs JSON to the research API with this server's bearer token."""

    def __init__(self, url: str | None = None, token: str | None = None, timeout: float | None = None):
        self.url = (url or os.environ.get("ATLAS_API_URL") or DEFAULT_URL).rstrip("/")
        self.token = token if token is not None else os.environ.get("ATLAS_ENGINE_TOKEN", "")
        # A walk-forward on five years of M1 data takes minutes; keep below the Hermes tool timeout.
        self.timeout = timeout or float(os.environ.get("ATLAS_API_TIMEOUT", 1700))

    def __call__(self, route: str, **args: Any) -> dict:
        body = json.dumps({k: v for k, v in args.items() if v is not None}).encode()
        req = urllib.request.Request(f"{self.url}/v1/{route}", data=body, method="POST", headers={
            "Content-Type": "application/json", "Authorization": f"Bearer {self.token}"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            try:
                err = json.loads(e.read())
            except (ValueError, OSError):
                err = {"error": e.reason, "code": str(e.code)}
            raise ToolError(f"refused ({err.get('code', e.code)}): {err.get('error')}") from None
        except urllib.error.URLError as e:
            raise ToolError(f"ATLAS API unreachable at {self.url}: {e.reason}") from None


def market_server(api: Callable[..., dict]) -> MCPServer:
    s = MCPServer("atlas-market", instructions="Read-only ATLAS market data (dev and validation periods only).")

    @s.tool()
    def collect_market_state(symbols: list[str]) -> dict:
        """Compact state per symbol (mid, ATR, H1 trend and ADX, spread, prior-day range) at the last research bar."""
        return api("market/state", symbols=symbols)

    @s.tool(description=f"M15 or H1 mid-price bars with spread, newest last. window: {WINDOW_HELP}")
    def get_bars(symbol: str, timeframe: Literal["M15", "H1"], window: Window, limit: int = 200) -> dict:
        return api("market/bars", symbol=symbol, timeframe=timeframe, window=window, limit=limit)

    @s.tool(description=f"Spread median and p95 in pips per trading session. window: {WINDOW_HELP}")
    def get_spread_stats(symbol: str, window: Window) -> dict:
        return api("market/spread_stats", symbol=symbol, window=window)

    return s


def backtest_server(api: Callable[..., dict]) -> MCPServer:
    s = MCPServer("atlas-backtest", instructions=(
        "ATLAS backtests on development and validation data only. Every run is recorded as an experiment, "
        "counts against the monthly budget and the deflated Sharpe, and the holdout is refused."))

    @s.tool(description=(
        "Backtest one parameter set of a configured strategy on one window, with bid/ask fills and stressed costs. "
        f"Recorded as an experiment. window: {WINDOW_HELP}"))
    def run_backtest(strategy: str, window: Window = "validation", params: dict[str, Any] | None = None,
                     symbols: list[str] | None = None, spread_mult: float | None = None) -> dict:
        return api("backtest/run", strategy=strategy, window=window, params=params, symbols=symbols,
                   spread_mult=spread_mult)

    @s.tool()
    def run_walk_forward(strategy: str) -> dict:
        """Full T0 pipeline for a configured strategy: dev walk-forward, validation once, every PRD §15 gate. Slow."""
        return api("backtest/walk_forward", strategy=strategy)

    @s.tool()
    def monte_carlo(run_id: str, sims: int = 10000, skip_frac: float = 0.0) -> dict:
        """Reshuffle a recorded run's trades: drawdown percentiles and daily-loss breach probability."""
        return api("backtest/monte_carlo", run_id=run_id, sims=sims, skip_frac=skip_frac)

    @s.tool()
    def get_run_summary(run_id: str) -> dict:
        """Summary, gates and Kanban metadata of a recorded run."""
        return api("backtest/summary", run_id=run_id)

    @s.tool()
    def list_runs(strategy: str | None = None, limit: int = 20) -> dict:
        """Recorded experiments, newest first, failures included."""
        return api("backtest/list_runs", strategy=strategy, limit=limit)

    return s


def journal_server(api: Callable[..., dict]) -> MCPServer:
    s = MCPServer("atlas-journal", instructions="Read-only ATLAS trade journal (recorded research trades for now).")

    @s.tool()
    def query_trades(run_id: str, symbol: str | None = None, session: Literal["asia", "london", "overlap", "new_york"] | None = None,
                     direction: Literal[1, -1] | None = None, outcome: Literal["win", "loss"] | None = None,
                     limit: int = 50) -> dict:
        """Filtered trades of a recorded run, newest last (at most 200 rows)."""
        return api("journal/trades", run_id=run_id, symbol=symbol, session=session, direction=direction,
                   outcome=outcome, limit=limit)

    @s.tool()
    def mfe_mae(run_id: str, group_by: Literal["exit_reason", "session", "symbol", "direction"] = "exit_reason") -> dict:
        """Maximum favourable and adverse excursion in R per group, and how many losers first reached +1R."""
        return api("journal/mfe_mae", run_id=run_id, group_by=group_by)

    @s.tool()
    def loss_clusters(run_id: str) -> dict:
        """Longest losing streak and the worst sessions, weekdays, hours and symbols by total R."""
        return api("journal/loss_clusters", run_id=run_id)

    return s


def performance_server(api: Callable[..., dict]) -> MCPServer:
    s = MCPServer("atlas-performance", instructions="Read-only ATLAS performance statistics in R after costs.")

    @s.tool()
    def performance_summary(run_id: str, by: Literal["year", "session", "symbol"] | None = None) -> dict:
        """Expectancy, profit factor, win rate and costs in R for a recorded run, optionally broken down."""
        return api("performance/summary", run_id=run_id, by=by)

    return s


def operations_server(api: Callable[..., dict]) -> MCPServer:
    s = MCPServer("atlas-operations", instructions=(
        "ATLAS trading-engine operations: status, health state, reconciliation, and one safe write, "
        "disable_trading. Nothing here can enable trading, flatten positions or change a limit; those "
        "are the operator's, outside Hermes."))

    @s.tool()
    def system_status() -> dict:
        """Engine mode, health state, whether new trades are enabled (and who disabled them), MT5 link,
        heartbeats, clock drift, open positions and risk, and today's loss and drawdown as a fraction of
        the firm's limits."""
        return api("operations/status")

    @s.tool()
    def health_state(symbol: str | None = None) -> dict:
        """PRD §23 health: NORMAL, DEGRADED (no new trades on a symbol), HALT (no new trades at all) or
        KILL (flattened and disabled), with the reason codes and the telemetry behind them."""
        return api("operations/health", symbol=symbol)

    @s.tool()
    def reconciliation_report() -> dict:
        """Last engine-vs-broker reconciliation: when it ran, positions on each side, and every mismatch."""
        return api("operations/reconciliation")

    @s.tool()
    def disable_trading(reason: str) -> dict:
        """Stop the engine from opening new trades. Open positions keep their broker-side stops. Use it
        when health is HALT or KILL and trading is still enabled, or when you see an anomaly the engine
        has not caught. Give the reason in one or two sentences (10-500 characters): what you saw and
        where. It cannot be undone from here; only the operator re-enables trading."""
        return api("operations/disable_trading", reason=reason)

    return s


SERVERS = {
    "atlas-market": market_server,
    "atlas-backtest": backtest_server,
    "atlas-journal": journal_server,
    "atlas-performance": performance_server,
    "atlas-operations": operations_server,
}


def _run(name: str) -> None:
    SERVERS[name](ApiClient()).run("stdio")


def main_market() -> None:
    _run("atlas-market")


def main_backtest() -> None:
    _run("atlas-backtest")


def main_journal() -> None:
    _run("atlas-journal")


def main_performance() -> None:
    _run("atlas-performance")


def main_operations() -> None:
    _run("atlas-operations")
