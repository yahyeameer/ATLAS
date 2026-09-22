"""Shared fixtures: a research service over synthetic data with a short, fixed calendar.

Calendar: dev 2019-01-01..2020-07-01, validation ..2021-01-01, holdout from
2021-01-01. The loader only has data up to the holdout and records every
request, so tests can assert the holdout was never asked for.
"""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from atlas_api.auth import SCOPES, TokenStore, hash_token
from atlas_api.service import ResearchService
from atlas_engine.market_data import synthetic
from atlas_research.cli import DEFAULT_CONFIG, load_config
from atlas_research.registry import Registry

HOLDOUT = pd.Timestamp("2021-01-01", tz="UTC")
NOW = dt.datetime(2026, 9, 22, 12, 0, tzinfo=dt.timezone.utc)


@pytest.fixture(scope="session")
def m1_data():
    return {
        "EURUSD": synthetic.random_walk_m1("EURUSD", "2019-01-01", "2021-01-01", seed=11),
        "GBPUSD": synthetic.random_walk_m1("GBPUSD", "2019-01-01", "2021-01-01", seed=12, start_price=1.30),
    }


@pytest.fixture()
def cfg():
    c = load_config(DEFAULT_CONFIG)
    c["segments"] = {"dev": ["2019-01-01", "2020-07-01"], "validation": ["2020-07-01", "2021-01-01"],
                     "holdout_start": "2021-01-01"}
    c["walk_forward"] = {"train_months": 9, "test_months": 3, "min_train_trades": 5}
    c["gates"]["mc_sims"] = 500
    c["gates"]["random_control_runs"] = 5
    c["budget"]["experiments_per_strategy_per_month"] = 3
    return c


class SpyLoader:
    """Serves synthetic M1 rows and records every (symbol, start, end) asked for."""

    def __init__(self, data: dict[str, pd.DataFrame]):
        self.data = data
        self.calls: list[tuple[str, pd.Timestamp, pd.Timestamp]] = []

    def __call__(self, symbol, start, end):
        self.calls.append((symbol, pd.Timestamp(start), pd.Timestamp(end)))
        m1 = self.data[symbol]
        return m1.loc[(m1.index >= start) & (m1.index < end)]

    def max_end(self) -> pd.Timestamp | None:
        return max((c[2] for c in self.calls), default=None)


@pytest.fixture()
def loader(m1_data):
    return SpyLoader(m1_data)


@pytest.fixture()
def service(cfg, loader, tmp_path):
    return ResearchService(cfg, loader, Registry(tmp_path / "experiments.jsonl"), tmp_path / "runs", now=lambda: NOW)


def token_for(scopes) -> str:
    return "tok-" + "-".join(sorted(scopes)) if scopes else "tok-none"


@pytest.fixture()
def tokens():
    """One token per single scope, one with all scopes, and one with none."""
    combos = [[s] for s in SCOPES] + [sorted(SCOPES), []]
    return TokenStore([{"name": token_for(c), "sha256": hash_token(token_for(c)), "scopes": c} for c in combos])
