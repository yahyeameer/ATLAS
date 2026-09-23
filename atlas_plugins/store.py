"""Host-wide ATLAS operations files on the agent host.

    <hermes root>/atlas/
        ops.yaml                    budgets and alert settings (installed from deploy/hermes/ops.yaml)
        audit/agent_actions.jsonl   every ATLAS MCP tool call by any profile, from the hooks
        audit/usage.jsonl           model tokens per call, with profile and Kanban board
        alerts/alerts.jsonl         every alert raised; the relay delivers from a cursor
        state/*.json                cursors and last-seen states of the cron checks
        reports/                    hourly reconciliation reports

<hermes root> is Hermes' root home (HERMES_HOME without /profiles/<name>), so
every profile's hooks and cron scripts share one place, and the gateway units
already allow writes under it. Agents' file tools are fenced to the Kanban
workspaces (HERMES_WRITE_SAFE_ROOT) and their terminals run in Docker, so they
cannot edit these files. ATLAS_OPS_DIR overrides the location.
"""

from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
from pathlib import Path

import yaml

DEFAULT_CONFIG = {
    # Monthly model-token budget (input + output) per Kanban board (PRD §5, §15).
    # "other" counts gateway chats and cron sessions. Placeholders until the operator sets them.
    "budgets": {"atlas-research": 60_000_000, "atlas-engineering": 40_000_000, "atlas-ops": 10_000_000,
                "other": 10_000_000},
    "budget_alert_fracs": [0.8, 1.0],
    "alert_on_refusals": True,
    "health_check": {"remind_after_min": 120},
    # Engine mode -> Kanban tenant for incident cards (deploy/hermes/boards.yaml).
    "tenants": {"paper": "paper", "evaluation": "eval-ftmo", "funded": "funded-ftmo"},
    "research_registry": None,
    "experiments_per_strategy_per_month": 20,
}


def ops_dir() -> Path:
    override = os.environ.get("ATLAS_OPS_DIR", "").strip()
    if override:
        return Path(override)
    try:
        from hermes_constants import get_default_hermes_root
        root = get_default_hermes_root()
    except ImportError:
        home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
        root = home.parent.parent if home.parent.name == "profiles" else home
    return Path(root) / "atlas"


def path(*parts: str) -> Path:
    return ops_dir().joinpath(*parts)


AUDIT = ("audit", "agent_actions.jsonl")
USAGE = ("audit", "usage.jsonl")
ALERTS = ("alerts", "alerts.jsonl")


def config() -> dict:
    p = path("ops.yaml")
    data = (yaml.safe_load(p.read_text()) if p.exists() else None) or {}
    return {**DEFAULT_CONFIG, **data}


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso(t: dt.datetime | None = None) -> str:
    return (t or now()).astimezone(dt.timezone.utc).isoformat(timespec="seconds")


def append(rel: tuple[str, ...], record: dict) -> None:
    """Append one JSON line under an exclusive lock (several worker processes write at once)."""
    p = path(*rel)
    p.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(record, default=str, separators=(",", ":")) + "\n").encode()
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o640)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        os.write(fd, data)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def read(rel: tuple[str, ...], start: int = 0) -> list[dict]:
    """Complete records from line ``start`` on (a corrupt line reads as {"corrupt_line": n})."""
    p = path(*rel)
    if not p.exists():
        return []
    out = []
    with open(p) as fh:
        fcntl.flock(fh, fcntl.LOCK_SH)
        try:
            lines = fh.readlines()
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)
    for i, line in enumerate(lines[start:], start):
        if not line.endswith("\n"):
            break
        try:
            out.append(json.loads(line))
        except ValueError:
            out.append({"corrupt_line": i})
    return out


def tail(rel: tuple[str, ...], n: int) -> list[dict]:
    return read(rel)[-n:] if n > 0 else []


def load_state(name: str) -> dict:
    p = path("state", f"{name}.json")
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return {}


def save_state(name: str, state: dict) -> None:
    p = path("state", f"{name}.json")
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1, default=str))
    os.replace(tmp, p)


def alert(severity: str, source: str, text: str, **extra) -> dict:
    """Raise an alert. The alert-relay cron job delivers it to the operations chat."""
    assert severity in ("info", "warning", "critical"), severity
    rec = {"at": iso(), "severity": severity, "source": source, "text": text[:1000], **extra}
    append(ALERTS, rec)
    return rec
