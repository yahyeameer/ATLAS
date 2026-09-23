"""The ATLAS cron scripts (PRD §10). Each is a Hermes script-only job: no model call.

The installer writes a two-line script per job into the owning profile's
``scripts/`` directory, which calls ``main(<job>)`` here, and schedules it from
deploy/hermes/cron.yaml. Hermes delivers whatever the script prints (nothing
printed = a silent tick) and delivers an error alert if the script fails.

- ``health-check`` (every 15 min): engine health and status. Alerts on every
  change of state, reminds while an incident stays open, and for HALT, KILL or
  an unreachable engine opens an atlas-ops incident card for
  operations-monitor. That card is where a model gets involved: "LLM only on
  anomaly".
- ``alert-relay`` (every minute): delivers alerts raised by the hooks and the
  token-budget check.
- ``reconciliation-report`` (hourly): stores the engine's reconciliation report
  and speaks only on a mismatch or a stale reconcile.
- scheduled cards (daily, weekly, monthly): create one Kanban card per period,
  keyed like ``weekly-calibration-2026-W39`` so a re-run never duplicates it.

The engine calls use the cron job's own ``ops:read`` token (ATLAS_ENGINE_URL,
ATLAS_TOKEN_OPS_CRON, passed through by the profile's terminal.env_passthrough).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

from . import store

HALT_OR_WORSE = ("HALT", "KILL", "UNREACHABLE")
MAX_RELAY_CHARS = 3500
OPS_BOARD = "atlas-ops"


class EngineError(RuntimeError):
    pass


class OpsClient:
    """POSTs to the engine's operations routes with the cron job's ops:read token."""

    def __init__(self, url: str | None = None, token: str | None = None, timeout: float = 20):
        self.url = (url or os.environ.get("ATLAS_ENGINE_URL") or "").rstrip("/")
        self.token = token if token is not None else os.environ.get("ATLAS_TOKEN_OPS_CRON", "")
        if not self.url or not self.token:
            raise SystemExit("ATLAS_ENGINE_URL and ATLAS_TOKEN_OPS_CRON must be set "
                             "(operations-monitor .env, passed through by terminal.env_passthrough)")
        self.timeout = timeout

    def __call__(self, route: str, **args) -> dict:
        req = urllib.request.Request(f"{self.url}/v1/{route}", data=json.dumps(args).encode(), method="POST",
                                     headers={"Content-Type": "application/json",
                                              "Authorization": f"Bearer {self.token}"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            try:
                body = json.loads(e.read())
            except (ValueError, OSError):
                body = {"error": e.reason, "code": str(e.code)}
            raise EngineError(f"{route}: {e.code} {body.get('code')}: {body.get('error')}") from None
        except (urllib.error.URLError, OSError) as e:
            raise EngineError(f"{route}: engine API unreachable at {self.url}: {getattr(e, 'reason', e)}") from None


# --------------------------------------------------------------------------- Kanban cards


def hermes_bin() -> str:
    explicit = os.environ.get("ATLAS_HERMES_BIN")
    if explicit:
        return explicit
    sibling = Path(sys.executable).with_name("hermes")
    return str(sibling) if sibling.exists() else (shutil.which("hermes") or "hermes")


def kanban_create(board: str, title: str, assignee: str, body: str, key: str, tenant: str | None = None,
                  skills: tuple[str, ...] = (), created_by: str = "atlas-cron") -> str:
    """Create a card (or get the existing one with this idempotency key) and return its id."""
    cmd = [hermes_bin(), "kanban", "--board", board, "create", title, "--assignee", assignee, "--body", body,
           "--idempotency-key", key, "--created-by", created_by, "--json"]
    if tenant:
        cmd += ["--tenant", tenant]
    for s in skills:
        cmd += ["--skill", s]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise RuntimeError(f"kanban create failed ({proc.returncode}): {proc.stderr.strip() or proc.stdout.strip()}")
    return str(json.loads(proc.stdout)["id"])


CreateCard = Callable[..., str]


# --------------------------------------------------------------------------- health check


def _trading_line(status: dict | None) -> str:
    if not status:
        return ""
    t = status.get("trading") or {}
    if t.get("enabled"):
        return "New trades: enabled."
    return f"New trades: DISABLED by {t.get('by')} ({t.get('reason')})."


def _incident_body(state: str, reasons: list[str], status: dict | None, detail: str, at: str) -> str:
    source = (status or {}).get("source", "unknown")
    return "\n".join([
        f"Engine health is {state}: {', '.join(reasons)}. Seen by the health check at {at} (engine source: {source}).",
        _trading_line(status) or detail,
        "",
        "Acceptance criteria:",
        "1. Read system_status, health_state and reconciliation_report, and say what you see.",
        "2. If the state is HALT or KILL, or the engine is unreachable, or you see an anomaly the engine has not "
        "caught, and new trades are still enabled, call disable_trading with a one-sentence reason.",
        "3. Never try to re-enable trading, flatten or change a limit. Those are the operator's.",
        "4. If the operator must act (re-enable once the fault clears, flatten, contact the broker), "
        "kanban_block with exactly what they need to do. Otherwise complete with what happened and what you did.",
    ])


def health_check(client: Callable[..., dict], now: dt.datetime | None = None,
                 create_card: CreateCard = kanban_create) -> str:
    now = now or store.now()
    cfg = store.config()
    st = store.load_state("health-check")
    status = None
    try:
        status = client("operations/status")
        health = client("operations/health")
        state, reasons, detail = health["state"], list(health["reasons"]), ""
        symbols = {s: v["reasons"] for s, v in (health.get("symbols") or {}).items() if v["state"] != "NORMAL"}
    except EngineError as e:
        state, reasons, detail, symbols = "UNREACHABLE", ["engine_api_unreachable"], str(e), {}

    key = f"{state}:{','.join(reasons)}"
    lines: list[str] = []
    severity = "critical" if state in HALT_OR_WORSE else "warning"
    if key != st.get("key"):
        if state == "NORMAL":
            if st.get("key"):
                lines.append(f"RECOVERED: engine health back to NORMAL (was {st.get('state')}). "
                             + _trading_line(status))
                severity = "info"
        else:
            where = "; ".join(f"{s}: {', '.join(r)}" for s, r in symbols.items()) if state == "DEGRADED" else ""
            lines.append(" ".join(x for x in [f"{state}: {', '.join(reasons)}.", where and f"({where})",
                                              _trading_line(status), detail] if x))
            if state in HALT_OR_WORSE:
                digest = hashlib.sha1(key.encode()).hexdigest()[:8]
                tenant = (cfg.get("tenants") or {}).get((status or {}).get("mode"))
                card = create_card(
                    OPS_BOARD, f"Incident: engine {state} ({', '.join(reasons)[:80]})", "operations-monitor",
                    _incident_body(state, reasons, status, detail, store.iso(now)),
                    key=f"incident-{now:%Y-%m-%d}-{digest}", tenant=tenant, skills=("incident-triage",),
                    created_by="operations-monitor/health-check")
                lines.append(f"Incident card {card} opened on atlas-ops for operations-monitor.")
        st.update(key=key, state=state, reasons=reasons, alerted_at=store.iso(now))
    elif state != "NORMAL":
        last = dt.datetime.fromisoformat(st.get("alerted_at") or store.iso(now))
        if now - last >= dt.timedelta(minutes=cfg["health_check"]["remind_after_min"]):
            lines.append(f"STILL {state} since {st.get('since') or st.get('alerted_at')}: {', '.join(reasons)}. "
                         + _trading_line(status))
            st["alerted_at"] = store.iso(now)
    if state == "NORMAL":
        st.pop("since", None)
    else:
        st.setdefault("since", store.iso(now))
    store.save_state("health-check", st)
    text = "\n".join(lines)
    if text:
        store.alert(severity, "health-check", text, delivered_by="health-check")
    return text


# --------------------------------------------------------------------------- token budgets


def usage_totals(now: dt.datetime | None = None, save: bool = False) -> dict:
    """This month's model tokens per board, counted incrementally from audit/usage.jsonl."""
    now = now or store.now()
    month = f"{now:%Y-%m}"
    st = store.load_state("budget")
    if st.get("month") != month:
        st = {"month": month, "cursor": st.get("cursor", 0), "used": {}, "alerted": {}}
    recs = store.read(store.USAGE, st["cursor"])
    for r in recs:
        if str(r.get("at", "")).startswith(month):
            board = r.get("board") or "other"
            st["used"][board] = st["used"].get(board, 0) + int(r.get("input_tokens") or 0) + int(r.get("output_tokens") or 0)
    st["cursor"] += len(recs)
    if save:
        store.save_state("budget", st)
    return st


