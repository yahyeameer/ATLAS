"""Dukascopy historical M1 bid/ask candles.

Dukascopy publishes one LZMA-compressed ``.bi5`` file per symbol, UTC day and
price side at::

    https://datafeed.dukascopy.com/datafeed/{SYMBOL}/{YYYY}/{MM0}/{DD}/{BID|ASK}_candles_min_1.bi5

where ``MM0`` is the zero-based month. Each record is 24 big-endian bytes:
``uint32 seconds-from-day-start, int32 open, int32 close, int32 low,
int32 high, float32 volume``. Prices are integers scaled by the symbol's
``price_scale``. Closed-market minutes are present with zero volume.

Files are cached on disk exactly as downloaded, so a download can be resumed
and a rebuild never touches the network. A 404 or empty body is cached as an
empty file (weekends, holidays).
"""

from __future__ import annotations

import datetime as dt
import logging
import lzma
import struct
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import pandas as pd

from .symbols import spec

log = logging.getLogger(__name__)

BASE_URL = "https://datafeed.dukascopy.com/datafeed"
SIDES = ("BID", "ASK")
RECORD = struct.Struct(">I4if")
RECORD_DTYPE = np.dtype(
    [("t", ">u4"), ("open", ">i4"), ("close", ">i4"), ("low", ">i4"), ("high", ">i4"), ("volume", ">f4")]
)


def candle_url(symbol: str, day: dt.date, side: str) -> str:
    side = side.upper()
    if side not in SIDES:
        raise ValueError(f"side must be BID or ASK, got {side!r}")
    return f"{BASE_URL}/{symbol.upper()}/{day.year:04d}/{day.month - 1:02d}/{day.day:02d}/{side}_candles_min_1.bi5"


def cache_path(cache_dir: Path, symbol: str, day: dt.date, side: str) -> Path:
    return Path(cache_dir) / symbol.upper() / f"{day.year:04d}" / f"{day:%m}" / f"{day:%d}_{side.upper()}.bi5"


def decode_candles(raw: bytes, day: dt.date, price_scale: int) -> pd.DataFrame:
    """Decode one day's ``.bi5`` payload into a UTC-indexed OHLCV frame."""
    cols = ["open", "high", "low", "close", "volume"]
    if not raw:
        return pd.DataFrame(columns=cols, index=pd.DatetimeIndex([], tz="UTC", name="time"), dtype=float)
    payload = lzma.decompress(raw)
    if len(payload) % RECORD.size:
        raise ValueError(f"corrupt candle file for {day}: {len(payload)} bytes is not a multiple of {RECORD.size}")
    rec = np.frombuffer(payload, dtype=RECORD_DTYPE)
    day_start = pd.Timestamp(day, tz="UTC")
    index = day_start + pd.to_timedelta(rec["t"].astype(np.int64), unit="s")
    df = pd.DataFrame(
        {
            "open": rec["open"].astype(np.float64) / price_scale,
            "high": rec["high"].astype(np.float64) / price_scale,
            "low": rec["low"].astype(np.float64) / price_scale,
            "close": rec["close"].astype(np.float64) / price_scale,
            "volume": rec["volume"].astype(np.float64),
        },
        index=pd.DatetimeIndex(index, name="time"),
    )
    _check_field_order(df, day)
    return df


def _check_field_order(df: pd.DataFrame, day: dt.date) -> None:
    """Guard against a wrong record layout: low/high must bracket open/close."""
    if df.empty:
        return
    ok = (df["low"] <= df[["open", "close"]].min(axis=1)) & (df["high"] >= df[["open", "close"]].max(axis=1))
    if ok.mean() < 0.99:
        raise ValueError(f"candle file for {day} fails OHLC sanity ({ok.mean():.1%} valid); record layout may have changed")


def encode_candles(df: pd.DataFrame, day: dt.date, price_scale: int) -> bytes:
    """Inverse of :func:`decode_candles`. Used by tests and fixtures."""
    secs = ((df.index - pd.Timestamp(day, tz="UTC")).total_seconds()).astype(np.int64)
    rec = np.empty(len(df), dtype=RECORD_DTYPE)
    rec["t"] = secs
    for col in ("open", "close", "low", "high"):
        rec[col] = np.rint(df[col].to_numpy() * price_scale).astype(np.int64)
    rec["volume"] = df["volume"].to_numpy()
    return lzma.compress(rec.tobytes(), format=lzma.FORMAT_ALONE)


def fetch(url: str, retries: int = 4, timeout: float = 30.0) -> bytes:
    """GET ``url``; a 404 means no data for that day and returns ``b""``."""
    delay = 2.0
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return b""
            err: Exception = exc
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            err = exc
        if attempt == retries:
            raise RuntimeError(f"failed to fetch {url}: {err}") from err
        time.sleep(delay)
        delay *= 2
    raise AssertionError("unreachable")


def download_day(symbol: str, day: dt.date, side: str, cache_dir: Path, refresh: bool = False) -> Path:
    path = cache_path(cache_dir, symbol, day, side)
    if path.exists() and not refresh:
        return path
    raw = fetch(candle_url(symbol, day, side))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".part")
    tmp.write_bytes(raw)
    tmp.replace(path)
    return path


def download_range(
    symbol: str,
    start: dt.date,
    end: dt.date,
    cache_dir: Path,
    workers: int = 8,
    refresh: bool = False,
) -> int:
    """Download every day in ``[start, end]`` for both sides. Returns files fetched."""
    days = [d.date() for d in pd.date_range(start, end, freq="D")]
    today = dt.datetime.now(dt.timezone.utc).date()
    jobs = [(d, s) for d in days if d < today for s in SIDES]
    todo = [(d, s) for d, s in jobs if refresh or not cache_path(cache_dir, symbol, d, s).exists()]
    log.info("%s: %d day-files requested, %d to fetch", symbol, len(jobs), len(todo))
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(download_day, symbol, d, s, cache_dir, refresh): (d, s) for d, s in todo}
        for fut in as_completed(futures):
            fut.result()
            done += 1
            if done % 500 == 0:
                log.info("%s: %d/%d fetched", symbol, done, len(todo))
    return done


def load_side(symbol: str, day: dt.date, side: str, cache_dir: Path) -> pd.DataFrame:
    path = cache_path(cache_dir, symbol, day, side)
    if not path.exists():
        raise FileNotFoundError(f"{path} not downloaded; run `atlas-research data download` first")
    return decode_candles(path.read_bytes(), day, spec(symbol).price_scale)
