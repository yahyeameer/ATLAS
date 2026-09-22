"""Deterministic indicators. Every value at row ``t`` uses only rows ``<= t``."""

from __future__ import annotations

import numpy as np
import pandas as pd


def ema(x: pd.Series, length: int) -> pd.Series:
    return x.ewm(span=length, adjust=False, min_periods=length).mean()


def rma(x: pd.Series, length: int) -> pd.Series:
    """Wilder's moving average (alpha = 1/length), seeded after ``length`` values."""
    return x.ewm(alpha=1.0 / length, adjust=False, min_periods=length).mean()


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    prev = close.shift(1)
    return pd.concat([high - low, (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)


def atr(high: pd.Series, low: pd.Series, close: pd.Series, length: int = 14) -> pd.Series:
    return rma(true_range(high, low, close), length)


def adx(high: pd.Series, low: pd.Series, close: pd.Series, length: int = 14) -> pd.Series:
    up = high.diff()
    down = -low.diff()
    plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=high.index)
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=high.index)
    tr = rma(true_range(high, low, close), length)
    plus_di = 100 * rma(plus_dm, length) / tr
    minus_di = 100 * rma(minus_dm, length) / tr
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return rma(dx, length)


def slope(x: pd.Series, lookback: int) -> pd.Series:
    """Change over ``lookback`` rows, per row."""
    return (x - x.shift(lookback)) / lookback


def rolling_pct_rank(x: pd.Series, window: int, min_periods: int | None = None) -> pd.Series:
    """Percentile (0..1] of the current value within the trailing window."""
    return x.rolling(window, min_periods=min_periods or window // 2).rank(pct=True)
