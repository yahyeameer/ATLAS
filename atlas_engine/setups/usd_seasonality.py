"""US-hours dollar seasonality: short the dollar through the US trading day.

Breedon and Ranaldo (2013) find that currencies tend to depreciate during
their own country's business hours. For XXXUSD pairs that means a long from
``entry_hour`` to ``exit_hour`` UTC on weekdays. Decided on H1 bars; the trade
leaves at ``exit_hour`` unless the stop or the exit policy's target comes
first. Time-based, so it is exempt from the after-open blackout.
"""

from __future__ import annotations

import pandas as pd

from .base import Setup, emit

DEFAULTS = {
    "entry_hour": 13,  # UTC, decision at this bar close
    "exit_hour": 20,  # UTC, same day
    "sl_atr": 1.5,
}


def detect(f: pd.DataFrame, p: dict) -> pd.DataFrame:
    close_time = pd.DatetimeIndex(f["close_time"])
    entry_hour, exit_hour = int(p["entry_hour"]), int(p["exit_hour"])
    if exit_hour <= entry_hour:  # e.g. a ±20% neighbour that crosses over: no trades
        return emit(f, pd.Series(False, index=f.index), 1, f["close"])
    mask = pd.Series((close_time.hour == entry_hour) & (close_time.minute == 0) & (close_time.weekday < 5), index=f.index)
    stop = f["close"] - p["sl_atr"] * f["atr"]
    sig = emit(f, mask, 1, stop)
    sig["exit_by"] = pd.DatetimeIndex(sig["decision_time"]).normalize() + pd.Timedelta(hours=exit_hour)
    return sig


SETUP = Setup("usd_seasonality", "0.1.0", detect, DEFAULTS, session_based=True)
