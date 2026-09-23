#!/usr/bin/env python3
"""H3 exit gate, end to end: simulated engine incidents flow through Hermes cron, Kanban, MCP and the plugin.

Starts the engine operations API over the simulated engine (atlas-engine-sim,
the stand-in until the real engine exists in T4), builds a throwaway Hermes
home with deploy/hermes/bootstrap.py (profiles, scoped tokens, the atlas
plugin, ops.yaml and the cron jobs, delivering locally), then drives:

    baseline      health check on a NORMAL engine: silent
    HALT          MT5 disconnect -> HALT alert and one incident card on atlas-ops;
                  operations-monitor (incident-triage) reads health, status and
                  reconciliation, disables new trades, blocks for the operator
    recovery      operator clears the fault and re-enables -> RECOVERED alert
    KILL          daily loss past the hard limit -> the engine flattens and disables
                  itself; the worker sees that and does not disable again
    engine down   ops API stopped -> UNREACHABLE alert and card; the worker blocks
    relay         the alert-relay job delivers each non-health alert once, with budget alerts
    other jobs    hourly reconciliation report, a weekly card created once per week,
                  execution-engineer offered read tools only, the dashboard tab's API

A scripted OpenAI-compatible model (no key needed) plays the workers, so this
proves the wiring, not model judgment.

    python tests/hermes/h3_gate.py --hermes /path/to/hermes-venv/bin/hermes

Run it with a Python that has ATLAS installed with the mcp extra (the MCP
servers run under it). Hermes' own interpreter need not have ATLAS: the plugin
and cron scripts find this checkout through the path the installer records.
Exit status 0 means every check passed.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from h1_gate import REPO, ROSTER, SOUL_MARKER, Run, _last_tool_call_name, _task, _tool_call, serve  # noqa: E402
from h2_gate import _env, mcp, tool_payload  # noqa: E402

from atlas_api.auth import TokenStore  # noqa: E402
from atlas_api.http import dispatch, make_server  # noqa: E402
from atlas_api.ops import ENGINE_SCOPES, OPS_ROUTES, OpsService  # noqa: E402
from atlas_engine.ops.sim import SimulatedEngine  # noqa: E402

OPS = "atlas-ops"
OPS_SERVER = "atlas-operations"


class ReloadingEngineTokens:
    """bootstrap writes the engine token file after the API starts; read it on each call."""

    def __init__(self, path: Path):
        self.path = path

    def authenticate(self, token):
        return TokenStore.load(self.path, ENGINE_SCOPES).authenticate(token)


# --------------------------------------------------------------------------- scripted workers


class OpsModel:
    """Plays operations-monitor by the incident-triage procedure, and execution-engineer as a reader."""

    def __init__(self):
        self.lock = threading.Lock()
        self.offered: dict[str, list[str]] = {}
        self.systems: dict[str, str] = {}
        self.results: list[dict] = []  # {profile, task, tool, ok, value}
        self.finish: list[dict] = []  # {profile, tool, args}

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
        if called in ("kanban_complete", "kanban_block"):
            return {"content": "Done."}
        seen: dict[str, tuple[bool, object]] = {}
        calls = [c for msg in messages for c in msg.get("tool_calls") or []]
        results = {msg.get("tool_call_id"): msg.get("content") for msg in messages if msg.get("role") == "tool"}
        for c in calls:
            name = c["function"]["name"]
            if name.startswith("mcp__") and c["id"] in results:
                seen[name.rsplit("__", 1)[1]] = tool_payload(str(results[c["id"]]))
        if called and called.startswith("mcp__"):
            ok, value = tool_payload(str(last.get("content")))
            with self.lock:
                self.results.append({"profile": profile, "tool": called.rsplit("__", 1)[1], "ok": ok, "value": value})
        step = self.next_step(profile, seen)
        with self.lock:
            if step[0] in ("kanban_complete", "kanban_block"):
                self.finish.append({"profile": profile, "tool": step[0], "args": step[1]})
        return _tool_call(*step)

    @staticmethod
    def next_step(profile: str, seen: dict) -> tuple[str, dict]:
        op = lambda tool: mcp(OPS_SERVER, tool)  # noqa: E731
        if profile == "execution-engineer":
            if "health_state" not in seen:
                return op("health_state"), {}
            return "kanban_complete", {"summary": "execution-engineer: read engine health (H3 drill)"}
        for tool in ("health_state", "system_status", "reconciliation_report"):
            if tool not in seen:
                return op(tool), {}
            if not seen[tool][0]:
                return "kanban_block", {"kind": "capability", "reason": (
                    f"Engine unreachable ({tool} failed: {str(seen[tool][1])[:120]}). Operator: check the "
                    "trading host and the network path from the agent host.")}
        health, status = seen["health_state"][1], seen["system_status"][1]
        state, reasons = health["state"], ", ".join(health["reasons"])
        enabled = status["trading"]["enabled"]
        if state in ("HALT", "KILL") and enabled and "disable_trading" not in seen:
            return op("disable_trading"), {"reason": f"{state}: {reasons}; engine still reported new trades "
                                                     "enabled (H3 drill)."}
        disabled = "disable_trading" in seen
        if state == "KILL":
            return "kanban_block", {"kind": "needs_input", "reason": (
                f"KILL ({reasons}): engine flattened and disabled new trades itself. Operator: review, then "
                "re-enable on the next server day at the earliest.")}
        if state == "HALT":
            return "kanban_block", {"kind": "needs_input", "reason": (
                f"HALT ({reasons}). New trades disabled{' by me' if disabled else ''}. Operator: check the VPS "
                "and MT5 terminal; re-enable once the engine is back to NORMAL.")}
        meta = {"incident_state": state, "reasons": health["reasons"], "disabled_trading": disabled, "artifacts": []}
        return "kanban_complete", {"summary": f"Engine is {state}; nothing left to do.", "metadata": meta}


# --------------------------------------------------------------------------- driver


class Drill:
    def __init__(self, hermes: str, tmp: Path, model: OpsModel, base_url: str):
        self.tmp, self.model = tmp, model
        self.home = tmp / "hermes-home"
        self.ops = self.home / "atlas"
        self.state = tmp / "engine" / "sim.json"
        self.engine_tokens = tmp / "engine" / "engine-tokens.yaml"
        self.registry = tmp / "experiments.jsonl"
        self.engine = SimulatedEngine(self.state)
        self.engine.init("paper")
        self.api = make_server(OpsService(SimulatedEngine(self.state)), ReloadingEngineTokens(self.engine_tokens),
                               "127.0.0.1", 0, routes=OPS_ROUTES)
        threading.Thread(target=self.api.serve_forever, daemon=True).start()
        self.engine_url = f"http://127.0.0.1:{self.api.server_address[1]}"
        self.hermes = hermes
        self.base_url = base_url
        self.run: Run | None = None
        self.results: list[tuple[str, bool, str]] = []

    def ok(self, name, cond, detail=""):
        self.results.append((name, bool(cond), str(detail)[:300]))

    # ------------------------------------------------------------- setup

    def build(self) -> None:
        month = time.strftime("%Y-%m", time.gmtime())
        self.registry.write_text("".join(json.dumps({"strategy": s, "created_at": f"{month}-01T00:00:00+00:00"}) + "\n"
                                         for s in ("trend_pullback", "trend_pullback", "breakout")))
        models = self.tmp / "models-offline.yaml"
        tier = {"provider": "custom", "model": "atlas-fake", "base_url": self.base_url}
        models.write_text(yaml.safe_dump({"tiers": {t: tier for t in ("frontier", "mid", "cheap")}}))
        cmd = [sys.executable, str(REPO / "deploy" / "hermes" / "bootstrap.py"), "--hermes", self.hermes,
               "--hermes-home", str(self.home), "--models", str(models),
               "--api-tokens", str(self.tmp / "engine" / "api-tokens.yaml"), "--api-url", "http://127.0.0.1:9",
               "--engine-tokens", str(self.engine_tokens), "--engine-url", self.engine_url,
               "--research-registry", str(self.registry), "--enable-gated", "--cron-deliver", "local"]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(f"bootstrap failed:\n{proc.stdout}{proc.stderr}")
        # Tiny atlas-ops budget so the drill's own model calls cross both alert thresholds.
        cfg = yaml.safe_load((self.ops / "ops.yaml").read_text())
        cfg["budgets"]["atlas-ops"] = 10
        (self.ops / "ops.yaml").write_text(yaml.safe_dump(cfg))
        self.run = Run(self.hermes, self.home, {"HERMES_MANAGED_DIR": str(REPO / "deploy" / "hermes" / "managed"),
                                                "OPENAI_API_KEY": "sk-atlas-offline"})
        for p in ROSTER:
            self.run("-p", p, "config", "set", "model.api_key", "sk-atlas-offline")

    # ------------------------------------------------------------- helpers

    def jsonl(self, *rel: str) -> list[dict]:
        path = self.ops.joinpath(*rel)
        return [json.loads(x) for x in path.read_text().splitlines() if x.strip()] if path.exists() else []

    def cron_job(self, profile: str, name: str) -> dict:
        jobs = json.loads((self.home / "profiles" / profile / "cron" / "jobs.json").read_text())["jobs"]
        return next(j for j in jobs if j["name"] == name)

    def cron(self, name: str, profile: str = "operations-monitor") -> str:
        """Run a cron job now and return what it would deliver ('' when silent)."""
        job = self.cron_job(profile, name)
        out_dir = self.home / "profiles" / profile / "cron" / "output" / job["id"]
        before = set(out_dir.glob("*")) if out_dir.exists() else set()
        self.run("-p", profile, "cron", "run", job["id"])
        new = sorted(set(out_dir.glob("*")) - before) if out_dir.exists() else []
        if not new:
            raise RuntimeError(f"cron run {name} wrote no output file")
        text = new[-1].read_text()
        # Hermes writes a header; a silent run has no body ("Status: silent") and is not delivered.
        return text.split("\n---\n", 1)[1].strip() if "\n---\n" in text else ""

    def cards(self, board: str) -> list[dict]:
        out = json.loads(self.run("-p", "atlas-orchestrator", "kanban", "--board", board, "list", "--json"))
        return out.get("tasks", out) if isinstance(out, dict) else out

    def dispatch_until_settled(self, board: str, ids: list[str], timeout_s: int) -> dict[str, str]:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            self.run("-p", "atlas-orchestrator", "kanban", "--board", board, "dispatch")
            states = {i: _task(self.run.json("kanban", "--board", board, "show", i))["status"] for i in ids}
            if all(s in ("done", "blocked") for s in states.values()):
                return states
            time.sleep(2)
        raise RuntimeError(f"cards {ids} did not settle within {timeout_s}s")

    def runs(self, task_id: str) -> int:
        return len(self.run.json("kanban", "--board", OPS, "show", task_id).get("runs") or [])

    def incident_cards(self) -> list[dict]:
        return [c for c in self.cards(OPS) if str(c.get("title", "")).startswith("Incident: engine")]

    def alerts_since(self, n: int) -> list[dict]:
        return self.jsonl("alerts", "alerts.jsonl")[n:]

    # ------------------------------------------------------------- scenarios

    def scenarios(self, timeout_s: int) -> None:
        ok = self.ok
        doctor = self.run("-p", "operations-monitor", "plugins", "doctor", "atlas", check=False)
        ok("the atlas plugin passes hermes plugins doctor with its 5 hooks",
           "OK: runtime discovery" in doctor and "5 hook(s)" in doctor and "WARN" not in doctor,
           " / ".join(doctor.strip().splitlines()[-2:]))

        # Baseline.
        out = self.cron("atlas-health-check")
        ok("baseline: health check on a NORMAL engine is silent", out == "", out[:120])
        state_file = self.ops / "state" / "health-check.json"
        ok("baseline: the health check reached the engine with its own token",
           state_file.exists() and json.loads(state_file.read_text()).get("state") == "NORMAL",
           state_file.read_text() if state_file.exists() else "no state file")
        out = self.cron("atlas-reconciliation-report")
        reports = list((self.ops / "reports" / "reconciliation").rglob("*.json"))
        ok("reconciliation report written each run, silent when clean",
           len(reports) == 1 and json.loads(reports[0].read_text())["status"] == "clean"
           and out == "", f"{len(reports)} report(s)")

        # HALT.
        n = len(self.jsonl("alerts", "alerts.jsonl"))
        self.engine.inject("mt5_disconnect", seconds=120)
        out = self.cron("atlas-health-check")
        again = self.cron("atlas-health-check")
        new = self.alerts_since(n)
        cards = self.incident_cards()
        ok("HALT: the health check alerts once, naming the reason and the card",
           len(new) == 1 and new[0]["severity"] == "critical" and "HALT: mt5_disconnected" in new[0]["text"]
           and "Incident card" in new[0]["text"] and out == new[0]["text"] and again == "",
           new[0]["text"] if new else out)
        ok("HALT: exactly one incident card, for operations-monitor", len(cards) == 1
           and cards[0].get("assignee") == "operations-monitor", [(c.get("title"), c.get("assignee")) for c in cards])
        halt_id = cards[0]["id"] if cards else None
        show = _task(self.run.json("kanban", "--board", OPS, "show", halt_id)) if halt_id else {}
        ok("HALT: the card carries the incident-triage skill and the paper tenant",
           "incident-triage" in json.dumps(show.get("skills") or show) and show.get("tenant") == "paper",
           {k: show.get(k) for k in ("skills", "tenant", "created_by")})
        states = self.dispatch_until_settled(OPS, [halt_id], timeout_s)
        s = self.engine.status()
        ok("HALT: operations-monitor disabled new trades through atlas-operations",
           not s["trading"]["enabled"] and s["trading"]["by"] == "operations-monitor/atlas-operations",
           s["trading"])
        ok("HALT: the worker blocked the card for the operator in one run", states[halt_id] == "blocked"
           and self.runs(halt_id) == 1, f"{states} runs={self.runs(halt_id)}")
        mine = [r["tool"] for r in self.model.results if r["profile"] == "operations-monitor"]
        ok("HALT: the worker read health, status and reconciliation before disabling",
           mine[:4] == ["health_state", "system_status", "reconciliation_report", "disable_trading"], mine)
        audit = [a for a in self.jsonl("audit", "agent_actions.jsonl") if a.get("profile") == "operations-monitor"]
        ok("HALT: every MCP call is in the audit log with board, card and outcome",
           [a["tool"] for a in audit] == mine[:4] and all(a["board"] == OPS and a["kanban_task"] == halt_id
                                                          and a["outcome"] == "ok" for a in audit)
           and audit[-1]["write"] is True, [(a["tool"], a.get("board"), a.get("kanban_task")) for a in audit])
        texts = [a["text"] for a in self.alerts_since(n)]
        ok("HALT: the disable and the blocked card raised alerts from the worker's hooks",
           any("DISABLED new trading" in t for t in texts) and any(halt_id in t and "blocked" in t.lower()
                                                                    for t in texts), texts[1:])

        relay = self.cron("atlas-alert-relay")
        relay_again = self.cron("atlas-alert-relay")
        ok("relay: delivers the worker's alerts once and not the health check's own",
           "DISABLED new trading" in relay and "Incident card" not in relay
           and relay_again == "", relay[:400])
        budget = [x for x in relay.splitlines() if "atlas-ops has used" in x]
        ok("relay: the atlas-ops token budget overrun is one critical alert",
           len(budget) == 1 and budget[0].startswith("[CRITICAL]"), budget)

        # Recovery: only the operator re-enables.
        n = len(self.jsonl("alerts", "alerts.jsonl"))
        self.engine.clear()
        self.engine.enable("yahye", "MT5 reconnected (H3 drill)")
        out = self.cron("atlas-health-check")
        new = self.alerts_since(n)
        ok("recovery: RECOVERED alert once the operator clears and re-enables",
           "RECOVERED" in out and "New trades: enabled" in out and new and new[-1]["severity"] == "info", out[:200])

        # KILL: the engine disables itself; the worker must not disable again.
        n_calls = len(self.model.results)
        self.engine.inject("daily_loss", frac=0.9)
        out = self.cron("atlas-health-check")
        kill = [c for c in self.incident_cards() if c["id"] != halt_id]
        ok("KILL: alert and a second incident card", "KILL: daily_loss_hard_limit" in out and len(kill) == 1,
           out[:200])
        kill_id = kill[0]["id"] if kill else None
        states = self.dispatch_until_settled(OPS, [kill_id], timeout_s)
        tools = [r["tool"] for r in self.model.results[n_calls:] if r["profile"] == "operations-monitor"]
        s = self.engine.status()
        ok("KILL: engine flattened and disabled itself; the worker did not call disable_trading",
           s["open_positions"] == 0 and s["trading"]["by"] == "engine" and "disable_trading" not in tools
           and states[kill_id] == "blocked" and self.runs(kill_id) == 1,
           f"{tools} by={s['trading']['by']} {states} runs={self.runs(kill_id)}")

        # Execution-engineer: reads only.
        eng = json.loads(self.run("-p", "atlas-orchestrator", "kanban", "--board", OPS, "create",
                                  "H3 drill: execution-engineer reads engine health", "--assignee",
                                  "execution-engineer", "--tenant", "paper", "--body", "Read health_state, then complete.",
                                  "--json"))["id"]
        self.dispatch_until_settled(OPS, [eng], timeout_s)
        for p in ("operations-monitor", "execution-engineer"):
            offered = {t for t in self.model.offered.get(p, []) if t.startswith("mcp__")}
            expected = {mcp(sv, t) for sv, tl in (ROSTER[p].get("mcp") or {}).items() for t in tl}
            ok(f"{p} was offered exactly its roster MCP tools", offered == expected,
               f"extra: {sorted(offered - expected)} missing: {sorted(expected - offered)}" if offered != expected
               else ", ".join(sorted(t.split("__", 1)[1] for t in offered)))
        ok("operations-monitor sees the incident-triage skill", "incident-triage" in
           self.model.systems.get("operations-monitor", ""))
        tok = _env(self.home, "execution-engineer").get("ATLAS_TOKEN_OPERATIONS")
        status, body = dispatch(OpsService(SimulatedEngine(self.state)), TokenStore.load(self.engine_tokens,
                                ENGINE_SCOPES), "operations/disable_trading", tok, {"reason": "not mine to do (drill)"},
                                OPS_ROUTES)
        ok("the engine API refuses disable_trading with execution-engineer's own token",
           status == 403 and body["code"] == "forbidden", f"{status} {body.get('code')}")

        # Scheduled cards are created once per period.
        for _ in range(2):
            self.cron("atlas-calibration-review", CRON_PROFILE["atlas-calibration-review"])
        weekly = [c for c in self.cards("atlas-research")
                  if str(c.get("title", "")).startswith("Weekly calibration review (")]
        ok("weekly calibration card created once for this week", len(weekly) == 1,
           [c.get("title") for c in weekly])

        # Dashboard tab API through a real hermes dashboard.
        self.dashboard_check()

        # Engine down.
        n = len(self.jsonl("alerts", "alerts.jsonl"))
        self.engine.clear()
        self.engine.enable("yahye", "drill: after KILL review")
        self.api.shutdown()
        self.api.server_close()
        out = self.cron("atlas-health-check")
        down = [c for c in self.incident_cards() if c["id"] not in (halt_id, kill_id)]
        ok("engine down: UNREACHABLE alert and an incident card", "UNREACHABLE: engine_api_unreachable" in out
           and len(down) == 1, out[:200])
        if down:
            states = self.dispatch_until_settled(OPS, [down[0]["id"]], timeout_s)
            blocked = [f for f in self.model.finish if f["tool"] == "kanban_block"
                       and "unreachable" in f["args"]["reason"].lower()]
            ok("engine down: the worker blocked for the operator", states[down[0]["id"]] == "blocked" and blocked
               and self.runs(down[0]["id"]) == 1, states)
        errors = [a for a in self.jsonl("audit", "agent_actions.jsonl") if a["outcome"] == "error"]
        ok("engine down: the failed call is audited as an error", errors and errors[-1]["tool"] == "health_state",
           [(a["tool"], a["outcome"]) for a in errors])

    def dashboard_check(self) -> None:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        env = {**self.run.env, "HERMES_DASHBOARD_SESSION_TOKEN": "atlas-drill-session"}
        proc = subprocess.Popen([self.hermes, "-p", "atlas-orchestrator", "dashboard", "--isolated", "--skip-build",
                                 "--no-open", "--port", str(port)], env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True)
        body, err = None, ""
        try:
            deadline = time.time() + 60
            while time.time() < deadline and body is None:
                try:
                    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/plugins/atlas/overview",
                                                 headers={"Authorization": "Bearer atlas-drill-session"})
                    with urllib.request.urlopen(req, timeout=10) as r:
                        body = json.loads(r.read())
                except Exception as e:  # noqa: BLE001 - server still starting
                    err = str(e)
                    if proc.poll() is not None:
                        break
                    time.sleep(1)
        finally:
            proc.terminate()
            try:
                out = proc.communicate(timeout=15)[0]
            except subprocess.TimeoutExpired:
                proc.kill()
                out = proc.communicate()[0]
        if body is None:
            self.ok("dashboard: /api/plugins/atlas/overview answers in hermes dashboard", False,
                    f"{err} | {out[-300:]}")
            return
        eng = body["engine"]
        self.ok("dashboard: /api/plugins/atlas/overview answers in hermes dashboard", True)
        self.ok("dashboard: engine panels read with the dashboard's own read token",
                eng.get("ok") and eng["status"]["state"] == "KILL" and eng["status"]["source"] == "simulated",
                eng.get("error") or eng["status"]["state"])
        ops = next((b for b in body["budget"]["boards"] if b["board"] == OPS), {})
        self.ok("dashboard: actions, alerts, budget and experiments panels are filled",
                body["actions"] and body["alerts"] and ops.get("used", 0) > 0
                and {"strategy": "trend_pullback", "runs": 2} in body["experiments"].get("strategies", []),
                f"{len(body['actions'])} actions, {len(body['alerts'])} alerts, atlas-ops {ops}")
        self.ok("dashboard: deferred panels say what they wait for", set(body["deferred"]) ==
                {"equity", "risk", "calibration", "cost"})


CRON_PROFILE = {n: s["profile"] for n, s in
                yaml.safe_load((REPO / "deploy" / "hermes" / "cron.yaml").read_text())["jobs"].items()}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hermes", default="hermes", help="hermes executable at the pinned version")
    ap.add_argument("--timeout", type=int, default=240, help="seconds to wait for each drill card")
    ap.add_argument("--keep", action="store_true", help="keep the temporary Hermes home for inspection")
    ap.add_argument("--report", help="write the results as JSON to this path")
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="atlas-h3-gate-"))
    model = OpsModel()
    server = serve(model)
    drill = Drill(args.hermes, tmp, model, f"http://127.0.0.1:{server.server_address[1]}/v1")
    error = None
    try:
        drill.build()
        drill.scenarios(args.timeout)
    except Exception as e:  # noqa: BLE001 - report what ran, then fail
        error = f"{type(e).__name__}: {e}"
    finally:
        server.shutdown()
        try:
            drill.api.shutdown()
        except Exception:  # noqa: BLE001 - already stopped by the engine-down scenario
            pass
        if not args.keep:
            subprocess.run(["rm", "-rf", str(tmp)])
        else:
            print(f"kept {tmp}")

    results = drill.results + ([("drill ran to the end", False, error)] if error else [])
    width = max(len(n) for n, _, _ in results)
    for name, passed, detail in results:
        print(f"{'PASS' if passed else 'FAIL'}  {name.ljust(width)}  {detail}")
    passed = all(p for _, p, _ in results)
    print(f"\nH3 exit gate (simulated incidents through Hermes): {'PASSED' if passed else 'FAILED'}")
    if args.report:
        Path(args.report).write_text(json.dumps(
            {"passed": passed, "checks": [{"name": n, "passed": p, "detail": d} for n, p, d in results],
             "tool_results": model.results, "finishes": model.finish}, indent=2, default=str))
    return 0 if passed else 1


if __name__ == "__main__":
    os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    sys.exit(main())
