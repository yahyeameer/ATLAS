"""Parquet store for M1 bid/ask bars: ``<root>/m1/<SYMBOL>/<YEAR>.parquet``."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from .bars import BAR_COLS, empty_bars


def year_path(root: Path, symbol: str, year: int) -> Path:
    return Path(root) / "m1" / symbol.upper() / f"{year}.parquet"


def save_m1(root: Path, symbol: str, bars: pd.DataFrame) -> list[Path]:
    """Write bars split by UTC year, replacing each touched year's file."""
    written = []
    for year, chunk in bars.groupby(bars.index.year):
        path = year_path(root, symbol, int(year))
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            old = pd.read_parquet(path)
            chunk = pd.concat([old[~old.index.isin(chunk.index)], chunk]).sort_index()
        chunk[BAR_COLS].to_parquet(path)
        written.append(path)
    return written


def load_m1(root: Path, symbol: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    """Load bars with open time in ``[start, end)``."""
    start, end = pd.Timestamp(start), pd.Timestamp(end)
    start = start.tz_localize("UTC") if start.tzinfo is None else start
    end = end.tz_localize("UTC") if end.tzinfo is None else end
    frames = []
    for year in range(start.year, end.year + 1):
        path = year_path(root, symbol, year)
        if path.exists():
            frames.append(pd.read_parquet(path))
    if not frames:
        raise FileNotFoundError(f"no M1 data for {symbol} in {root}; run `atlas-research data build`")
    df = pd.concat(frames).sort_index()
    df = df.loc[(df.index >= start) & (df.index < end)]
    return df if not df.empty else empty_bars()
