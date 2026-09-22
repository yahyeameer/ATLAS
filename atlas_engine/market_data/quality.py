"""Data-quality checks for M1 bid/ask frames (owned by the data-engineer role)."""

from __future__ import annotations

import pandas as pd

from .symbols import spec


def _in_market_hours(index: pd.DatetimeIndex) -> pd.Series:
    """True from Sunday 22:00 to Friday 21:00 UTC, excluding the 21:00–22:30 rollover hour."""
    wd, hour, minute = index.weekday, index.hour, index.minute
    mins = hour * 60 + minute
    weekend = (wd == 5) | ((wd == 4) & (mins >= 21 * 60)) | ((wd == 6) & (mins < 22 * 60 + 30))
    rollover = (mins >= 20 * 60 + 45) & (mins < 22 * 60 + 30)
    return pd.Series(~weekend & ~rollover, index=index)


def quality_report(bars: pd.DataFrame, symbol: str, gap_minutes: int = 5) -> pd.DataFrame:
    """Per-year summary: coverage, gaps, crossed quotes, OHLC violations, spreads in pips."""
    pip = spec(symbol).pip
    rows = []
    for year, df in bars.groupby(bars.index.year):
        idx = df.index
        step = idx.to_series().diff().dt.total_seconds().div(60)
        in_hours = _in_market_hours(idx)
        gaps = step[(step > gap_minutes) & in_hours.shift(1, fill_value=False) & in_hours]
        spread = (df["ask_c"] - df["bid_c"]) / pip
        bad_ohlc = 0
        for side in ("bid", "ask"):
            o, h, l, c = (df[f"{side}_{x}"] for x in "ohlc")
            bad_ohlc += int(((l > o.combine(c, min)) | (h < o.combine(c, max))).sum())
        rows.append(
            {
                "year": int(year),
                "bars": len(df),
                "first": idx.min(),
                "last": idx.max(),
                "duplicates": int(idx.duplicated().sum()),
                f"gaps_gt_{gap_minutes}m": len(gaps),
                "max_gap_min": float(gaps.max()) if len(gaps) else 0.0,
                "crossed_quotes": int((df["ask_l"] < df["bid_l"]).sum() + (spread < 0).sum()),
                "ohlc_violations": bad_ohlc,
                "spread_median_pips": float(spread.median()),
                "spread_p99_pips": float(spread.quantile(0.99)),
                "spread_max_pips": float(spread.max()),
            }
        )
    return pd.DataFrame(rows).set_index("year") if rows else pd.DataFrame()
