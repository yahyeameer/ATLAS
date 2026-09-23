import datetime as dt
import shutil
from pathlib import Path

import pytest
import yaml

from atlas_engine.config import load_engine_config
from atlas_engine.risk import AccountSnapshot, OpenPosition, RiskEngine, TradeProposal

REPO_CONFIG = Path(__file__).resolve().parents[2] / "config"
UTC = dt.timezone.utc
# Tuesday 2026-09-22 10:00 UTC = 12:00 in Prague (CEST), mid server day.
T = dt.datetime(2026, 9, 22, 10, 0, tzinfo=UTC)


@pytest.fixture
def cfg():
    return load_engine_config(REPO_CONFIG)


@pytest.fixture
def config_dir(tmp_path):
    """A writable copy of the repo config; ``edit(file, {dotted.key: value})`` changes it."""
    d = tmp_path / "config"
    shutil.copytree(REPO_CONFIG, d)

    def edit(name: str, changes: dict):
        p = d / name
        data = yaml.safe_load(p.read_text())
        for key, value in changes.items():
            target = data
            parts = key.split(".")
            for part in parts[:-1]:
                target = target.setdefault(part, {})
            if value is _DELETE:
                target.pop(parts[-1], None)
            else:
                target[parts[-1]] = value
        p.write_text(yaml.safe_dump(data))
        return d

    edit.dir = d
    edit.DELETE = _DELETE
    return edit


_DELETE = object()


@pytest.fixture
def engine(cfg):
    return RiskEngine(cfg)


def snap(balance=10_000.0, equity=None, positions=(), time=T):
    return AccountSnapshot(time, balance, balance if equity is None else equity, tuple(positions))


def proposal(symbol="EURUSD", setup="trend_pullback", direction=1, entry=1.1000, stop_pips=15.0, **kw):
    pip = 0.01 if symbol.endswith("JPY") else 0.0001
    stop = entry - direction * stop_pips * pip
    kw.setdefault("rates", {"USDJPY": 150.0})
    return TradeProposal(symbol, setup, direction, entry, stop, **kw)


def position(symbol="EURUSD", setup="other", direction=1, risk=40.0, ticket="1"):
    return OpenPosition(ticket, symbol, setup, direction, 0.25, 1.1, 1.0985, risk)
