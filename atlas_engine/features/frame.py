"""The shared feature frame used by setups in both backtest and live.

One row per M15 bar, indexed by bar open time. A row's features are what is
knowable at that bar's *close* (``index + 15min``), which is when setups
decide. Higher-timeframe values come from the last H1 bar that had closed by
then, never the one still forming.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from atlas_engine.market_data.bars import resample, with_mid

from . import indicators as ind
from . import sessions

M15 = pd.Timedelta(minutes=15)
H1 = pd.Timedelta(hours=1)


@dataclass(frozen=True)
class FeatureConfig:
    ema_fast: int = 20
    atr_len: int = 14
    h1_ema_fast: int = 20
    h1_ema_slow: int = 50
    h1_slope_lookback: int = 5
    adx_len: int = 14
    atr_pct_days: int = 60
    asia_start: str = "00:00"  # London clock
    asia_end: str = "07:00"


def align_closed(htf: pd.DataFrame, htf_period: pd.Timedelta, ltf_index: pd.DatetimeIndex, ltf_period: pd.Timedelta) -> pd.DataFrame:
    """For each lower-timeframe bar, the latest higher-timeframe row closed by its close."""
    closed = htf.copy()
    closed.index = closed.index + htf_period
    target = ltf_index + ltf_period
    merged = closed.reindex(closed.index.union(target)).ffill().reindex(target)
    merged.index = ltf_index
    return merged


def h1_features(m1: pd.DataFrame, cfg: FeatureConfig) -> pd.DataFrame:
    h1 = with_mid(resample(m1, "1h"))
    out = pd.DataFrame(index=h1.index)
    out["h1_close"] = h1["close"]
    out["h1_ema_fast"] = ind.ema(h1["close"], cfg.h1_ema_fast)
    out["h1_ema_slow"] = ind.ema(h1["close"], cfg.h1_ema_slow)
    out["h1_slope"] = ind.slope(out["h1_ema_slow"], cfg.h1_slope_lookback)
    out["h1_adx"] = ind.adx(h1["high"], h1["low"], h1["close"], cfg.adx_len)
    out["h1_atr"] = ind.atr(h1["high"], h1["low"], h1["close"], cfg.atr_len)
    return out


def build_features(m1: pd.DataFrame, cfg: FeatureConfig = FeatureConfig()) -> pd.DataFrame:
    f = with_mid(resample(m1, "15min"))
    idx = f.index
    close_time = idx + M15

    f["atr"] = ind.atr(f["high"], f["low"], f["close"], cfg.atr_len)
    f["ema_fast"] = ind.ema(f["close"], cfg.ema_fast)
    f["atr_pct"] = ind.rolling_pct_rank(f["atr"], cfg.atr_pct_days * 96)

    f = f.join(align_closed(h1_features(m1, cfg), H1, idx, M15))
    f["h1_trend"] = np.sign(f["h1_ema_fast"] - f["h1_ema_slow"]).fillna(0).astype(int)

    # Prior trading day's range (days close 17:00 New York).
    f["fx_day"] = sessions.fx_day(idx)
    daily = f.groupby("fx_day").agg(day_high=("high", "max"), day_low=("low", "min"))
    prev = daily.shift(1)
    f["pdh"] = f["fx_day"].map(prev["day_high"])
    f["pdl"] = f["fx_day"].map(prev["day_low"])

    # Asian range on the London clock, usable only once it has fully formed.
    f["london_min"] = sessions.local_minutes(idx, sessions.LONDON)
    f["london_date"] = sessions.local_date(idx, sessions.LONDON)
    asia = sessions.in_window(f["london_min"].to_numpy(), cfg.asia_start, cfg.asia_end)
    rng = f.loc[asia].groupby("london_date").agg(asia_high=("high", "max"), asia_low=("low", "min"))
    formed = f["london_min"] >= sessions._hhmm(cfg.asia_end)
    f["asia_high"] = f["london_date"].map(rng["asia_high"]).where(formed)
    f["asia_low"] = f["london_date"].map(rng["asia_low"]).where(formed)

    # Edge-protection flags evaluated at the decision time (bar close).
    f["blk_rollover"] = sessions.rollover_blackout(close_time)
    f["blk_after_open"] = sessions.after_open_blackout(close_time)
    f["close_time"] = close_time
    return f
