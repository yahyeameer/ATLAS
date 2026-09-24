"""``atlas-engine``: run the trading engine and its operations API on the engine host (T4).

    atlas-engine run --config config --state /srv/atlas/engine --tokens engine-tokens.yaml \\
                     --operator-key /srv/atlas/engine/operator.key
    atlas-engine token --tokens engine-tokens.yaml --name operations-monitor/atlas-operations \\
                       --scopes ops:read,ops:disable_trading
    atlas-engine operator keygen --key /srv/atlas/engine/operator.key
    atlas-engine operator enable_trading --key ... --inbox /srv/atlas/engine/operator-inbox \\
                                         --operator yahye --reason "reviewed the incident, all clear"
    atlas-engine show --state /srv/atlas/engine

The API serves the same five operations routes as ``atlas-engine-sim`` (H3),
so the Hermes side (atlas-operations, cron checks, dashboard) works unchanged
when it is pointed at the real engine. Operator commands are signed files in
the engine's inbox, never API calls.

``--broker mt5`` needs the ``MetaTrader5`` package and a logged-in terminal
on Windows. ``--broker fake`` runs the engine on the in-process fake terminal
for drills: no network, no broker, no orders anywhere.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import threading
import uuid
from pathlib import Path

from atlas_api import auth
from atlas_api.auth import TokenStore
from atlas_api.ops import ENGINE_SCOPES, OPS_ROUTES, OpsService

log = logging.getLogger("atlas_engine")


def _watchdog_reader(path: str | None):
    if not path:
        return None

    def read() -> str | None:
        p = Path(path)
        if not p.exists():
            return None
        # The EA writes UTF-16 (MQL5 FILE_TXT default) or ANSI; accept both.
        raw = p.read_bytes()
        text = raw.decode("utf-16") if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else raw.decode("utf-8", "replace")
        return text.strip().splitlines()[0] if text.strip() else None

    return read


def build_engine(args):
    from atlas_engine.adapters.mt5 import MT5Adapter, ServerClock
    from atlas_engine.alerts import AlertOutbox
    from atlas_engine.config import load_engine_config
    from atlas_engine.execution import load_execution_settings
    from atlas_engine.journal import Journal
    from atlas_engine.operator import load_key
    from atlas_engine.runtime import TradingEngine
    from atlas_engine.strategies import load_strategies

    cfg = load_engine_config(args.config, require_read_only=not args.allow_writable_config)
    settings = load_execution_settings(args.config)
    clock = ServerClock(settings.server_tz, settings.server_offset_hours)
    if args.broker == "mt5":
        try:
            import MetaTrader5 as mt5  # noqa: N813 - Windows engine host only
        except ImportError:
            raise SystemExit("the MetaTrader5 package is not installed; it runs only on Windows next to a terminal")
        label = "mt5"
    else:
        from atlas_engine.adapters.mt5.fake import FakeMT5
        from atlas_engine.runtime import utc_now
        mt5 = FakeMT5(utc_now, clock, symbols=cfg.symbols, live_quotes=True)
        label = "fake-mt5"
    state = Path(args.state)
    adapter = MT5Adapter(mt5, clock, settings.commission_per_lot)
    journal = Journal(state / "journal.db", f"run-{uuid.uuid4().hex[:12]}")
    key = load_key(args.operator_key) if args.operator_key else None
    if key is None:
        log.warning("no --operator-key: operator commands will be refused, so trading can't be enabled")
    watchdog = _watchdog_reader(settings.watchdog_heartbeat_file)
    if label == "fake-mt5" and watchdog is None:
        import time

        def watchdog():  # the fake terminal has no EA; stand in for its heartbeat in drills
            return f"ATLAS-WD 1 {int(time.time())} 0 OK"
    return TradingEngine(cfg, settings, adapter, journal, state, sources=load_strategies(args.config),
                         alerts=AlertOutbox(state / "alerts.jsonl"), operator_key=key,
                         watchdog=watchdog, broker_label=label)


def main(argv: list[str] | None = None) -> None:
    from atlas_engine.operator import ACTIONS, keygen, load_key, sign

    ap = argparse.ArgumentParser(prog="atlas-engine", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="run the engine loop and serve the operations API")
    r.add_argument("--config", default="config")
    r.add_argument("--state", required=True, help="engine state directory (journal, inbox, alerts)")
    r.add_argument("--tokens", required=True, help="engine token hash file")
    r.add_argument("--operator-key", help="operator HMAC key file (mode 600)")
    r.add_argument("--broker", choices=["mt5", "fake"], default="mt5")
    r.add_argument("--host", default="127.0.0.1")
    r.add_argument("--port", type=int, default=8742)
    r.add_argument("--poll", type=float, default=1.0, help="seconds between engine steps")
    r.add_argument("--allow-writable-config", action="store_true",
                   help="skip the read-only config check (development only; PRD §12 wants it on)")

    t = sub.add_parser("token", help="issue an engine API token; prints it once")
    t.add_argument("--tokens", required=True)
    t.add_argument("--name", required=True)
    t.add_argument("--scopes", required=True, help="comma-separated: " + ", ".join(ENGINE_SCOPES))

    o = sub.add_parser("operator", help="operator key and signed commands")
    o.add_argument("action", choices=["keygen", *ACTIONS])
    o.add_argument("--key", required=True, help="operator key file")
    o.add_argument("--inbox", help="the engine's operator-inbox directory")
    o.add_argument("--operator", help="who is signing")
    o.add_argument("--reason", help="why (at least 10 characters)")

    s = sub.add_parser("show", help="print the engine's saved state (stopped or running)")
    s.add_argument("--state", required=True)

    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.cmd == "token":
        print(auth.issue(Path(args.tokens), args.name, [x.strip() for x in args.scopes.split(",") if x.strip()],
                         known=ENGINE_SCOPES))
    elif args.cmd == "operator":
        if args.action == "keygen":
            keygen(args.key)
            print(f"wrote {args.key}; keep it on the engine host, readable only by the operator and the engine user")
            return
        if not (args.inbox and args.operator and args.reason):
            raise SystemExit("--inbox, --operator and --reason are required to sign a command")
        cmd = sign(load_key(args.key), args.action, args.operator, args.reason)
        inbox = Path(args.inbox)
        tmp = inbox / f".{cmd['nonce']}.tmp"
        tmp.write_text(json.dumps(cmd))
        tmp.replace(inbox / f"{cmd['nonce']}.json")  # the engine never sees a half-written file
        print(f"queued {args.action}; the engine applies it on its next step (see its events)")
    elif args.cmd == "show":
        from atlas_engine.journal import Journal
        st = Journal(Path(args.state) / "journal.db", "show").load_state("engine") or {}
        print(json.dumps({k: st.get(k) for k in ("trading", "kill", "last_state", "last_reasons", "risk", "book",
                                                 "requests")}, indent=2, default=str))
    elif args.cmd == "run":
        from .http import make_server
        engine = build_engine(args)
        server = make_server(OpsService(engine), TokenStore.load(Path(args.tokens), ENGINE_SCOPES),
                             args.host, args.port, routes=OPS_ROUTES)
        threading.Thread(target=server.serve_forever, name="ops-api", daemon=True).start()
        log.info("engine (%s) operations API on %s:%d, state %s", engine.source, args.host, args.port, args.state)
        try:
            engine.run_forever(args.poll)
        except KeyboardInterrupt:
            server.shutdown()
            sys.exit(0)


if __name__ == "__main__":
    main()
