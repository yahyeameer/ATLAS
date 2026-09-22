"""Trend pullback: H1 trend, M15 pullback into the fast EMA, resumption entry."""

from __future__ import annotations

import pandas as pd

from .base import Setup, emit, structure_stop

DEFAULTS = {
    "adx_min": 20.0,
    "pullback_bars": 6,
    "sl_buffer_atr": 0.2,
    "sl_min_atr": 1.0,
    "sl_max_atr": 1.5,
}


def detect(f: pd.DataFrame, p: dict) -> pd.DataFrame:
    n = int(p["pullback_bars"])
    out = []
    for d in (1, -1):
        trend = (f["h1_trend"] == d) & (d * f["h1_slope"] > 0) & (f["h1_adx"] >= p["adx_min"])
        if d == 1:
            touched = (f["low"] - f["ema_fast"]).rolling(n).min() <= 0
            resume = (f["close"] > f["ema_fast"]) & (f["close"] > f["high"].shift(1)) & (f["close"] > f["open"])
            swing = f["low"].rolling(n).min()
        else:
            touched = (f["high"] - f["ema_fast"]).rolling(n).max() >= 0
            resume = (f["close"] < f["ema_fast"]) & (f["close"] < f["low"].shift(1)) & (f["close"] < f["open"])
            swing = f["high"].rolling(n).max()
        fresh = resume & ~resume.shift(1, fill_value=False)
        stop = structure_stop(f["close"], swing, d, f["atr"], p["sl_buffer_atr"], p["sl_min_atr"], p["sl_max_atr"])
        out.append(emit(f, trend & touched & fresh, d, stop))
    return pd.concat(out, ignore_index=True)


SETUP = Setup("trend_pullback", "0.1.0", detect, DEFAULTS)
