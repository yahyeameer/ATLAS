"""The trading journal: the PRD §24 tables and the engine's restart state, in SQLite.

PRD §24 names PostgreSQL on the Linux host. This first version writes a local
SQLite file on the engine host instead: it has no server to depend on, so the
engine keeps trading when the network to the agent host is down (§23), and it
needs no new dependency. Every row has the §24 correlation IDs as columns and
the full record as JSON, so shipping rows to Postgres later is a copy, not a
redesign. A failed write raises ``JournalError``; the engine turns that into
HALT (``db_write_failure``).
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
import threading
from pathlib import Path

# PRD §24 tables (market_states and calibration_models come with T2's decision layer).
TABLES = ("decisions", "risk_checks", "orders", "fills", "positions", "trades", "strategy_versions",
          "system_events", "latency_metrics", "experiments", "trade_intents", "agent_actions", "operator_commands")
IDS = ("run_id", "state_id", "decision_id", "trade_id", "experiment_id", "kanban_task_id")


class JournalError(RuntimeError):
    pass


class Journal:
    def __init__(self, path: str | Path, run_id: str):
        self.path = Path(path)
        self.run_id = run_id
        self.fail_writes = False  # failure injection: every write raises until cleared
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        cols = ", ".join(f"{c} TEXT" for c in IDS)
        for t in TABLES:
            self._db.execute(f"CREATE TABLE IF NOT EXISTS {t} (id INTEGER PRIMARY KEY, at TEXT NOT NULL, {cols}, "
                             "data TEXT NOT NULL)")
        self._db.execute("CREATE TABLE IF NOT EXISTS engine_state (key TEXT PRIMARY KEY, at TEXT, data TEXT NOT NULL)")

    def write(self, table: str, record: dict, at: dt.datetime | None = None, **ids) -> None:
        if table not in TABLES:
            raise ValueError(f"unknown journal table {table!r}")
        ids.setdefault("run_id", self.run_id)
        row = [(at or dt.datetime.now(dt.timezone.utc)).isoformat()] + [ids.get(c) for c in IDS]
        self._exec(f"INSERT INTO {table} (at, {', '.join(IDS)}, data) VALUES ({', '.join('?' * (len(IDS) + 2))})",
                   row + [json.dumps(record, default=str, sort_keys=True)])

    def save_state(self, key: str, data: dict) -> None:
        self._exec("INSERT INTO engine_state (key, at, data) VALUES (?, ?, ?) "
                   "ON CONFLICT(key) DO UPDATE SET at = excluded.at, data = excluded.data",
                   [key, dt.datetime.now(dt.timezone.utc).isoformat(), json.dumps(data, default=str, sort_keys=True)])

    def load_state(self, key: str) -> dict | None:
        row = self._db.execute("SELECT data FROM engine_state WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def rows(self, table: str, limit: int = 1000, **where) -> list[dict]:
        if table not in TABLES:
            raise ValueError(f"unknown journal table {table!r}")
        cond = " AND ".join(f"{k} = ?" for k in where if k in IDS)
        sql = f"SELECT at, {', '.join(IDS)}, data FROM {table}" + (f" WHERE {cond}" if cond else "") + \
            " ORDER BY id DESC LIMIT ?"
        out = []
        for r in self._db.execute(sql, [where[k] for k in where if k in IDS] + [limit]).fetchall():
            out.append({"at": r[0], **{c: v for c, v in zip(IDS, r[1:-1]) if v is not None}, **json.loads(r[-1])})
        return out[::-1]

    def probe(self) -> None:
        """A write that proves the journal works again after a failure."""
        self.save_state("probe", {"at": dt.datetime.now(dt.timezone.utc).isoformat()})

    def _exec(self, sql: str, args: list) -> None:
        if self.fail_writes:
            raise JournalError("journal write failed (injected)")
        try:
            with self._lock:
                self._db.execute(sql, args)
        except sqlite3.Error as e:
            raise JournalError(f"journal write failed: {e}") from e

    def close(self) -> None:
        self._db.close()