def budget_check(now: dt.datetime | None = None) -> list[dict]:
    cfg = store.config()
    st = usage_totals(now)
    raised = []
    for board, limit in (cfg.get("budgets") or {}).items():
        used = st["used"].get(board, 0)
        done = st["alerted"].setdefault(board, [])
        crossed = [f for f in sorted(cfg.get("budget_alert_fracs") or []) if limit and used >= f * limit and f not in done]
        if crossed:  # one alert per check, at the highest threshold crossed since the last one
            done.extend(crossed)
            raised.append(store.alert("critical" if crossed[-1] >= 1 else "warning", "budget",
                                      f"{board} has used {used / limit:.0%} of its monthly token budget "
                                      f"({used:,} of {limit:,} tokens)."))
    store.save_state("budget", st)
    return raised


# --------------------------------------------------------------------------- alert relay

MARK = {"critical": "[CRITICAL]", "warning": "[WARNING]", "info": "[INFO]"}


def alert_relay(now: dt.datetime | None = None) -> str:
    budget_check(now)
    st = store.load_state("alert-relay")
    cursor = st.get("cursor", 0)
    recs = store.read(store.ALERTS, cursor)
    st["cursor"] = cursor + len(recs)
    store.save_state("alert-relay", st)
    new = [r for r in recs if not r.get("delivered_by") and "text" in r]
    if not new:
        return ""
    lines = [f"ATLAS: {len(new)} alert{'s' if len(new) != 1 else ''}"]
    for i, r in enumerate(new):
        line = f"{MARK.get(r.get('severity'), '[?]')} {str(r.get('at', ''))[11:16]}Z {r['text']}"
        if sum(len(x) + 1 for x in lines) + len(line) > MAX_RELAY_CHARS:
            lines.append(f"... and {len(new) - i} more on the ATLAS dashboard.")
            break
        lines.append(line)
    return "\n".join(lines)


