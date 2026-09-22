"""Liquidity sweep reversal: prior-day high/low swept, bar closes back inside.

PRD §16 allows XAUUSD and GBPUSD only after separate validation, so the T0
config runs this on EURUSD by default.
"""

from __future__ import annotations

import pandas as pd

from atlas_engine.features import sessions

from .base import Setup, emit, first_per_day, structure_stop

DEFAULTS = {
    "entry_start": "07:00",  # London clock
    "entry_end": "16:00",
    "min_sweep_atr": 0.0,
    "sl_buffer_atr": 0.2,
    "sl_min_atr": 1.0,
    "sl_max_atr": 1.5,
}


def detect(f: pd.DataFrame, p: dict) -> pd.DataFrame:
    window = pd.Series(sessions.in_window(f["london_min"].to_numpy(), p["entry_start"], p["entry_end"]), index=f.index)
    out = []
    for d in (1, -1):
        # d = +1 is a long after a sweep of the prior-day low; -1 a short after the high.
        level = f["pdl"] if d == 1 else f["pdh"]
        extreme = f["low"] if d == 1 else f["high"]
        swept = d * (level - extreme) > p["min_sweep_atr"] * f["atr"]
        back_inside = d * (f["close"] - level) > 0
        mask = first_per_day(window & swept & back_inside, f["fx_day"])
        stop = structure_stop(f["close"], extreme, d, f["atr"], p["sl_buffer_atr"], p["sl_min_atr"], p["sl_max_atr"])
        out.append(emit(f, mask, d, stop))
    return pd.concat(out, ignore_index=True)


SETUP = Setup("liquidity_sweep", "0.1.0", detect, DEFAULTS)
