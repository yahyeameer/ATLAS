"""Engine alerts that don't depend on Hermes (PRD §23: the engine runs without the agent runtime).

Every alert is appended to an outbox file on the engine host, then handed to
the configured senders. The only sender here is SMTP from the standard
library, configured from the engine host's environment:

    ATLAS_ALERT_SMTP_HOST, ATLAS_ALERT_SMTP_PORT (587), ATLAS_ALERT_SMTP_USER,
    ATLAS_ALERT_SMTP_PASSWORD, ATLAS_ALERT_FROM, ATLAS_ALERT_TO (comma-separated)

With no host set nothing is sent and the outbox is the record. A failed send
is recorded and retried on the next alert; it never stops the engine. The
Hermes relay (H3) still reads the engine's event log through the operations
API, so alerts reach Telegram too when the agent host is up.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import smtplib
from email.message import EmailMessage
from pathlib import Path


class SmtpSender:
    def __init__(self, host: str, port: int, user: str | None, password: str | None, sender: str, to: list[str],
                 smtp=smtplib.SMTP):
        self.host, self.port, self.user, self.password = host, port, user, password
        self.sender, self.to, self.smtp = sender, to, smtp

    @classmethod
    def from_env(cls) -> "SmtpSender | None":
        host = os.environ.get("ATLAS_ALERT_SMTP_HOST")
        to = [a.strip() for a in os.environ.get("ATLAS_ALERT_TO", "").split(",") if a.strip()]
        if not host or not to:
            return None
        return cls(host, int(os.environ.get("ATLAS_ALERT_SMTP_PORT", 587)), os.environ.get("ATLAS_ALERT_SMTP_USER"),
                   os.environ.get("ATLAS_ALERT_SMTP_PASSWORD"), os.environ.get("ATLAS_ALERT_FROM", to[0]), to)

    def send(self, alerts: list[dict]) -> None:
        msg = EmailMessage()
        worst = max((a["severity"] for a in alerts), key=["info", "warning", "critical"].index)
        msg["Subject"] = f"[ATLAS engine {worst}] {alerts[-1]['text'][:80]}"
        msg["From"], msg["To"] = self.sender, ", ".join(self.to)
        msg.set_content("\n\n".join(f"{a['at']} {a['severity'].upper()}: {a['text']}" for a in alerts))
        with self.smtp(self.host, self.port, timeout=10) as s:
            s.starttls()
            if self.user:
                s.login(self.user, self.password or "")
            s.send_message(msg)


class AlertOutbox:
    def __init__(self, path: str | Path, senders: list | None = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.senders = senders if senders is not None else [s for s in (SmtpSender.from_env(),) if s]
        self.pending: list[dict] = []

    def alert(self, severity: str, text: str, at: dt.datetime | None = None, **extra) -> dict:
        rec = {"at": (at or dt.datetime.now(dt.timezone.utc)).isoformat(timespec="seconds"), "severity": severity,
               "text": text[:1000], **extra}
        self._append(rec)
        self.pending.append(rec)
        self.flush()
        return rec

    def flush(self) -> None:
        if not self.senders or not self.pending:
            self.pending.clear()
            return
        batch, self.pending = self.pending, []
        for s in self.senders:
            try:
                s.send(batch)
            except Exception as e:  # noqa: BLE001 - an alert failure must never stop the engine
                self._append({"at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                              "severity": "warning", "text": f"alert delivery failed: {e}", "undelivered": len(batch)})
                self.pending = (batch + self.pending)[-200:]
                return

    def _append(self, rec: dict) -> None:
        try:
            with self.path.open("a") as f:
                f.write(json.dumps(rec, default=str) + "\n")
        except OSError:
            pass
