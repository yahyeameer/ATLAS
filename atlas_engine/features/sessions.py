"""Session and calendar math. UTC in, DST-aware local clocks out."""

from __future__ import annotations

import numpy as np
import pandas as pd

LONDON = "Europe/London"
NEW_YORK = "America/New_York"


def local_minutes(index: pd.DatetimeIndex, tz: str) -> np.ndarray:
    local = index.tz_convert(tz)
    return (local.hour * 60 + local.minute).to_numpy()


def local_date(index: pd.DatetimeIndex, tz: str) -> pd.Index:
    return pd.Index(index.tz_convert(tz).date)


def fx_day(index: pd.DatetimeIndex) -> pd.Index:
    """Trading day that closes at 17:00 New York (the broker server day at NY+7)."""
    shifted = index.tz_convert(NEW_YORK) + pd.Timedelta(hours=7)
    return pd.Index(shifted.date)


def in_window(minutes: np.ndarray, start: str, end: str) -> np.ndarray:
    """``start <= t < end`` for "HH:MM" clock strings; wraps past midnight."""
    s, e = (_hhmm(start), _hhmm(end))
    return (minutes >= s) & (minutes < e) if s <= e else (minutes >= s) | (minutes < e)


def _hhmm(s: str) -> int:
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def rollover_blackout(index: pd.DatetimeIndex, start: str = "16:45", end: str = "18:15") -> np.ndarray:
    return in_window(local_minutes(index, NEW_YORK), start, end)


def after_open_blackout(index: pd.DatetimeIndex, minutes: int = 15) -> np.ndarray:
    """First ``minutes`` after the London (08:00) and New York (08:00) opens, local time."""
    lon = local_minutes(index, LONDON)
    ny = local_minutes(index, NEW_YORK)
    return ((lon >= 480) & (lon < 480 + minutes)) | ((ny >= 480) & (ny < 480 + minutes))


def friday_cutoff(index: pd.DatetimeIndex, cutoff_utc: str) -> np.ndarray:
    """True from ``cutoff_utc`` on Friday until the weekend close."""
    mins = (index.hour * 60 + index.minute).to_numpy()
    return (index.weekday.to_numpy() == 4) & (mins >= _hhmm(cutoff_utc))


SESSIONS = ("asia", "london", "overlap", "new_york")


def session_label(index: pd.DatetimeIndex) -> np.ndarray:
    """London, London/New York overlap, New York, else Asia, on local clocks."""
    lon = local_minutes(index, LONDON)
    ny = local_minutes(index, NEW_YORK)
    return np.select(
        [(lon >= 480) & (ny < 480), (ny >= 480) & (lon < 16 * 60 + 30), (ny >= 480) & (ny < 17 * 60)],
        ["london", "overlap", "new_york"],
        default="asia",
    )
