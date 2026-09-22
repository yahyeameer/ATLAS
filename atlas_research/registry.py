"""Append-only experiment registry (PRD §13, §15).

Every T0 run is recorded here, failures included, before its gates are
reported. The deflated Sharpe ratio reads the trial count and the spread of
trial Sharpes from this file, so deleting entries overstates any edge.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path


class BudgetExceeded(RuntimeError):
    pass


class Registry:
    def __init__(self, path: Path):
        self.path = Path(path)

    def entries(self, strategy: str | None = None) -> list[dict]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text().splitlines():
            if line.strip():
                e = json.loads(line)
                if strategy is None or e["strategy"] == strategy:
                    out.append(e)
        return out

    def trial_sharpes(self, strategy: str) -> list[float]:
        return [s for e in self.entries(strategy) for s in e.get("trial_sharpes", [])]

    def experiments_in_month(self, strategy: str, when: dt.datetime) -> int:
        month = when.strftime("%Y-%m")
        return sum(1 for e in self.entries(strategy) if e["created_at"].startswith(month))

    def check_budget(self, strategy: str, limit: int, when: dt.datetime) -> None:
        used = self.experiments_in_month(strategy, when)
        if used >= limit:
            raise BudgetExceeded(f"{strategy}: {used} experiments already run in {when:%Y-%m} (budget {limit})")

    def append(self, entry: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as fh:
            fh.write(json.dumps(entry, sort_keys=True, default=str) + "\n")
