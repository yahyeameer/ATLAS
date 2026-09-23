#!/usr/bin/env python3
"""H2 exit gate, end to end: Hermes workers call the ATLAS MCP servers against the research API.

Starts the research API on synthetic data (a short calendar with the holdout
from 2021-01-01), builds a throwaway Hermes home with deploy/hermes/bootstrap.py
(which issues one scoped token per profile and MCP server), puts four cards on
the atlas-research board and lets the Kanban dispatcher run them:

    strategy-researcher   runs a backtest, then asks for the holdout twice
    risk-analyst          Monte Carlo on that run (it is never offered run_backtest)
    performance-analyst   performance summary and MFE/MAE on that run
    market-researcher     market state, then bars reaching into the holdout

A scripted OpenAI-compatible model (no key needed) plays each worker, so this
proves the wiring, not model judgment: which MCP tools and skills each profile
is offered, that calls reach the API with that profile's own token, and that
the API refuses the holdout and out-of-scope calls.

    python tests/hermes/h2_gate.py --hermes /path/to/venv/bin/hermes

Run it with a Python that has ATLAS installed with the mcp extra (`pip install
-e '.[mcp]'`): the installer points the profiles' MCP servers at it. The unit
tests in tests/mcp/ are the fast part of the gate; this is the integration run.
Exit status 0 means every check passed.
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pandas as pd
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from h1_gate import REPO, ROSTER, SOUL_MARKER, Run, _last_tool_call_name, _task, _tool_call, serve  # noqa: E402

from atlas_api.auth import TokenStore  # noqa: E402
from atlas_api.http import dispatch, make_server  # noqa: E402
from atlas_api.service import ResearchService  # noqa: E402
from atlas_engine.market_data import synthetic  # noqa: E402
from atlas_mcp.scopes import api_of, token_env_var  # noqa: E402
from atlas_research.cli import DEFAULT_CONFIG, load_config  # noqa: E402
from atlas_research.registry import Registry  # noqa: E402

BOARD = "atlas-research"
HOLDOUT = pd.Timestamp("2021-01-01", tz="UTC")
DRILL = ["strategy-researcher", "risk-analyst", "performance-analyst", "market-researcher"]
INTO_HOLDOUT = {"start": "2020-12-01", "end": "2021-03-01"}


def mcp(server: str, tool: str) -> str:
    """Hermes' registered name for an MCP tool."""
    return f"mcp__{server.replace('-', '_')}__{tool}"


# --------------------------------------------------------------------------- research API


class SpyLoader:
    def __init__(self):
        self.data = {s: synthetic.random_walk_m1(s, "2019-01-01", "2021-01-01", seed=i, start_price=p)
                     for i, (s, p) in enumerate([("EURUSD", 1.15), ("GBPUSD", 1.30)])}
        self.calls: list[tuple[str, pd.Timestamp, pd.Timestamp]] = []

    def __call__(self, symbol, start, end):
        self.calls.append((symbol, pd.Timestamp(start), pd.Timestamp(end)))
        m1 = self.data[symbol]
        return m1.loc[(m1.index >= start) & (m1.index < end)]


class ReloadingTokens:
    """The token file is written by bootstrap after the API starts; read it on each call."""

    def __init__(self, path: Path):
        self.path = path

    def authenticate(self, token):
        return TokenStore.load(self.path).authenticate(token)


class Audit(logging.Handler):
    def __init__(self):
        super().__init__()
        self.ok: list[tuple[str, str]] = []  # (principal, route)
        self.refused: list[str] = []

    def emit(self, record):
        if record.msg == "%s %s ok":
            self.ok.append(record.args)
        elif record.levelno >= logging.WARNING:
            self.refused.append(record.getMessage())


