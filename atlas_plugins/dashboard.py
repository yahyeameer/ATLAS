"""Data and routes behind the ATLAS tab of the Hermes web dashboard (PRD §2, §5, §25).

``plugin_api.py`` in the installed plugin re-exports ``router``; Hermes mounts
it at /api/plugins/atlas/ behind the dashboard's own auth. The dashboard binds
to localhost only (PRD §4); reach it over an SSH tunnel.

Panels and where their numbers come from:

    health, trading, reconciliation   engine operations API, with the dashboard's own ops:read token
    agent actions                     audit/agent_actions.jsonl (hooks)
    alerts                            alerts/alerts.jsonl
    token budget per board            audit/usage.jsonl against ops.yaml budgets
    experiments this month            the research registry against the PRD §13 budget
    equity, risk, calibration, cost   not available until the engine journal (T4, T7) and calibration (T2)

Every read is best-effort: a panel whose source is missing says why instead of
failing the page. Nothing here writes to the engine.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from collections import Counter
from pathlib import Path

from . import jobs, store

DEFERRED = {
    "equity": "Equity curve and daily P/L need the engine journal (T4; paper fills in T7).",
    "risk": "Open risk, currency exposure and prop-rule headroom need the risk engine (T3) and engine journal (T4).",
    "calibration": "Brier score and reliability curves need the selection layer (T2).",
    "cost": "Spread, slippage and commission drift need real fills (T4, T7).",
}


def _env_value(name: str) -> str:
    """From the process environment, else from the dashboard profile's .env."""
    if os.environ.get(name):
        return os.environ[name]
    try:
        from hermes_constants import get_hermes_home
        env = Path(get_hermes_home()) / ".env"
    except ImportError:
        return ""
    if env.exists():
        for line in env.read_text().splitlines():
            k, _, v = line.partition("=")
            if k.strip() == name:
                return v.strip()
    return ""


def engine_panels(client=None) -> dict:
    try:
        client = client or jobs.OpsClient(_env_value("ATLAS_ENGINE_URL"), _env_value("ATLAS_TOKEN_DASHBOARD"))
        return {"ok": True, "status": client("operations/status"), "health": client("operations/health"),
                "reconciliation": client("operations/reconciliation")}
    except (jobs.EngineError, SystemExit) as e:
        return {"ok": False, "error": str(e)}


def budget_panel(now: dt.datetime | None = None) -> dict:
    cfg = store.config()
    st = jobs.usage_totals(now)
    boards = []
    for board, limit in (cfg.get("budgets") or {}).items():
        used = st["used"].get(board, 0)
        boards.append({"board": board, "used": used, "budget": limit, "frac": round(used / limit, 4) if limit else None})
    return {"month": st["month"], "boards": boards}


def experiments_panel(now: dt.datetime | None = None) -> dict:
    cfg = store.config()
    reg = cfg.get("research_registry")
    if not reg or not Path(reg).exists():
        return {"available": False, "why": "research registry not configured or not found"}
    month = f"{(now or store.now()):%Y-%m}"
    counts: Counter = Counter()
    for line in Path(reg).read_text().splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if str(e.get("created_at", "")).startswith(month):
            counts[e.get("strategy") or "unknown"] += 1
    limit = cfg.get("experiments_per_strategy_per_month", 20)
    return {"available": True, "month": month, "budget_per_strategy": limit,
            "strategies": [{"strategy": s, "runs": n} for s, n in sorted(counts.items())]}


def overview(client=None, now: dt.datetime | None = None) -> dict:
    actions = store.tail(store.AUDIT, 50)
    return {
        "as_of": store.iso(now),
        "engine": engine_panels(client),
        "actions": list(reversed(actions)),
        "writes_today": sum(1 for a in actions if a.get("write") and str(a.get("at", "")).startswith(f"{(now or store.now()):%Y-%m-%d}")),
        "alerts": list(reversed(store.tail(store.ALERTS, 30))),
        "budget": budget_panel(now),
        "experiments": experiments_panel(now),
        "deferred": DEFERRED,
    }


try:  # FastAPI exists in the dashboard process; keep this module importable without it.
    from fastapi import APIRouter

    router = APIRouter()

    @router.get("/overview")
    def get_overview() -> dict:
        return overview()
except ImportError:  # pragma: no cover
    router = None
