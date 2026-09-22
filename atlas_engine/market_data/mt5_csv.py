"""Import bars exported from MetaTrader 5 (History Center → Export bars).

MT5 exports are bid-only with a ``<SPREAD>`` column in points and timestamps
in broker server time. Most FX brokers run server time at New York + 7h
(UTC+2 winter, UTC+3 summer) so the trading day closes at 00:00 server time;
that is the default conversion here. The ask side is reconstructed as
``bid + spread``, which is only exact at the bar's recorded spread, so broker
exports are a cross-check on Dukascopy rather than a replacement for it.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from .bars import BAR_COLS
from .symbols import spec


def server_to_utc(ts: pd.Series, ny_offset_hours: int = 7) -> pd.DatetimeIndex:
    ny_local = pd.DatetimeIndex(ts) - pd.Timedelta(hours=ny_offset_hours)
    return ny_local.tz_localize("America/New_York", ambiguous="NaT", nonexistent="shift_forward").tz_convert("UTC")


def read_mt5_bars(path: Path, symbol: str, point: float | None = None, ny_offset_hours: int = 7) -> pd.DataFrame:
    raw = pd.read_csv(path, sep="\t")
    raw.columns = [c.strip("<>").lower() for c in raw.columns]
    stamp = pd.to_datetime(raw["date"] + " " + raw.get("time", "00:00:00"), format="%Y.%m.%d %H:%M:%S")
    index = server_to_utc(stamp, ny_offset_hours)
    pt = point if point is not None else 1.0 / spec(symbol).price_scale
    spread = raw["spread"].astype(float) * pt
    df = pd.DataFrame(index=pd.DatetimeIndex(index, name="time"))
    for c, src in zip("ohlc", ("open", "high", "low", "close")):
        df[f"bid_{c}"] = raw[src].to_numpy()
        df[f"ask_{c}"] = raw[src].to_numpy() + spread.to_numpy()
    df["volume"] = raw["tickvol"].astype(float).to_numpy()
    df = df[df.index.notna()]
    return df[BAR_COLS].sort_index()