def start_api(tmp: Path, tokens_path: Path):
    cfg = load_config(DEFAULT_CONFIG)
    cfg["segments"] = {"dev": ["2019-01-01", "2020-07-01"], "validation": ["2020-07-01", "2021-01-01"],
                       "holdout_start": "2021-01-01"}
    loader = SpyLoader()
    service = ResearchService(cfg, loader, Registry(tmp / "experiments.jsonl"), tmp / "runs")
    audit = Audit()
    log = logging.getLogger("atlas_api")
    log.setLevel(logging.INFO)
    log.addHandler(audit)
    httpd = make_server(service, ReloadingTokens(tokens_path), "127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, service, loader, audit


# --------------------------------------------------------------------------- scripted workers


def tool_payload(text: str) -> tuple[bool, dict | str]:
    """Unwrap a Hermes MCP tool result: (True, parsed result) or (False, error text)."""
    body = text[text.find("{"):text.rfind("}") + 1] if "{" in text else ""
    try:
        outer = json.loads(body)
    except ValueError:
        return False, text
    if "error" in outer:
        return False, str(outer["error"])
    result = outer.get("result", outer)
    try:
        return True, json.loads(result) if isinstance(result, str) else result
    except ValueError:
        return True, result


class DrillModel:
    """Plays each worker: a fixed sequence of tool calls per profile, then kanban_complete."""

    def __init__(self):
        self.lock = threading.Lock()
        self.run_id: str | None = None
        self.offered: dict[str, list[str]] = {}
        self.systems: dict[str, str] = {}
        self.results: list[dict] = []  # {profile, tool, args, text}

    def script(self, profile: str) -> list[tuple[str, dict]]:
        bt, mk = "atlas-backtest", "atlas-market"
        return {
            "strategy-researcher": [
                (mcp(bt, "run_backtest"), {"strategy": "trend_pullback", "window": "validation"}),
                (mcp(bt, "run_backtest"), {"strategy": "trend_pullback", "window": "holdout"}),
                (mcp(bt, "run_backtest"), {"strategy": "trend_pullback", "window": INTO_HOLDOUT}),
            ],
            "risk-analyst": [
                (mcp(bt, "monte_carlo"), {"run_id": self.run_id, "sims": 1000}),
            ],
            "performance-analyst": [
                (mcp("atlas-performance", "performance_summary"), {"run_id": self.run_id, "by": "session"}),
                (mcp("atlas-journal", "mfe_mae"), {"run_id": self.run_id}),
            ],
            "market-researcher": [
                (mcp(mk, "collect_market_state"), {"symbols": ["EURUSD", "GBPUSD"]}),
                (mcp(mk, "get_bars"), {"symbol": "EURUSD", "timeframe": "H1", "window": INTO_HOLDOUT}),
            ],
        }.get(profile, [])

    def respond(self, req: dict) -> dict:
        messages = req.get("messages") or []
        system = next((m.get("content") or "" for m in messages if m.get("role") == "system"), "")
        tools = sorted(t["function"]["name"] for t in req.get("tools") or [])
        if "kanban_complete" not in tools:
            return {"content": "ok"}
        m = SOUL_MARKER.search(system)
        profile = m.group(1) if m else "unknown"
        with self.lock:
            self.offered.setdefault(profile, tools)
            self.systems.setdefault(profile, system)
        last = messages[-1]
        if last.get("role") != "tool":
            return _tool_call("kanban_show", {})
        called = _last_tool_call_name(messages)
        if called == "kanban_complete":
            return {"content": "Done."}
        done = [c for msg in messages for c in msg.get("tool_calls") or []
                if not c["function"]["name"].startswith("kanban_")]
        if called and called.startswith("mcp__"):
            ok, value = tool_payload(str(last.get("content")))
            with self.lock:
                self.results.append({"profile": profile, "tool": called, "ok": ok, "value": value})
                if called.endswith("__run_backtest") and ok and self.run_id is None:
                    self.run_id = value["run_id"]
        steps = self.script(profile)
        if len(done) < len(steps):
            name, args = steps[len(done)]
            return _tool_call(name, args)
        return _tool_call("kanban_complete", {"summary": f"{profile}: H2 drill done",
                                              "metadata": {"drill": "h2-gate", "experiment_id": self.run_id}})


# --------------------------------------------------------------------------- driver


def build_home(hermes: str, home: Path, base_url: str, tokens: Path, api_url: str) -> None:
    models = home.parent / "models-offline.yaml"
    tier = {"provider": "custom", "model": "atlas-fake", "base_url": base_url}
    models.write_text(yaml.safe_dump({"tiers": {t: tier for t in ("frontier", "mid", "cheap")}}))
    cmd = [sys.executable, str(REPO / "deploy" / "hermes" / "bootstrap.py"), "--hermes", hermes,
           "--hermes-home", str(home), "--models", str(models), "--api-tokens", str(tokens), "--api-url", api_url,
           "--engine-tokens", str(tokens.with_name("engine-tokens.yaml"))]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"bootstrap failed:\n{proc.stdout}{proc.stderr}")