# --------------------------------------------------------------------------- reconciliation report


def reconciliation_report(client: Callable[..., dict], now: dt.datetime | None = None) -> str:
    now = now or store.now()
    try:
        r = client("operations/reconciliation")
    except EngineError as e:
        return f"Reconciliation report unavailable: {e}"
    out = store.path("reports", "reconciliation", f"{now:%Y-%m-%d}", f"{now:%H}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(r, indent=1))
    age = (now - dt.datetime.fromisoformat(r["reconciled_at"])).total_seconds()
    stale = age > 2 * r.get("interval_s", 60)
    if r["status"] == "clean" and not stale:
        return ""
    lines = [f"Reconciliation {r['status'].upper()}: engine {r['engine_positions']} positions, "
             f"broker {r['broker_positions']}. Last reconcile {int(age)} s ago"
             + (" (STALE)." if stale else ".")]
    for m in r.get("mismatches", [])[:10]:
        lines.append(f"- {m['kind']} {m['symbol']} ticket {m['ticket']}: {m.get('detail', '')}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- scheduled cards


def period_of(kind: str, now: dt.datetime) -> str:
    if kind == "day":
        return f"{now:%Y-%m-%d}"
    if kind == "week":
        y, w, _ = now.isocalendar()
        return f"{y}-W{w:02d}"
    if kind == "month":
        return f"{now:%Y-%m}"
    raise ValueError(f"unknown period {kind!r}")


def card_job(spec: dict, now: dt.datetime | None = None, create_card: CreateCard = kanban_create) -> str:
    """One card per period, e.g. key weekly-calibration-2026-W39 (PRD §4). Silent on success."""
    now = now or store.now()
    period = period_of(spec["period"], now)
    create_card(spec["board"], f"{spec['title']} ({period})", spec["assignee"], spec["body"].strip(),
                key=f"{spec['key']}-{period}", tenant=spec.get("tenant"), skills=tuple(spec.get("skills") or ()),
                created_by=f"atlas-cron/{spec['name']}")
    return ""


# --------------------------------------------------------------------------- entry point

JOBS = {
    "health-check": lambda spec: health_check(OpsClient()),
    "alert-relay": lambda spec: alert_relay(),
    "reconciliation-report": lambda spec: reconciliation_report(OpsClient()),
    "card": lambda spec: card_job(spec),
}


def main(job: str, spec: dict | None = None) -> None:
    out = JOBS[job](spec or {})
    if out:
        print(out)
