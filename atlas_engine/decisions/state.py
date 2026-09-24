"""Compact, leakage-safe decision state for a setup signal (PRD §16, §17).

The same code builds the state in research (one row per candidate signal) and
live (one row for the signal just decided), so the selection models see
identical inputs in both. Every value is what is known at the signal's
decision time, taken from the shared M15 feature frame.

Leakage rule (§17): the state carries no absolute dates, timestamps, price
levels or symbol names. Prices appear only as distances in ATR or R, signed
so that positive means "in the trade's favour", and time only as a session
label. ``payload`` returns exactly those fields; anything else a caller keeps
alongside (decision time, symbol) is for joining and never reaches a model.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from atlas_engine.features import sessions

# Fixed regime labels (§16 regime filter, §17 Jev choice question).
TRENDS = ("trend", "range")
VOLS = ("low", "normal", "high")
REGIMES = tuple(f"{t}_{v}_vol" for t in TRENDS for v in VOLS)

NUMERIC = (
    "stop_atr",
    "spread_r",
    "dist_ema_atr",
    "h1_fast_dist",
    "h1_slow_dist",
    "h1_slope_atr",
    "h1_adx",
    "htf_aligned",
    "atr_pct",
    "atr_ratio",
    "room_prior_day_atr",
    "behind_prior_day_atr",
    "ret_1_atr",
    "ret_4_atr",
    "ret_16_atr",
)
CATEGORICAL = {"session": sessions.SESSIONS, "regime": REGIMES}
PAYLOAD_KEYS = (*NUMERIC, "session", "regime", "setup")

RETURN_LAGS = (1, 4, 16)


def regime_label(h1_adx, atr_pct, adx_trend: float = 25.0, low_vol: float = 0.33, high_vol: float = 0.80) -> np.ndarray:
    """Rule-based regime: trend when H1 ADX ≥ ``adx_trend``, volatility bucket from the ATR percentile."""
    adx = np.asarray(h1_adx, float)
    pct = np.asarray(atr_pct, float)
    trend = np.where(adx >= adx_trend, "trend", "range")
    vol = np.select([pct < low_vol, pct > high_vol], ["low", "high"], default="normal")
    return np.char.add(np.char.add(trend.astype(str), "_"), np.char.add(vol.astype(str), "_vol"))


def with_returns(features: pd.DataFrame) -> pd.DataFrame:
    """Add unsigned close-to-close returns in ATR over ``RETURN_LAGS`` M15 bars (causal)."""
    f = features.copy()
    for k in RETURN_LAGS:
        f[f"_ret_{k}"] = (f["close"] - f["close"].shift(k)) / f["atr"]
    return f


def state_frame(features: pd.DataFrame, signals: pd.DataFrame) -> pd.DataFrame:
    """One state row per signal, aligned to ``signals``' order.

    ``features`` is the shared M15 frame (optionally already passed through
    ``with_returns``); ``signals`` has the setup columns (decision_time,
    direction, stop, spread, setup). Signals whose decision bar is missing get
    NaN numerics and are left for the caller to drop.
    """
    f = features if "_ret_1" in features.columns else with_returns(features)
    row = f.set_index("close_time").reindex(pd.DatetimeIndex(signals["decision_time"]))
    d = signals["direction"].to_numpy(float)
    close = row["close"].to_numpy()
    atr = row["atr"].to_numpy()
    h1_atr = row["h1_atr"].to_numpy()
    stop_dist = np.abs(close - signals["stop"].to_numpy(float))

    out = pd.DataFrame(index=signals.index)
    out["stop_atr"] = stop_dist / atr
    out["spread_r"] = signals["spread"].to_numpy(float) / stop_dist
    out["dist_ema_atr"] = d * (close - row["ema_fast"].to_numpy()) / atr
    out["h1_fast_dist"] = d * (close - row["h1_ema_fast"].to_numpy()) / h1_atr
    out["h1_slow_dist"] = d * (close - row["h1_ema_slow"].to_numpy()) / h1_atr
    out["h1_slope_atr"] = d * row["h1_slope"].to_numpy() / h1_atr
    out["h1_adx"] = row["h1_adx"].to_numpy()
    out["htf_aligned"] = d * row["h1_trend"].to_numpy(float)
    out["atr_pct"] = row["atr_pct"].to_numpy()
    out["atr_ratio"] = atr / h1_atr
    pdh, pdl = row["pdh"].to_numpy(), row["pdl"].to_numpy()
    out["room_prior_day_atr"] = np.where(d > 0, pdh - close, close - pdl) / atr
    out["behind_prior_day_atr"] = np.where(d > 0, close - pdl, pdh - close) / atr
    for k in RETURN_LAGS:
        out[f"ret_{k}_atr"] = d * row[f"_ret_{k}"].to_numpy()
    out["session"] = sessions.session_label(pd.DatetimeIndex(signals["decision_time"]))
    out["regime"] = regime_label(out["h1_adx"], out["atr_pct"])
    out["setup"] = signals["setup"].to_numpy()
    return out


def payload(state_row: pd.Series | dict) -> dict:
    """The model-facing state: whitelisted keys only, floats rounded, NaN as None."""
    s = dict(state_row)
    out = {}
    for k in PAYLOAD_KEYS:
        v = s.get(k)
        if k in NUMERIC:
            v = None if v is None or not np.isfinite(v) else round(float(v), 4)
        out[k] = v
    return out
