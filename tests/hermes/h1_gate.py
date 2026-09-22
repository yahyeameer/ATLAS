#!/usr/bin/env python3
"""H1 exit gate: the orchestrator routes a dummy card through three profiles.

Builds a throwaway Hermes home with deploy/hermes/bootstrap.py, drops a triage
card on the atlas-research board, lets the atlas-orchestrator decomposer fan it
out, runs the Kanban dispatcher until the card graph finishes, and checks what
happened.

Two modes:

  offline (default)  Every profile talks to a scripted OpenAI-compatible endpoint
                     started by this script, so no model key is needed. The
                     decomposer's answer is scripted (market-researcher ->
                     strategy-researcher -> risk-analyst), so this mode proves the
                     Hermes wiring, not routing judgment: profiles install from
                     the distributions, the roster and descriptions reach the
                     decomposer, each worker runs under its own profile with its
                     own toolset, handoffs flow from parent to child, the
                     orchestrator wakes to close the parent, and the board stays
                     isolated.
  --live             Uses deploy/hermes/models.yaml and your provider key, and
                     checks that the real orchestrator model routes the card to at
                     least three ATLAS specialists and the graph completes.

    python tests/hermes/h1_gate.py --hermes /path/to/venv/bin/hermes
    python tests/hermes/h1_gate.py --hermes ... --live

Exit status 0 means every check passed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
ROSTER = yaml.safe_load((REPO / "atlas-profiles" / "roster.yaml").read_text())["profiles"]
MANAGED = yaml.safe_load((REPO / "deploy" / "hermes" / "managed" / "config.yaml").read_text())
BOARD = "atlas-research"
ROUTE = ["market-researcher", "strategy-researcher", "risk-analyst"]
CARD_TITLE = "H1 routing drill: EURUSD London trend-pullback idea"
CARD_BODY = (
    "Routing drill for the H1 exit gate. Do not do real research, run tools other than "
    "the kanban tools, or write files. Split this into three steps that each belong to a "
    "different specialist: market context, a hypothesis, and a risk review of that "
    "hypothesis. Each worker completes immediately with a one-line summary naming its role."
)
SOUL_MARKER = re.compile(r"ATLAS profile: ([a-z0-9-]+)")

# Tool names per Hermes toolset, for checking what each worker was offered.
TOOLSET_TOOLS = {
    "kanban": None,  # any kanban_* tool
    "memory": {"memory"},
    "skills": {"skill_view", "skills_list", "skill_manage"},
    "session_search": {"session_search"},
    "todo": {"todo_list"},
    "code_execution": {"execute_code"},
    "terminal": {"terminal", "process_manage"},
    "file": {"read_file", "write_file", "patch", "search_files"},
    "web": {"web_search", "web_extract"},
}
# Tools Hermes may add on its own for tool discovery; not a permission.
META_TOOLS = {"tool_search", "tool_describe", "tool_call"}


# --------------------------------------------------------------------------- scripted model


class ScriptedModel:
    """OpenAI-compatible endpoint that plays the decomposer and the workers."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.decomposer_prompts: list[str] = []
        self.worker_turns: list[dict] = []  # {profile, tools, show_result}

    # -- responses

    def respond(self, req: dict) -> dict:
        messages = req.get("messages") or []
        system = next((m.get("content") or "" for m in messages if m.get("role") == "system"), "")
        tools = sorted(t["function"]["name"] for t in req.get("tools") or [])
        if "You are the Kanban decomposer" in system:
            return self._decompose(messages)
        if "kanban_complete" in tools:
            return self._work(system, messages, tools)
        return {"content": "ok"}

    def _decompose(self, messages: list) -> dict:
        user = "\n".join(str(m.get("content")) for m in messages if m.get("role") == "user")
        with self.lock:
            self.decomposer_prompts.append(user)
        tasks = [
            {"title": "Summarise EURUSD London-session context", "assignee": ROUTE[0], "parents": [],
             "body": "Drill step 1: complete immediately with a one-line summary."},
            {"title": "Write a trend-pullback hypothesis from that context", "assignee": ROUTE[1],
             "parents": [0], "body": "Drill step 2: complete immediately with a one-line summary."},
            {"title": "Risk-review the hypothesis against the §15 gates", "assignee": ROUTE[2],
             "parents": [1], "body": "Drill step 3: complete immediately with a one-line summary."},
        ]
        roster = set(re.findall(r"\b([a-z]+(?:-[a-z]+)+)\b", user))
        for t in tasks:  # like a real model, only pick names the roster offered
            if t["assignee"] not in roster:
                t["assignee"] = None
        return {"content": json.dumps({"fanout": True, "rationale": "three specialist steps", "tasks": tasks})}

    def _work(self, system: str, messages: list, tools: list) -> dict:
        m = SOUL_MARKER.search(system)
        profile = m.group(1) if m else "unknown"
        last = messages[-1]
        called = _last_tool_call_name(messages)
        if last.get("role") != "tool":
            return _tool_call("kanban_show", {})
        if called == "kanban_show":
            show = str(last.get("content"))
            with self.lock:
                self.worker_turns.append({"profile": profile, "tools": tools, "show_result": show})
            summary = f"{profile}: drill step done"
            if profile == "atlas-orchestrator":
                summary = "atlas-orchestrator: all three drill steps are done; closing the card"
            return _tool_call("kanban_complete", {"summary": summary,
                                                  "metadata": {"drill": "h1-gate", "profile": profile}})
        return {"content": "Done."}


