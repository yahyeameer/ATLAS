"""Decompose positions into currency legs and find correlated bets (PRD §20).

A long EURUSD that risks 0.4% to its stop is +0.4% EUR and -0.4% USD; the
net per currency is capped. XAUUSD splits into a gold bucket (XAU) and a USD
leg. Pairs whose direction-adjusted 60-day correlation is above the
threshold count as one bet.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from dataclasses import dataclass

import numpy as np
import pandas as pd

from atlas_engine.sizing.lots import contract


@dataclass(frozen=True)
class Exposure:
    symbol: str
    direction: int  # +1 long, -1 short
    risk_pct: float  # % of equity lost at the stop (0 once the stop is at or past entry)


def legs(e: Exposure) -> dict[str, float]:
    spec = contract(e.symbol)
    return {spec.base: e.direction * e.risk_pct, spec.quote: -e.direction * e.risk_pct}


def currency_risk(exposures: list[Exposure]) -> dict[str, float]:
    """Net signed risk per currency, % of equity."""
    net: dict[str, float] = defaultdict(float)
    for e in exposures:
        for ccy, r in legs(e).items():
            net[ccy] += r
    return dict(net)


@dataclass(frozen=True)
class CorrelationMatrix:
    corr: pd.DataFrame  # symbol x symbol, daily-return correlation
    as_of: dt.date

    def get(self, a: str, b: str) -> float | None:
        if a == b:
            return 1.0
        if a in self.corr.index and b in self.corr.columns:
            v = self.corr.at[a, b]
            return None if pd.isna(v) else float(v)
        return None


def correlation_matrix(daily_close: pd.DataFrame, window_days: int = 60) -> CorrelationMatrix:
    """Correlation of daily log returns over the last ``window_days`` rows (one column per symbol)."""
    rets = np.log(daily_close).diff().dropna(how="all").tail(window_days)
    return CorrelationMatrix(rets.corr(), pd.Timestamp(daily_close.index[-1]).date())


def correlated_with(
    new: Exposure, open_: list[Exposure], matrix: CorrelationMatrix | None, threshold: float,
) -> tuple[list[Exposure], str]:
    """Open exposures that form one bet with ``new``, and which rule decided it.

    With a usable matrix: direction-adjusted correlation above ``threshold``.
    Without one (missing, stale, or the pair not in it) the conservative
    fallback applies: any position with a shared currency leg pointing the
    same way is treated as the same bet.
    """
    out, rule = [], "matrix" if matrix is not None else "shared_currency"
    new_legs = legs(new)
    for p in open_:
        c = matrix.get(new.symbol, p.symbol) if matrix is not None else None
        if c is not None:
            if c * new.direction * p.direction > threshold:
                out.append(p)
            continue
        if matrix is not None:
            rule = "matrix+shared_currency"
        p_legs = legs(p)
        if any(ccy in p_legs and np.sign(r) == np.sign(p_legs[ccy]) for ccy, r in new_legs.items()):
            out.append(p)
    return out, rule
