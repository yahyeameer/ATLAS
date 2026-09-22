"""Synthetic M1 bid/ask data for tests and pipeline dry runs.

The default series is a driftless random walk with intraday volatility
seasonality, a variable spread that widens around the New York rollover, and
weekend closures. It has no edge by construction, so any setup that passes
the T0 gates on it is a bug in the research stack.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .bars import BAR_COLS
from .symbols import spec


def market_minutes(start: str, end: str) -> pd.DatetimeIndex:
    """UTC minutes from Sunday 22:00 to Friday 21:00, the usual FX week."""
    idx = pd.date_range(start, end, freq="1min", inclusive="left", tz="UTC")
    wd, mins = idx.weekday, idx.hour * 60 + idx.minute
    closed = (wd == 5) | ((wd == 4) & (mins >= 21 * 60)) | ((wd == 6) & (mins < 22 * 60))
    return idx[~closed]


def random_walk_m1(
    symbol: str = "EURUSD",
    start: str = "2019-01-01",
    end: str = "2019-04-01",
    seed: int = 7,
    start_price: float = 1.15,
    annual_vol: float = 0.07,
    spread_pips: float = 0.2,
    momentum: float = 0.0,
    wick: float = 0.3,
) -> pd.DataFrame:
    """Generate M1 bid/ask bars.

    ``momentum`` is an AR(1) coefficient on 15-minute returns; a positive
    value plants a trend-continuation edge (used to check the stack can find
    an edge that exists). ``wick`` scales the random extension of each bar's
    high and low beyond its open/close, in units of the minute's volatility.
    """
    rng = np.random.default_rng(seed)
    idx = market_minutes(start, end)
    n = len(idx)
    pip = spec(symbol).pip

    hour = idx.hour.to_numpy()
    season = np.where((hour >= 7) & (hour < 17), 1.4, 0.7)  # London/NY busier than Asia
    sigma = annual_vol / np.sqrt(252 * 1440) * season
    shocks = rng.standard_normal(n) * sigma
    if momentum:
        # AR(1) on 15-minute block returns, spread evenly across the block's minutes.
        blocks = np.arange(n) // 15
        block_ret = np.bincount(blocks, weights=shocks)
        ar = np.empty_like(block_ret)
        prev = 0.0
        for i, r in enumerate(block_ret):
            prev = momentum * prev + r
            ar[i] = prev
        counts = np.bincount(blocks)
        shocks = (ar / counts)[blocks] + (shocks - (block_ret / counts)[blocks])
    close = start_price * np.exp(np.cumsum(shocks))
    open_ = np.concatenate([[start_price], close[:-1]])
    wicks = np.abs(rng.standard_normal((2, n))) * sigma * start_price * wick
    high = np.maximum(open_, close) + wicks[0]
    low = np.minimum(open_, close) - wicks[1]

    mins = hour * 60 + idx.minute.to_numpy()
    rollover = (mins >= 20 * 60 + 45) & (mins < 22 * 60 + 15)
    spread = spread_pips * pip * np.exp(0.25 * rng.standard_normal(n)) * np.where(rollover, 6.0, 1.0)
    half = spread / 2

    df = pd.DataFrame(index=pd.DatetimeIndex(idx, name="time"))
    for c, mid in zip("ohlc", (open_, high, low, close)):
        df[f"bid_{c}"] = mid - half
        df[f"ask_{c}"] = mid + half
    df["volume"] = rng.integers(20, 400, n).astype(float)
    return df[BAR_COLS]
