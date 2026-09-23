"""HTTP front of the research API. JSON in, JSON out, bearer-token auth.

    POST /v1/<route>   Authorization: Bearer <token>   body: {"arg": ...}

Binds to localhost by default: the MCP servers run on the same agent host.
Status codes: 401 unknown token, 403 missing scope or holdout, 400 bad
arguments, 404 unknown run, 429 experiment budget spent, 503 engine unavailable.

The same dispatcher serves the engine's operations routes (atlas_api/ops.py),
with their own route table and token store.
"""

from __future__ import annotations

import argparse
import inspect
import json
import logging
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from atlas_research import data as rdata
from atlas_research.cli import DEFAULT_CONFIG, load_config
from atlas_research.registry import BudgetExceeded, Registry

from . import auth
from .auth import AuthError, ScopeError, TokenStore
from .service import BadRequest, HoldoutRefused, NotFound, ResearchService

log = logging.getLogger("atlas_api")

# route -> service method. Scopes are checked inside each method.
ROUTES = {
    "market/state": "market_state",
    "market/bars": "bars",
    "market/spread_stats": "spread_stats",
    "backtest/run": "run_backtest",
    "backtest/walk_forward": "run_walk_forward",
    "backtest/list_runs": "list_runs",
    "backtest/summary": "run_summary",
    "backtest/monte_carlo": "monte_carlo",
    "journal/trades": "query_trades",
    "journal/mfe_mae": "mfe_mae",
    "journal/loss_clusters": "loss_clusters",
    "performance/summary": "performance_summary",
}

MAX_BODY = 64 * 1024


def dispatch(service, tokens: TokenStore, route: str, token: str | None, args: dict,
             routes: dict[str, str] = ROUTES) -> tuple[int, dict]:
    """Authenticate, call, and map errors to (status, body). Shared by HTTP and tests."""
    try:
        principal = tokens.authenticate(token)
        if route not in routes:
            return 404, {"error": f"unknown route {route!r}", "code": "not_found"}
        if not isinstance(args, dict):
            return 400, {"error": "body must be a JSON object", "code": "bad_request"}
        method = getattr(service, routes[route])
        try:
            inspect.signature(method).bind(principal, **args)
        except TypeError as e:
            raise BadRequest(f"{route}: {e}") from None
        result = method(principal, **args)
        log.info("%s %s ok", principal.name, route)
        return 200, result
    except AuthError as e:
        return 401, {"error": str(e), "code": "unauthorized"}
    except HoldoutRefused as e:
        log.warning("holdout refused: %s", e)
        return 403, {"error": str(e), "code": "holdout_refused"}
    except ScopeError as e:
        log.warning("scope refused: %s", e)
        return 403, {"error": str(e), "code": "forbidden"}
    except BudgetExceeded as e:
        return 429, {"error": str(e), "code": "budget_exceeded"}
    except NotFound as e:
        return 404, {"error": str(e), "code": "not_found"}
    except BadRequest as e:
        return 400, {"error": str(e), "code": "bad_request"}
    except ConnectionError as e:
        log.error("engine unavailable: %s", e)
        return 503, {"error": str(e), "code": "engine_unavailable"}


def make_server(service, tokens: TokenStore, host: str, port: int,
                routes: dict[str, str] = ROUTES) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *a):
            log.debug(fmt, *a)

        def _send(self, status: int, body: dict) -> None:
            data = json.dumps(body, default=str).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            if not self.path.startswith("/v1/"):
                return self._send(404, {"error": "not found", "code": "not_found"})
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                return self._send(413, {"error": "body too large", "code": "bad_request"})
            try:
                args = json.loads(self.rfile.read(n) or b"{}")
            except json.JSONDecodeError:
                return self._send(400, {"error": "invalid JSON", "code": "bad_request"})
            header = self.headers.get("Authorization", "")
            token = header[7:] if header.startswith("Bearer ") else None
            self._send(*dispatch(service, tokens, self.path[4:], token, args, routes))

        def do_GET(self):
            if self.path == "/healthz":
                return self._send(200, {"ok": True})
            self._send(404, {"error": "not found", "code": "not_found"})

    return ThreadingHTTPServer((host, port), Handler)


def build_service(config: Path, data_root: Path, registry: Path, runs_dir: Path) -> ResearchService:
    cfg = load_config(config)
    holdout = cfg["segments"]["holdout_start"]
    load = lambda sym, start, end: rdata.load_m1(data_root, sym, start, end, holdout)  # noqa: E731
    return ResearchService(cfg, load, Registry(registry), runs_dir)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="atlas-api", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tokens", required=True, help="token hash file (owned by the engine OS user)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve")
    s.add_argument("--config", default=str(DEFAULT_CONFIG))
    s.add_argument("--data-root", default="data")
    s.add_argument("--registry", default="research/experiments.jsonl")
    s.add_argument("--runs", default="research/runs")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8741)

    t = sub.add_parser("token", help="issue a token; prints it once")
    t.add_argument("--name", required=True, help="e.g. risk-analyst/atlas-backtest")
    t.add_argument("--scopes", required=True, help="comma-separated: " + ", ".join(auth.SCOPES))

    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.cmd == "token":
        print(auth.issue(Path(args.tokens), args.name, [s.strip() for s in args.scopes.split(",") if s.strip()]))
        return
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        log.warning("binding to %s: make sure only the MCP host can reach it", args.host)
    service = build_service(Path(args.config), Path(args.data_root), Path(args.registry), Path(args.runs))
    server = make_server(service, TokenStore.load(Path(args.tokens)), args.host, args.port)
    log.info("atlas-api on %s:%d (holdout starts %s)", args.host, args.port, service.holdout_start.date())
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
