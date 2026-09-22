"""Session breakout: the Asian range broken around the London open.

``mode="momentum"`` enters on the first M15 close outside the range;
``mode="retest"`` waits for price to come back to the broken edge and close
beyond it again. Stops sit beyond the broken edge.
"""

from __future__ import annotations

import pandas as pd

from atlas_engine.features import sessions

from .base import Setup, emit, first_per_day, structure_stop

DEFAULTS = {
    "mode": "momentum",
    "entry_start": "07:00",  # London clock
    "entry_end": "10:00",
    "range_min_h1atr": 1.0,
    "range_max_h1atr": 4.0,
    "sl_buffer_atr": 0.5,
    "sl_min_atr": 1.0,
    "sl_max_atr": 1.5,
}


def detect(f: pd.DataFrame, p: dict) -> pd.DataFrame:
    window = pd.Series(sessions.in_window(f["london_min"].to_numpy(), p["entry_start"], p["entry_end"]), index=f.index)
    width = (f["asia_high"] - f["asia_low"]) / f["h1_atr"]
    ok = window & width.between(p["range_min_h1atr"], p["range_max_h1atr"])
    day = f["london_date"]
    out = []
    for d in (1, -1):
        level = f["asia_high"] if d == 1 else f["asia_low"]
        beyond = d * (f["close"] - level) > 0
        if p["mode"] == "momentum":
            trigger = beyond
        elif p["mode"] == "retest":
            broke_before = (beyond & ok).astype(int).groupby(day).cumsum().shift(1, fill_value=0) > 0
            touched = (f["low"] <= level) if d == 1 else (f["high"] >= level)
            trigger = broke_before & touched & beyond
        else:
            raise ValueError(f"unknown mode {p['mode']!r}")
        mask = first_per_day(trigger & ok, day)
        stop = structure_stop(f["close"], level, d, f["atr"], p["sl_buffer_atr"], p["sl_min_atr"], p["sl_max_atr"])
        out.append(emit(f, mask, d, stop))
    return pd.concat(out, ignore_index=True)


SETUP = Setup("session_breakout", "0.1.0", detect, DEFAULTS, session_based=True)
