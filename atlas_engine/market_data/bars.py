"""Bid/ask bar frames.

The canonical bar frame is indexed by bar *open* time (UTC) and carries both
sides: ``bid_o bid_h bid_l bid_c ask_o ask_h ask_l ask_c volume``. Buys fill at
the ask and sells at the bid, so both sides are kept all the way to the
backtester instead of collapsing to a mid price.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pandas as pd

from . import dukascopy

SIDE_COLS = ["o", "h", "l", "c"]
BID = [f"bid_{c}" for c in SIDE_COLS]
ASK = [f"ask_{c}" for c in SIDE_COLS]
BAR_COLS = BID + ASK + ["volume"]


def merge_sides(bid: pd.DataFrame, ask: pd.DataFrame) -> pd.DataFrame:
    """Join one bid and one ask OHLCV frame and drop closed-market minutes."""
    rename = {"open": "o", "high": "h", "low": "l", "close": "c"}
    b = bid.rename(columns=rename)
    a = ask.rename(columns=rename)
    df = pd.DataFrame(index=b.index.union(a.index))
    for c in SIDE_COLS:
        df[f"bid_{c}"] = b[c]
        df[f"ask_{c}"] = a[c]
    df["volume"] = b["volume"].reindex(df.index).fillna(0) + a["volume"].reindex(df.index).fillna(0)
    df = df.dropna()
    # Dukascopy pads closed-market minutes with flat zero-volume candles.
    return df.loc[df["volume"] > 0, BAR_COLS]


def build_m1_from_dukascopy(symbol: str, start: dt.date, end: dt.date, cache_dir: Path) -> pd.DataFrame:
    """Assemble the M1 bid/ask frame for ``[start, end]`` from cached day files."""
    frames = []
    for day in pd.date_range(start, end, freq="D"):
        d = day.date()
        bid = dukascopy.load_side(symbol, d, "BID", cache_dir)
        ask = dukascopy.load_side(symbol, d, "ASK", cache_dir)
        if bid.empty or ask.empty:
            continue
        frames.append(merge_sides(bid, ask))
    if not frames:
        return empty_bars()
    df = pd.concat(frames)
    return df[~df.index.duplicated(keep="first")].sort_index()


def empty_bars() -> pd.DataFrame:
    return pd.DataFrame(columns=BAR_COLS, index=pd.DatetimeIndex([], tz="UTC", name="time"), dtype=float)


def resample(bars: pd.DataFrame, rule: str) -> pd.DataFrame:
    """Aggregate bid/ask bars to a coarser timeframe (e.g. ``"15min"``, ``"1h"``).

    Bars are labelled by open time. Periods with no underlying bars are
    dropped rather than forward-filled. Also emits spread statistics taken
    from the underlying bars' closes.
    """
    agg = {}
    for side in ("bid", "ask"):
        agg[f"{side}_o"] = "first"
        agg[f"{side}_h"] = "max"
        agg[f"{side}_l"] = "min"
        agg[f"{side}_c"] = "last"
    agg["volume"] = "sum"
    out = bars.resample(rule, label="left", closed="left").agg(agg)
    spread = (bars["ask_c"] - bars["bid_c"]).resample(rule, label="left", closed="left")
    out["spread_mean"] = spread.mean()
    out["spread_max"] = spread.max()
    out["n_sub"] = bars["bid_c"].resample(rule, label="left", closed="left").count()
    return out.loc[out["n_sub"] > 0]


def with_mid(bars: pd.DataFrame) -> pd.DataFrame:
    """Add ``open high low close`` mid-price columns used by indicators."""
    out = bars.copy()
    out["open"] = (bars["bid_o"] + bars["ask_o"]) / 2
    out["high"] = (bars["bid_h"] + bars["ask_h"]) / 2
    out["low"] = (bars["bid_l"] + bars["ask_l"]) / 2
    out["close"] = (bars["bid_c"] + bars["ask_c"]) / 2
    out["spread"] = bars["ask_c"] - bars["bid_c"]
    return out