def _last_tool_call_name(messages: list) -> str | None:
    for m in reversed(messages):
        for call in m.get("tool_calls") or []:
            return call["function"]["name"]
    return None


def _tool_call(name: str, args: dict) -> dict:
    return {"tool_calls": [{"id": f"call_{uuid.uuid4().hex[:12]}", "type": "function",
                            "function": {"name": name, "arguments": json.dumps(args)}}]}


def serve(model: ScriptedModel) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # quiet
            pass

        def _json(self, code: int, obj: dict) -> None:
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self._json(200, {"object": "list", "data": [{"id": "atlas-fake", "object": "model"}]})

        def do_POST(self):
            req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            if not self.path.endswith("/chat/completions"):
                return self._json(404, {"error": "not found"})
            out = model.respond(req)
            finish = "tool_calls" if out.get("tool_calls") else "stop"
            base = {"id": "chatcmpl-atlas", "created": int(time.time()), "model": "atlas-fake"}
            usage = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
            if not req.get("stream"):
                msg = {"role": "assistant", "content": out.get("content")}
                if out.get("tool_calls"):
                    msg["tool_calls"] = out["tool_calls"]
                return self._json(200, {**base, "object": "chat.completion", "usage": usage,
                                        "choices": [{"index": 0, "message": msg, "finish_reason": finish}]})
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            delta = {"role": "assistant"}
            if out.get("content"):
                delta["content"] = out["content"]
            if out.get("tool_calls"):
                delta["tool_calls"] = [{"index": i, **c} for i, c in enumerate(out["tool_calls"])]
            chunks = [
                {**base, "object": "chat.completion.chunk",
                 "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                {**base, "object": "chat.completion.chunk", "usage": usage,
                 "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]},
            ]
            for c in chunks:
                self.wfile.write(f"data: {json.dumps(c)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


# --------------------------------------------------------------------------- driver


class Run:
    def __init__(self, hermes: str, home: Path, extra_env: dict):
        self.hermes = hermes
        self.env = {**os.environ, "HERMES_HOME": str(home), **extra_env}

    def __call__(self, *args: str, check: bool = True) -> str:
        proc = subprocess.run([self.hermes, *args], env=self.env, capture_output=True, text=True)
        if check and proc.returncode != 0:
            raise RuntimeError(f"hermes {' '.join(args)} failed:\n{proc.stdout}{proc.stderr}")
        return proc.stdout

    def json(self, *args: str):
        return json.loads(self(*args, "--json"))


def build_home(hermes: str, home: Path, live: bool, base_url: str | None) -> Path:
    models = REPO / "deploy" / "hermes" / "models.yaml"
    if not live:
        models = home.parent / "models-offline.yaml"
        tier = {"provider": "custom", "model": "atlas-fake", "base_url": base_url}
        models.write_text(yaml.safe_dump({"tiers": {t: tier for t in ("frontier", "mid", "cheap")}}))
    cmd = [sys.executable, str(REPO / "deploy" / "hermes" / "bootstrap.py"), "--hermes", hermes,
           "--hermes-home", str(home), "--models", str(models),
           "--api-tokens", str(home.parent / "api-tokens.yaml")]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"bootstrap failed:\n{proc.stdout}{proc.stderr}")
    return models


def drive(run: Run, timeout_s: int) -> str:
    out = run("-p", "atlas-orchestrator", "kanban", "--board", BOARD, "create", CARD_TITLE,
              "--triage", "--tenant", "paper", "--body", CARD_BODY, "--json")
    root = json.loads(out)["id"]
    run("-p", "atlas-orchestrator", "kanban", "--board", BOARD, "decompose", root)
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        run("-p", "atlas-orchestrator", "kanban", "--board", BOARD, "dispatch")
        task = run.json("kanban", "--board", BOARD, "show", root)
        if _task(task)["status"] in ("done", "blocked"):
            return root
        time.sleep(3)
    raise RuntimeError(f"card graph did not finish within {timeout_s}s")


def _task(show: dict) -> dict:
    return show.get("task", show)


# --------------------------------------------------------------------------- checks


def allowed_tools(profile: str) -> set[str] | None:
    """Every tool a worker for `profile` may be offered, or None if unbounded."""
    disabled = set(MANAGED["agent"]["disabled_toolsets"])
    out: set[str] = set()
    for ts in ROSTER[profile]["toolsets"]:
        if ts in disabled:
            continue
        tools = TOOLSET_TOOLS.get(ts)
        if tools is None and ts != "kanban":
            return None
        out |= tools or set()
    # H2: the role's ATLAS MCP tools, under Hermes' registered names.
    for server, tools in (ROSTER[profile].get("mcp") or {}).items():
        out |= {f"mcp__{server.replace('-', '_')}__{t}" for t in tools}
    return out


def check(run: Run, root: str, model: ScriptedModel | None) -> list[tuple[str, bool, str]]:
    results: list[tuple[str, bool, str]] = []

    def ok(name: str, cond: bool, detail: str = "") -> None:
        results.append((name, bool(cond), detail))

    tasks = run.json("kanban", "--board", BOARD, "list", "--archived")
    by_id = {t["id"]: t for t in tasks}
    root_task = by_id[root]
    children = [t for t in tasks if t["id"] != root]
    assignees = [c.get("assignee") for c in sorted(children, key=lambda t: t.get("created_at") or 0)]

    ok("decomposer fanned the card out to 3+ child cards", len(children) >= 3, f"{len(children)} children")
    atlas = [a for a in assignees if a in ROSTER and a != "atlas-orchestrator"]
    ok("children went to 3+ distinct ATLAS specialists", len(set(atlas)) >= 3, ", ".join(map(str, assignees)))
    ok("every child card is done", all(c["status"] == "done" for c in children),
       ", ".join(f"{c['assignee']}={c['status']}" for c in children))
    ok("parent card is owned by atlas-orchestrator", root_task.get("assignee") == "atlas-orchestrator",
       str(root_task.get("assignee")))
    ok("parent card was closed after the children", root_task["status"] == "done", root_task["status"])

    runs = run.json("kanban", "--board", BOARD, "runs", root)
    run_rows = runs if isinstance(runs, list) else runs.get("runs", [])
    ok("orchestrator ran the parent card itself",
       any(r.get("profile") == "atlas-orchestrator" for r in run_rows),
       f"{len(run_rows)} run(s) on the parent")

    for other in ("atlas-engineering", "atlas-ops"):
        n = len(run.json("kanban", "--board", other, "list", "--archived"))
        ok(f"board isolation: {other} is untouched", n == 0, f"{n} cards")

    if model is None:
        return results

    prompt = model.decomposer_prompts[0] if model.decomposer_prompts else ""
    missing = [p for p in ROSTER if p not in prompt]
    ok("decomposer saw all 10 ATLAS profiles", not missing, "missing: " + ", ".join(missing) if missing else "")
    desc_missing = [p for p in ROSTER
                    if yaml.safe_load((REPO / "atlas-profiles" / p / "distribution.yaml").read_text())
                    ["description"][:40] not in prompt]
    ok("decomposer saw every routing description", not desc_missing, ", ".join(desc_missing))

    seen = {t["profile"] for t in model.worker_turns}
    ok("each specialist ran under its own profile (SOUL loaded)", set(ROUTE) <= seen,
       ", ".join(sorted(seen)))
    ok("orchestrator woke up to judge the parent", "atlas-orchestrator" in seen)

    for turn in model.worker_turns:
        allowed = allowed_tools(turn["profile"])
        extra = sorted(t for t in turn["tools"]
                       if not t.startswith("kanban_") and t not in META_TOOLS
                       and allowed is not None and t not in allowed)
        own = sorted(t for t in turn["tools"] if not t.startswith("kanban_") and t not in META_TOOLS)
        ok(f"{turn['profile']} was offered only its toolsets", not extra,
           "extra: " + ", ".join(extra) if extra else "kanban_* + " + (", ".join(own) or "nothing else"))

    def show_of(profile: str) -> str:
        return next((t["show_result"] for t in model.worker_turns if t["profile"] == profile), "")

    ok("strategy-researcher received market-researcher's handoff",
       "market-researcher: drill step done" in show_of("strategy-researcher"))
    ok("risk-analyst received strategy-researcher's handoff",
       "strategy-researcher: drill step done" in show_of("risk-analyst"))
    return results


# --------------------------------------------------------------------------- main


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hermes", default="hermes", help="hermes executable at the pinned version")
    ap.add_argument("--live", action="store_true", help="use real models from deploy/hermes/models.yaml")
    ap.add_argument("--timeout", type=int, default=None, help="seconds to wait for the card graph")
    ap.add_argument("--keep", action="store_true", help="keep the temporary Hermes home for inspection")
    ap.add_argument("--report", help="write the results as JSON to this path")
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="atlas-h1-gate-"))  # honours TMPDIR
    home = tmp / "hermes-home"
    model = None if args.live else ScriptedModel()
    server = None if args.live else serve(model)
    base_url = None if args.live else f"http://127.0.0.1:{server.server_address[1]}/v1"
    # Exercise the host policy exactly as deployed, and keep the gate off any
    # Docker daemon: the drill never runs a terminal command.
    extra_env = {"HERMES_MANAGED_DIR": str(REPO / "deploy" / "hermes" / "managed")}
    if not args.live:
        extra_env["OPENAI_API_KEY"] = "sk-atlas-offline"
    try:
        build_home(args.hermes, home, args.live, base_url)
        run = Run(args.hermes, home, extra_env)
        if not args.live:
            for p in ROSTER:
                run("-p", p, "config", "set", "model.api_key", "sk-atlas-offline")
        root = drive(run, args.timeout or (900 if args.live else 240))
        results = check(run, root, model)
    finally:
        if server:
            server.shutdown()
        if not args.keep:
            subprocess.run(["rm", "-rf", str(tmp)])
        else:
            print(f"kept {tmp}")

    width = max(len(n) for n, _, _ in results)
    for name, passed, detail in results:
        print(f"{'PASS' if passed else 'FAIL'}  {name.ljust(width)}  {detail}")
    passed = all(p for _, p, _ in results)
    print(f"\nH1 exit gate ({'live' if args.live else 'offline'}): {'PASSED' if passed else 'FAILED'}")
    if args.report:
        Path(args.report).write_text(json.dumps(
            {"mode": "live" if args.live else "offline", "passed": passed,
             "checks": [{"name": n, "passed": p, "detail": d} for n, p, d in results]}, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
