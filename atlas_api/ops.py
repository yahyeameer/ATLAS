"""The engine API's operations routes (PRD §6, §21, §23), and the simulated engine that serves them in H3.

    POST /v1/operations/status             ops:read             system_status
    POST /v1/operations/health             ops:read             health_state
    POST /v1/operations/reconciliation     ops:read             reconciliation_report
    POST /v1/operations/events             ops:read             engine event log (alerts, dashboard)
    POST /v1/operations/disable_trading    ops:disable_trading  the one write: stop new trades

This is the contract the real engine API implements in T4, on the trading host
with its own token file. There is no route to enable trading, flatten, clear a
kill or change any limit, and no scope that could authorize one: those are
operator actions outside Hermes (PRD §11 layer 4, §23).

Until T4 the routes are served by ``atlas-engine-sim serve`` over the
simulated engine in atlas_engine/ops/sim.py. Engine tokens live in their own
file with their own scope set, so no research-API token works here and no
engine token works on the research API.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Protocol

from . import auth
from .auth import Principal, TokenStore
from .service import BadRequest

log = logging.getLogger("atlas_api")

ENGINE_SCOPES = {
    "ops:read": "Engine status, health state, reconciliation report and event log",
    "ops:disable_trading": "Disable new trades. Cannot enable trading, flatten, or change any limit",
}

OPS_ROUTES = {
    "operations/status": "system_status",
    "operations/health": "health_state",
    "operations/reconciliation": "reconciliation_report",
    "operations/events": "events",
    "operations/disable_trading": "disable_trading",
}

MIN_REASON, MAX_REASON = 10, 500


class EngineUnavailable(ConnectionError):
    """The engine (or its simulated stand-in) cannot be reached (HTTP 503)."""


class EngineOps(Protocol):
    """What the operations routes need from the engine. atlas_engine.ops.sim.SimulatedEngine implements it."""

    source: str

    def status(self) -> dict: ...
    def health(self, symbol: str | None = None) -> dict: ...
    def reconciliation(self) -> dict: ...
    def events(self, since: int = 0, limit: int = 50) -> dict: ...
    def disable_trading(self, by: str, reason: str) -> dict: ...


class OpsService:
    """Scope checks and argument validation in front of the engine."""

    def __init__(self, engine: EngineOps):
        self.engine = engine

    def _call(self, fn, *a, **kw) -> dict:
        try:
            return fn(*a, **kw)
        except FileNotFoundError as e:
            raise EngineUnavailable(str(e)) from None

    def system_status(self, p: Principal) -> dict:
        p.require("ops:read")
        return self._call(self.engine.status)

    def health_state(self, p: Principal, symbol: str | None = None) -> dict:
        p.require("ops:read")
        if symbol is not None and not isinstance(symbol, str):
            raise BadRequest("symbol must be a string")
        try:
            return self._call(self.engine.health, symbol.upper() if symbol else None)
        except KeyError as e:
            raise BadRequest(str(e.args[0])) from None

    def reconciliation_report(self, p: Principal) -> dict:
        p.require("ops:read")
        return self._call(self.engine.reconciliation)

    def events(self, p: Principal, since: int = 0, limit: int = 50) -> dict:
        p.require("ops:read")
        if not isinstance(since, int) or since < 0 or not isinstance(limit, int) or not 1 <= limit <= 200:
            raise BadRequest("since must be an int >= 0 and limit an int in 1..200")
        return self._call(self.engine.events, since, limit)

    def disable_trading(self, p: Principal, reason: str) -> dict:
        p.require("ops:disable_trading")
        if not isinstance(reason, str) or not MIN_REASON <= len(reason.strip()) <= MAX_REASON:
            raise BadRequest(f"reason must be {MIN_REASON}-{MAX_REASON} characters saying what you saw")
        out = self._call(self.engine.disable_trading, p.name, reason.strip())
        log.warning("trading disabled by %s: %s", p.name, reason.strip())
        return out


# --------------------------------------------------------------------------- atlas-engine-sim CLI


def _params(pairs: list[str]) -> dict:
    out = {}
    for pair in pairs:
        k, _, v = pair.partition("=")
        if not _:
            raise SystemExit(f"expected key=value, got {pair!r}")
        try:
            out[k] = json.loads(v)
        except ValueError:
            out[k] = v
    return out


def main(argv: list[str] | None = None) -> None:
    from atlas_engine.ops.sim import FAULTS, SimRefused, SimulatedEngine

    from .http import make_server

    ap = argparse.ArgumentParser(prog="atlas-engine-sim", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--state", default=str(Path.home() / ".atlas" / "engine-sim.json"), help="simulated engine state")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="serve the operations routes over the simulated engine")
    s.add_argument("--tokens", required=True, help="engine token hash file")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8742)

    t = sub.add_parser("token", help="issue an engine token; prints it once")
    t.add_argument("--tokens", required=True)
    t.add_argument("--name", required=True, help="e.g. operations-monitor/atlas-operations")
    t.add_argument("--scopes", required=True, help="comma-separated: " + ", ".join(ENGINE_SCOPES))

    i = sub.add_parser("init", help="(re)start the simulated engine healthy, trading enabled")
    i.add_argument("--mode", default="paper", choices=["paper", "evaluation", "funded"])
    sub.add_parser("show", help="print status, health and reconciliation")
    f = sub.add_parser("inject", help="inject a fault: " + ", ".join(FAULTS))
    f.add_argument("fault", choices=sorted(FAULTS))
    f.add_argument("params", nargs="*", help="key=value overrides, e.g. symbol=GBPUSD seconds=120")
    c = sub.add_parser("clear", help="clear one fault, or all")
    c.add_argument("fault", nargs="?")
    e = sub.add_parser("enable", help="operator: re-enable new trades (refused while HALT or KILL)")
    e.add_argument("--operator", required=True)
    e.add_argument("--reason", required=True)
    k = sub.add_parser("kill", help="operator: manual kill (flatten and disable)")
    k.add_argument("--operator", required=True)
    k.add_argument("--reason", required=True)

    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    engine = SimulatedEngine(Path(args.state))
    if args.cmd == "token":
        print(auth.issue(Path(args.tokens), args.name, [x.strip() for x in args.scopes.split(",") if x.strip()],
                         known=ENGINE_SCOPES))
    elif args.cmd == "serve":
        if not Path(args.state).exists():
            engine.init()
        server = make_server(OpsService(engine), TokenStore.load(Path(args.tokens), ENGINE_SCOPES),
                             args.host, args.port, routes=OPS_ROUTES)
        log.info("simulated engine operations API on %s:%d (state %s)", args.host, args.port, args.state)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            sys.exit(0)
    elif args.cmd == "init":
        engine.init(mode=args.mode)
        print(json.dumps(engine.status(), indent=2))
    elif args.cmd == "show":
        print(json.dumps({"status": engine.status(), "health": engine.health(),
                          "reconciliation": engine.reconciliation()}, indent=2))
    elif args.cmd == "inject":
        print(json.dumps(engine.inject(args.fault, **_params(args.params))))
    elif args.cmd == "clear":
        print(json.dumps(engine.clear(args.fault)))
    elif args.cmd in ("enable", "kill"):
        try:
            out = engine.enable(args.operator, args.reason) if args.cmd == "enable" else engine.kill(args.operator, args.reason)
        except SimRefused as err:
            raise SystemExit(str(err)) from None
        print(json.dumps(out or engine.status()))


if __name__ == "__main__":
    main()