def drive(run: Run, model: DrillModel, timeout_s: int) -> dict[str, str]:
    def create(profile, title, parents=()):
        args = ["-p", "atlas-orchestrator", "kanban", "--board", BOARD, "create", title, "--assignee", profile,
                "--tenant", "paper", "--body", "H2 drill card: call the scripted ATLAS tools, then complete."]
        for p in parents:
            args += ["--parent", p]
        return json.loads(run(*args, "--json"))["id"]

    ids = {"strategy-researcher": create("strategy-researcher", "H2 drill: backtest through atlas-backtest")}
    ids["market-researcher"] = create("market-researcher", "H2 drill: market state through atlas-market")
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        run("-p", "atlas-orchestrator", "kanban", "--board", BOARD, "dispatch")
        if model.run_id and "risk-analyst" not in ids:
            # Children are created once the run exists, like a real handoff.
            ids["risk-analyst"] = create("risk-analyst", "H2 drill: risk read-back", [ids["strategy-researcher"]])
            ids["performance-analyst"] = create("performance-analyst", "H2 drill: journal and performance",
                                                [ids["strategy-researcher"]])
        states = {p: _task(run.json("kanban", "--board", BOARD, "show", i))["status"] for p, i in ids.items()}
        if len(ids) == 4 and all(s in ("done", "blocked") for s in states.values()):
            return states
        time.sleep(2)
    raise RuntimeError(f"drill cards did not finish within {timeout_s}s")


# --------------------------------------------------------------------------- checks


def check(home: Path, tokens_path: Path, api, service, loader: SpyLoader, audit: Audit, model: DrillModel,
          states: dict) -> list[tuple[str, bool, str]]:
    results = []

    def ok(name, cond, detail=""):
        results.append((name, bool(cond), detail))

    def res(profile, tool):
        return [r for r in model.results if r["profile"] == profile and r["tool"].endswith("__" + tool)]

    def good(profile, tool, key):
        return any(r["ok"] and key in json.dumps(r["value"]) for r in res(profile, tool))

    ok("every drill card finished", all(s == "done" for s in states.values()),
       ", ".join(f"{p}={s}" for p, s in states.items()))

    # Tokens: one per (profile, server), hashes only in the engine's file.
    token_names = {t["name"] for t in yaml.safe_load(tokens_path.read_text())["tokens"]}
    want = {f"{n}/{s}" for n, e in ROSTER.items() for s in e.get("mcp") or {} if api_of(s) == "research"}
    ok("installer issued one research token per profile and research MCP server", token_names == want,
       f"{len(token_names)} tokens")
    leaked = [n for n in ROSTER for k, v in _env(home, n).items() if k.startswith("ATLAS_TOKEN_") and v in tokens_path.read_text()]
    ok("token file holds hashes only", not leaked, ", ".join(leaked))

    # Hermes layer: tools and skills per profile.
    for p in DRILL:
        offered = {t for t in model.offered.get(p, []) if t.startswith("mcp__")}
        expected = {mcp(s, t) for s, tools in (ROSTER[p].get("mcp") or {}).items() for t in tools}
        ok(f"{p} was offered exactly its roster MCP tools", offered == expected,
           f"extra: {sorted(offered - expected)} missing: {sorted(expected - offered)}" if offered != expected
           else ", ".join(sorted(t.split("__", 1)[1] for t in offered)))
        system = model.systems.get(p, "")
        pinned = ROSTER[p].get("skills") or []
        others = {s for e in ROSTER.values() for s in e.get("skills") or []} - set(pinned)
        ok(f"{p} sees its pinned skills and no others",
           all(s in system for s in pinned) and not any(s in system for s in others),
           ", ".join(pinned))

    # Work flowed through MCP to the API under each profile's own token.
    bt = res("strategy-researcher", "run_backtest")
    ok("strategy-researcher ran a backtest through MCP", model.run_id is not None and bt and bt[0]["ok"],
       model.run_id or (str(bt[0]["value"])[:120] if bt else "no result"))
    entries = service.registry.entries("trend_pullback")
    ok("the run is recorded as an experiment by that profile's token",
       [e["requested_by"] for e in entries] == ["strategy-researcher/atlas-backtest"]
       and entries[0]["experiment_id"] == model.run_id,
       ", ".join(e["requested_by"] for e in entries))
    ok("risk-analyst ran Monte Carlo on that run", good("risk-analyst", "monte_carlo", "dd_p95_pct"))
    ok("performance-analyst read performance and MFE/MAE",
       good("performance-analyst", "performance_summary", "by_session")
       and good("performance-analyst", "mfe_mae", "losers_reaching_1r"))
    ok("market-researcher read market state", good("market-researcher", "collect_market_state", "atr_m15_pips"))

    # Refusals.
    holdout = bt[1:] + res("market-researcher", "get_bars")
    ok("every holdout request was refused by the API",
       len(holdout) == 3 and all(not r["ok"] and "holdout_refused" in r["value"] for r in holdout),
       " | ".join(str(r["value"])[:80] for r in holdout))
    ok("the loader never read a holdout row", loader.calls and max(c[2] for c in loader.calls) <= HOLDOUT,
       f"{len(loader.calls)} loads, latest end {max(c[2] for c in loader.calls) if loader.calls else '-'}")
    tok = _env(home, "risk-analyst").get(token_env_var("atlas-backtest"))
    status, body = dispatch(service, TokenStore.load(tokens_path), "backtest/run", tok,
                            {"strategy": "trend_pullback", "window": "dev"})
    ok("the API refuses run_backtest with risk-analyst's own token", status == 403 and body["code"] == "forbidden",
       f"{status} {body.get('code')}")
    runners = {who for who, route in audit.ok if route == "backtest/run"}
    ok("only strategy-researcher's token started runs", runners == {"strategy-researcher/atlas-backtest"},
       ", ".join(sorted(runners)))
    return results


