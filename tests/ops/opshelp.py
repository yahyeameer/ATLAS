"""Fixtures and helpers shared by the ops tests.

Not a conftest.py: tests/mcp imports its own conftest by module name, and a
second conftest module would shadow it. Test modules import the fixtures below.
"""

from __future__ import annotations

import datetime as dt

import pytest

from atlas_api.auth import TokenStore, hash_token
from atlas_api.ops import ENGINE_SCOPES
from atlas_engine.ops.sim import SimulatedEngine

T0 = dt.datetime(2026, 9, 23, 14, 0, tzinfo=dt.timezone.utc)


class Clock:
    def __init__(self, t: dt.datetime = T0):
        self.t = t

    def __call__(self) -> dt.datetime:
        return self.t

    def advance(self, **kw) -> None:
        self.t += dt.timedelta(**kw)


def engine_token(scopes) -> str:
    return "etok-" + "-".join(sorted(scopes)) if scopes else "etok-none"


ENGINE_TOKEN_SCOPES = [[s] for s in ENGINE_SCOPES] + [sorted(ENGINE_SCOPES), []]


@pytest.fixture()
def clock():
    return Clock()


@pytest.fixture()
def engine(tmp_path, clock):
    e = SimulatedEngine(tmp_path / "engine" / "sim.json", now=clock)
    e.init()
    return e


@pytest.fixture()
def ops_dir(tmp_path, monkeypatch):
    d = tmp_path / "ops"
    monkeypatch.setenv("ATLAS_OPS_DIR", str(d))
    for var in ("HERMES_PROFILE", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK"):
        monkeypatch.delenv(var, raising=False)
    return d


@pytest.fixture()
def engine_tokens():
    return TokenStore([{"name": engine_token(c), "sha256": hash_token(engine_token(c)), "scopes": c}
                       for c in ENGINE_TOKEN_SCOPES], ENGINE_SCOPES)
