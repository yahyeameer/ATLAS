"""Hermes plugin hooks for ATLAS: audit, token accounting and Kanban alerts (PRD §5, §9, §24).

Registered by the ``atlas`` plugin (atlas_plugins/hermes/atlas/), which the
installer enables in every ATLAS profile. Kanban completed/blocked hooks fire
in the worker process and worker-exit hooks in the dispatcher (H0 gap G6), so
the plugin runs everywhere and all processes write to the one ops directory.

- ``post_tool_call``: every ``mcp__atlas_*`` call becomes one line in
  audit/agent_actions.jsonl (the agent_actions feed of PRD §24). A successful
  ``disable_trading`` raises a critical alert; a scope or holdout refusal a warning.
- ``post_api_request``: model tokens per profile and Kanban board, for the
  monthly budgets on the dashboard and in the alert relay.
- Kanban: atlas-ops cards that block or finish, and any worker that crashes,
  raise alerts for the operations chat. Research-board notifications already
  reach the operator through the orchestrator's Kanban subscriptions (H1).

Callbacks never raise: an audit failure must not break an agent turn. Hermes
isolates hook errors too, but the audit file is best-effort next to the
engine's own journal, which is authoritative.
"""

from __future__ import annotations

import json
import logging
import os
import re

from . import store

log = logging.getLogger("atlas_plugins.hooks")

ATLAS_TOOL = re.compile(r"^mcp__(atlas_[a-z]+)__([a-z_]+)$")
WRITE_TOOLS = {"run_backtest", "run_walk_forward", "disable_trading"}
REFUSAL = re.compile(r"refused \((\w+)\)")
OPS_BOARD = "atlas-ops"
MAX_ARGS = 2000


def _profile() -> str:
    name = os.environ.get("HERMES_PROFILE", "").strip()
    if name:
        return name
    try:
        from hermes_cli.profiles import get_active_profile_name
        return get_active_profile_name()
    except Exception:  # noqa: BLE001 - outside Hermes (tests, scripts)
        return "unknown"


def _context() -> dict:
    return {"profile": _profile(),
            "board": os.environ.get("HERMES_KANBAN_BOARD") or None,
            "kanban_task": os.environ.get("HERMES_KANBAN_TASK") or None}


def parse_result(result, status: str | None = None, error_message: str | None = None) -> tuple[str, str | None, dict | str | None]:
    """(outcome, refusal code, payload) from a Hermes MCP tool result string."""
    text = result if isinstance(result, str) else json.dumps(result, default=str)
    payload: dict | str | None = None
    outer = None
    if "{" in (text or ""):
        try:
            outer = json.loads(text[text.find("{"):text.rfind("}") + 1])
        except ValueError:
            outer = None
    if isinstance(outer, dict) and "error" in outer:
        err = str(outer["error"])
        m = REFUSAL.search(err)
        return ("refused" if m else "error"), (m.group(1) if m else None), err[:500]
    if isinstance(outer, dict):
        inner = outer.get("result", outer)
        try:
            payload = json.loads(inner) if isinstance(inner, str) else inner
        except ValueError:
            payload = inner
    if status and status not in ("ok", "success"):
        msg = error_message or text or ""
        m = REFUSAL.search(msg)
        return ("refused" if m else "error"), (m.group(1) if m else None), msg[:500]
    return "ok", None, payload


def on_post_tool_call(tool_name: str = "", args=None, result=None, session_id=None, duration_ms=None,
                      status=None, error_message=None, **_):
    try:
        m = ATLAS_TOOL.match(tool_name or "")
        if not m:
            return
        server, tool = m.group(1).replace("_", "-"), m.group(2)
        outcome, code, payload = parse_result(result, status, error_message)
        ctx = _context()
        args_text = json.dumps(args or {}, default=str)
        store.append(store.AUDIT, {
            "at": store.iso(), **ctx, "session_id": session_id, "server": server, "tool": tool,
            "write": tool in WRITE_TOOLS, "args": args_text[:MAX_ARGS], "outcome": outcome, "code": code,
            "duration_ms": duration_ms,
            "detail": payload if outcome != "ok" else _summary(payload),
        })
        if tool == "disable_trading" and outcome == "ok":
            already = isinstance(payload, dict) and payload.get("already_disabled")
            reason = (args or {}).get("reason", "")
            store.alert("warning" if already else "critical", "hooks",
                        f"{ctx['profile']} {'confirmed trading already disabled' if already else 'DISABLED new trading'}"
                        f": {reason}", kanban_task=ctx["kanban_task"])
        elif outcome == "refused" and store.config().get("alert_on_refusals"):
            store.alert("warning", "hooks", f"{ctx['profile']} was refused {server}.{tool} ({code})",
                        kanban_task=ctx["kanban_task"])
    except Exception:  # noqa: BLE001
        log.exception("atlas audit hook failed")


def _summary(payload) -> dict | None:
    """Keep the audit line small: ids and verdicts, not whole results."""
    if not isinstance(payload, dict):
        return None
    keys = ("run_id", "experiment_id", "state", "status", "trading_enabled", "already_disabled", "source", "code")
    return {k: payload[k] for k in keys if k in payload} or None


def on_post_api_request(usage=None, model=None, provider=None, session_id=None, **_):
    try:
        if not isinstance(usage, dict):
            return
        tokens_in = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
        tokens_out = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
        store.append(store.USAGE, {
            "at": store.iso(), **_context(), "session_id": session_id, "model": model, "provider": provider,
            "input_tokens": tokens_in, "output_tokens": tokens_out,
            "cache_read_tokens": int(usage.get("cache_read_tokens") or 0),
        })
    except Exception:  # noqa: BLE001
        log.exception("atlas usage hook failed")


def on_kanban_task_blocked(task_id=None, board=None, assignee=None, reason=None, **_):
    try:
        if board == OPS_BOARD:
            store.alert("warning", "kanban", f"atlas-ops card {task_id} ({assignee}) is blocked and needs you: "
                                             f"{(reason or '').strip()[:600]}", kanban_task=task_id)
    except Exception:  # noqa: BLE001
        log.exception("atlas kanban hook failed")


def on_kanban_task_completed(task_id=None, board=None, assignee=None, summary=None, **_):
    try:
        if board == OPS_BOARD:
            store.alert("info", "kanban", f"atlas-ops card {task_id} ({assignee}) done: "
                                          f"{(summary or '').strip()[:600]}", kanban_task=task_id)
    except Exception:  # noqa: BLE001
        log.exception("atlas kanban hook failed")


def on_kanban_worker_exited(task_id=None, board=None, assignee=None, exit_kind=None, exit_code=None,
                            outcome=None, retry_status=None, **_):
    try:
        store.alert("warning", "kanban", f"{assignee} worker on {board} card {task_id} exited "
                                         f"({exit_kind}, code {exit_code}, {outcome}); retry: {retry_status}",
                    kanban_task=task_id)
    except Exception:  # noqa: BLE001
        log.exception("atlas kanban hook failed")


HOOKS = {
    "post_tool_call": on_post_tool_call,
    "post_api_request": on_post_api_request,
    "kanban_task_blocked": on_kanban_task_blocked,
    "kanban_task_completed": on_kanban_task_completed,
    "on_kanban_worker_exited": on_kanban_worker_exited,
}


def register(ctx) -> None:
    for name, fn in HOOKS.items():
        ctx.register_hook(name, fn)
