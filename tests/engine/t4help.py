"""Builders shared by the T4 tests and the T4 gate: an engine on a fake MT5 terminal.

Not a conftest.py, so the gate script can import it without pytest.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from pathlib import Path

from atlas_engine.adapters.mt5 import MT5Adapter
from atlas_engine.adapters.mt5.fake import FakeMT5
from atlas_engine.alerts import AlertOutbox
from atlas_engine.config import load_engine_config
from atlas_engine.execution import ExecutionSettings
from atlas_engine.journal import Journal
from atlas_engine.operator import sign
from atlas_engine.runtime import TradingEngine
from atlas_engine.strategies import Signal

REPO_CONFIG = Path(__file__).resolve().parents[2] / "config"
UTC = dt.timezone.utc
# Tuesday 2026-09-22 10:00 UTC: London session, mid server day in Prague.
T0 = dt.datetime(2026, 9, 22, 10, 0, tzinfo=UTC)
KEY = bytes(range(32))


class Clock:
    def __init__(self, t: dt.datetime = T0):
        self.t = t

    def __call__(self) -> dt.datetime:
        return self.t

    def advance(self, **kw) -> None:
        self.t += dt.timedelta(**kw)


@dataclass
class Rig:
    engine: TradingEngine
    mt5: FakeMT5
    adapter: MT5Adapter
    journal: Journal
    clock: Clock
    root: Path
    watchdog_line: list
    config_root: Path = REPO_CONFIG

    def step(self, seconds: float = 1.0, touch: bool = True):
        """Advance the clock, keep the watchdog and the quotes alive, and run one engine step."""
        self.clock.advance(seconds=seconds)
        if touch:
            self.mt5.touch()
        self.beat()
        return self.engine.step()

    def beat(self, state: str = "OK") -> None:
        info = self.mt5.account_info()
        eq = info.equity if info is not None else self.mt5.balance
        self.watchdog_line[0] = f"ATLAS-WD 1 {int(self.clock().timestamp())} {eq:.2f} {state}"

    def operator(self, action: str, reason: str = "operator drill in the test suite", key: bytes = KEY,
                 at: dt.datetime | None = None) -> dict:
        cmd = sign(key, action, "yahye", reason, at or self.clock())
        path = self.engine.inbox / f"{cmd['nonce']}.json"
        path.write_text(json.dumps(cmd))
        self.step()
        return cmd

    def enable(self) -> None:
        self.operator("enable_trading")
        assert self.engine.trading["enabled"], self.engine.events()["events"][-3:]

    def signal(self, symbol: str = "EURUSD", direction: int = 1, stop_pips: float = 15, setup: str = "trend_pullback",
               minute: int | None = None) -> Signal:
        tick = self.adapter.tick(symbol)
        entry = tick.ask if direction == 1 else tick.bid
        pip = 0.01 if symbol.endswith("JPY") else 0.0001
        at = self.clock().replace(second=0, microsecond=0)
        if minute is not None:
            at = at.replace(minute=minute)
        return Signal(symbol, setup, "1", direction, round(entry - direction * stop_pips * pip, 5), at)

    def events(self, kind: str | None = None) -> list[dict]:
        ev = self.engine.events(0, 500)["events"]
        return [e for e in ev if kind is None or e["kind"] == kind]

    def state(self) -> str:
        return self.engine.health()["state"]

    def restart(self) -> "Rig":
        """A new engine process on the same terminal, journal and state directory."""
        self.journal.close()
        return build(self.root, self.clock, mt5=self.mt5, watchdog_line=self.watchdog_line, start=True,
                     settings=self.engine.settings, config_root=self.config_root)


def build(root: Path, clock: Clock | None = None, *, mt5: FakeMT5 | None = None, settings: ExecutionSettings | None = None,
          config_root: Path = REPO_CONFIG, start: bool = True, watchdog_line: list | None = None,
          sources: list | None = None, adapter_clock=None) -> Rig:
    clock = clock or Clock()
    cfg = load_engine_config(config_root)
    mt5 = mt5 or FakeMT5(clock, symbols=cfg.symbols)
    adapter = MT5Adapter(mt5, adapter_clock)
    journal = Journal(root / "journal.db", "run-test")
    line = watchdog_line or [""]
    engine = TradingEngine(cfg, settings or ExecutionSettings(), adapter, journal, root / "state", now=clock,
                           alerts=AlertOutbox(root / "alerts.jsonl", senders=[]), operator_key=KEY,
                           watchdog=lambda: line[0], broker_label="fake-mt5", sources=sources)
    rig = Rig(engine, mt5, adapter, journal, clock, root, line, config_root)
    if start:
        rig.beat()
        engine.start()
    return rig