def _env(home: Path, profile: str) -> dict:
    path = home / "profiles" / profile / ".env"
    out = {}
    for line in (path.read_text().splitlines() if path.exists() else []):
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hermes", default="hermes", help="hermes executable at the pinned version")
    ap.add_argument("--timeout", type=int, default=300, help="seconds to wait for the drill cards")
    ap.add_argument("--keep", action="store_true", help="keep the temporary Hermes home for inspection")
    ap.add_argument("--report", help="write the results as JSON to this path")
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="atlas-h2-gate-"))
    home, tokens = tmp / "hermes-home", tmp / "engine" / "api-tokens.yaml"
    api, service, loader, audit = start_api(tmp, tokens)
    model = DrillModel()
    server = serve(model)
    try:
        build_home(args.hermes, home, f"http://127.0.0.1:{server.server_address[1]}/v1", tokens,
                   f"http://127.0.0.1:{api.server_address[1]}")
        run = Run(args.hermes, home, {"HERMES_MANAGED_DIR": str(REPO / "deploy" / "hermes" / "managed"),
                                      "OPENAI_API_KEY": "sk-atlas-offline"})
        for p in ROSTER:
            run("-p", p, "config", "set", "model.api_key", "sk-atlas-offline")
        states = drive(run, model, args.timeout)
        results = check(home, tokens, api, service, loader, audit, model, states)
    finally:
        server.shutdown()
        api.shutdown()
        if not args.keep:
            subprocess.run(["rm", "-rf", str(tmp)])
        else:
            print(f"kept {tmp}")

    width = max(len(n) for n, _, _ in results)
    for name, passed, detail in results:
        print(f"{'PASS' if passed else 'FAIL'}  {name.ljust(width)}  {detail}")
    passed = all(p for _, p, _ in results)
    print(f"\nH2 exit gate (Hermes integration): {'PASSED' if passed else 'FAILED'}")
    if args.report:
        Path(args.report).write_text(json.dumps(
            {"passed": passed, "checks": [{"name": n, "passed": p, "detail": d} for n, p, d in results],
             "tool_results": model.results}, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
