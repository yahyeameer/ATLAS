"""Every API route with the scope it needs and a valid argument set for the test calendar."""

RUN = "<recorded run>"  # replaced by a real run id in tests

ROUTE_SCOPE = {
    "market/state": "market:read",
    "market/bars": "market:read",
    "market/spread_stats": "market:read",
    "backtest/run": "backtest:run",
    "backtest/walk_forward": "backtest:run",
    "backtest/list_runs": "backtest:read",
    "backtest/summary": "backtest:read",
    "backtest/monte_carlo": "backtest:read",
    "journal/trades": "journal:read",
    "journal/mfe_mae": "journal:read",
    "journal/loss_clusters": "journal:read",
    "performance/summary": "performance:read",
}

ROUTE_ARGS = {
    "market/state": {"symbols": ["EURUSD"]},
    "market/bars": {"symbol": "EURUSD", "timeframe": "H1", "window": "validation", "limit": 5},
    "market/spread_stats": {"symbol": "EURUSD", "window": {"start": "2020-12-01", "end": "2020-12-08"}},
    "backtest/run": {"strategy": "session_breakout", "window": {"start": "2020-10-01", "end": "2021-01-01"}},
    "backtest/walk_forward": {"strategy": "liquidity_sweep"},
    "backtest/list_runs": {},
    "backtest/summary": {"run_id": RUN},
    "backtest/monte_carlo": {"run_id": RUN, "sims": 200},
    "journal/trades": {"run_id": RUN, "limit": 3},
    "journal/mfe_mae": {"run_id": RUN},
    "journal/loss_clusters": {"run_id": RUN},
    "performance/summary": {"run_id": RUN, "by": "session"},
}

# Routes that take a date window, with the argument that carries it.
WINDOW_ROUTES = {
    "market/bars": {"symbol": "EURUSD", "timeframe": "M15"},
    "market/spread_stats": {"symbol": "EURUSD"},
    "backtest/run": {"strategy": "trend_pullback"},
}

# Routes that read a recorded run's trades.
RUN_ROUTES = ["backtest/summary", "backtest/monte_carlo", "journal/trades", "journal/mfe_mae",
              "journal/loss_clusters", "performance/summary"]


def with_run(args: dict, run_id: str) -> dict:
    return {k: (run_id if v == RUN else v) for k, v in args.items()}
