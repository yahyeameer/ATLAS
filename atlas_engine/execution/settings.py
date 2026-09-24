"""Execution and filter settings from ``config/atlas.yaml`` (PRD §27 ``execution:`` and ``filters:``).

Both sections are optional. When a key is missing the PRD's default applies,
and ``status`` reports which defaults are in force, so adding the sections to
``atlas.yaml`` stays an operator decision (a signed commit). Unknown keys are
refused, like every other engine config.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from pathlib import Path

import yaml

from atlas_engine.config import ConfigError

EXECUTION_KEYS = {"max_deviation_points", "reconcile_interval_s", "magic_base", "orphan_policy", "server_tz",
                  "server_offset_hours", "request_budget_share", "require_watchdog", "watchdog_heartbeat_file",
                  "commission_per_lot"}
FILTER_KEYS = {"max_spread_to_stop_ratio", "news_blackout_min", "rollover_blackout_ny", "friday_flatten_utc"}
ORPHAN_POLICIES = {"close", "attach_sl"}


@dataclass(frozen=True)
class ExecutionSettings:
    max_deviation_points: int = 2  # §21: deviation <= 2 points on majors
    reconcile_interval_s: int = 60  # §21: startup + every 60 s
    magic_base: int = 26_090_000  # strategy magic numbers are magic_base + a per-strategy offset
    orphan_policy: str = "close"  # §21: an unknown ATLAS position is closed (or gets an SL attached)
    server_tz: str = "America/New_York"  # broker server clock = this zone + server_offset_hours
    server_offset_hours: float = 7.0
    request_budget_share: float = 0.90  # stop new entries at this share of the firm's daily request cap
    require_watchdog: bool = True  # a missing watchdog heartbeat is a HALT
    watchdog_heartbeat_file: str | None = None  # the EA's heartbeat file (MT5 Common\Files)
    commission_per_lot: dict | None = None  # round turn, account currency; per symbol
    max_spread_to_stop_ratio: float = 0.20  # §16
    news_blackout_min: tuple[int, int] = (10, 15)  # §16, needs a calendar feed (not wired yet)
    rollover_blackout_ny: tuple[str, str] = ("16:45", "18:15")  # §16
    friday_flatten_utc: str | None = "20:00"  # §18 session exit
    defaults_used: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return asdict(self)


def load_execution_settings(root: str | Path = "config") -> ExecutionSettings:
    path = Path(root) / "atlas.yaml"
    raw = yaml.safe_load(path.read_text()) or {}
    ex, fl = raw.get("execution") or {}, raw.get("filters") or {}
    for section, allowed, name in ((ex, EXECUTION_KEYS, "execution"), (fl, FILTER_KEYS, "filters")):
        if not isinstance(section, dict):
            raise ConfigError(f"{path}: {name} must be a mapping")
        extra = set(section) - allowed
        if extra:
            raise ConfigError(f"{path}: unknown key(s) {', '.join(f'{name}.{k}' for k in sorted(extra))}")
    given = {**ex, **fl}
    kw = {}
    for f in fields(ExecutionSettings):
        if f.name in given:
            v = given[f.name]
            kw[f.name] = tuple(v) if isinstance(v, list) else v
    kw["defaults_used"] = tuple(sorted((EXECUTION_KEYS | FILTER_KEYS) - set(given)))
    s = ExecutionSettings(**kw)
    checks = [
        (0 <= s.max_deviation_points <= 20, "execution.max_deviation_points must be in 0..20"),
        (10 <= s.reconcile_interval_s <= 300, "execution.reconcile_interval_s must be in 10..300"),
        (s.orphan_policy in ORPHAN_POLICIES, f"execution.orphan_policy must be one of {sorted(ORPHAN_POLICIES)}"),
        (0 < s.request_budget_share <= 1, "execution.request_budget_share must be in (0, 1]"),
        (0 < s.max_spread_to_stop_ratio <= 0.5, "filters.max_spread_to_stop_ratio must be in (0, 0.5]"),
    ]
    bad = [m for ok, m in checks if not ok]
    if bad:
        raise ConfigError(f"{path}: " + "; ".join(bad))
    return s
