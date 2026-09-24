"""Managed exits for Phase T1 exit research (PRD §18).

``simulate_managed`` extends the T0 simulator (``backtest.simulate``) with the
§18 exit rules. It shares the fill model: M1 bid/ask with stressed spread,
buys at the ask, long exits on the bid, same-minute stop and target count as
the stop, gaps through the stop fill at the open, costs charged in R.

Management follows the §18 rules:

- The initial stop and target (and a partial-close level) are broker-side
  orders, triggered inside the M1 bar.
- Every other rule decides at an M15 close, using only closed bars. A market
  close fills at the next M1 open; a stop move takes effect from that minute.
- Stops only ever tighten, and move at most once per M15 bar (the tightest of
  breakeven, ATR trail and structure trail wins).
- Session exits (before the New York rollover, Friday 20:00 UTC) are
  scheduled closes at the first minute at or after the cutoff.

With only a fixed target and the Friday flatten switched on, the result is
identical to ``backtest.simulate``; a test holds the two together.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields

import numpy as np
import pandas as pd

from atlas_engine.features import sessions

from .backtest import TRADE_COLS, CostModel, M1Path, count_rollovers

MANAGED_COLS = TRADE_COLS + ["exit_variant", "bars_held", "stop_moves", "partial_r"]  # bars_held counts M15 bars with data
M15_NS = np.int64(15 * 60 * 1_000_000_000)


@dataclass(frozen=True)
class ExitSpec:
    """One exit variant. ``None`` switches a rule off.

    Defaults are the §18 "defaults to test"; every variant is declared in
    ``configs/t1.yaml`` before any result is seen.
    """

    name: str = "fixed_2r"
    rr: float | None = 2.0  # fixed target in R from the actual fill; None = no target
    # Time stop: close at the M15 close after this many bars if MFE never reached min_r.
    time_stop_bars: int | None = None
    time_stop_min_r: float = 0.5
    # Breakeven: once MFE reaches this, stop to entry plus commission and stop slippage.
    breakeven_at_r: float | None = None
    # Partial close: a broker-side take-profit for ``partial_frac`` of the position.
    partial_at_r: float | None = None
    partial_frac: float = 0.5
    # ATR trail: once MFE reaches ``atr_trail_after_r``, stop = best price - mult x ATR(M15).
    atr_trail_mult: float | None = None
    atr_trail_after_r: float = 1.5
    # Structure trail: once MFE reaches ``structure_after_r``, stop beyond the last confirmed M15 swing.
    structure_trail: bool = False
    structure_after_r: float = 1.0
    swing_bars: int = 2  # bars on each side that confirm a swing
    structure_buffer_atr: float = 0.1
    # Session exits.
    rollover_exit_ny: str | None = None  # e.g. "16:45": flatten before the New York rollover
    friday_flatten_utc: str | None = "20:00"
    # Invalidation: close when the closed-H1 trend flips against the position.
    invalidation: bool = False
    max_entry_delay_min: int = 5

    @classmethod
    def from_dict(cls, name: str, d: dict | None) -> "ExitSpec":
        known = {f.name for f in fields(cls)}
        unknown = set(d or {}) - known
        if unknown:
            raise ValueError(f"exit variant {name!r}: unknown key(s) {', '.join(sorted(unknown))}")
        spec = cls(**{**(d or {}), "name": name})
        spec.validate()
        return spec

    def validate(self) -> None:
        if self.rr is not None and self.rr <= 0:
            raise ValueError(f"{self.name}: rr must be positive")
        if self.partial_at_r is not None:
            if not 0 < self.partial_frac < 1:
                raise ValueError(f"{self.name}: partial_frac must be in (0, 1)")
            if self.rr is not None and self.partial_at_r >= self.rr:
                raise ValueError(f"{self.name}: partial level must sit below the target")
        if self.time_stop_bars is not None and self.time_stop_bars < 1:
            raise ValueError(f"{self.name}: time_stop_bars must be >= 1")
        if self.rr is None and not (self.atr_trail_mult or self.structure_trail or self.time_stop_bars
                                    or self.invalidation or self.rollover_exit_ny or self.friday_flatten_utc):
            raise ValueError(f"{self.name}: no target and nothing else that ever closes the trade")

    @property
    def needs_management(self) -> bool:
        return bool(self.time_stop_bars or self.breakeven_at_r is not None or self.atr_trail_mult
                     or self.structure_trail or self.invalidation)

    def as_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if k != "name"}


class ManagementFrame:
    """Closed-M15 values the manager may read, keyed by bar close time (ns)."""

    def __init__(self, features: pd.DataFrame, swing_bars: int = 2):
        f = features
        self.close_t = pd.DatetimeIndex(f["close_time"]).as_unit("ns").asi8
        self.atr = f["atr"].to_numpy(float)
        self.h1_trend = f["h1_trend"].to_numpy(int) if "h1_trend" in f else np.zeros(len(f), int)
        k = swing_bars
        lo, hi = f["low"], f["high"]
        win = 2 * k + 1
        # A swing low at bar i is the lowest of bars i-k..i+k, so it is known only at bar i+k's close.
        is_low = lo.eq(lo.rolling(win, center=True).min())
        is_high = hi.eq(hi.rolling(win, center=True).max())
        self.swing_low = lo.where(is_low).shift(k).ffill().to_numpy(float)
        self.swing_high = hi.where(is_high).shift(k).ffill().to_numpy(float)

    def row(self, close_ns: int) -> int:
        """Index of the last M15 bar closed at or before ``close_ns`` (-1 if none)."""
        return int(np.searchsorted(self.close_t, close_ns, side="right")) - 1


def _clock_mask(index: pd.DatetimeIndex, tz: str, start: str, end: str) -> np.ndarray:
    return sessions.in_window(sessions.local_minutes(index, tz), start, end)


def simulate_managed(
    signals: pd.DataFrame,
    m1: pd.DataFrame,
    features: pd.DataFrame | None,
    costs: CostModel = CostModel(),
    spec: ExitSpec = ExitSpec(),
    symbol: str = "",
    path: M1Path | None = None,
    mgmt: ManagementFrame | None = None,
) -> pd.DataFrame:
    if signals.empty or m1.empty:
        return pd.DataFrame(columns=MANAGED_COLS)
    p = path or M1Path(m1, costs.spread_mult)
    if spec.needs_management and mgmt is None:
        if features is None:
            raise ValueError(f"exit variant {spec.name!r} manages trades and needs the M15 feature frame")
        mgmt = ManagementFrame(features, spec.swing_bars)
    n = len(p.t)
    fri = p.friday_after(spec.friday_flatten_utc) if spec.friday_flatten_utc else np.zeros(n, bool)
    flat = fri.copy()
    if spec.rollover_exit_ny:
        # Flatten from the cutoff to the 17:00 New York rollover itself.
        flat |= _clock_mask(p.index, sessions.NEW_YORK, spec.rollover_exit_ny, "17:00")
    no_entry = fri if spec.friday_flatten_utc else None

    busy_until = np.iinfo(np.int64).min
    max_delay = np.int64(spec.max_entry_delay_min * 60 * 1_000_000_000)
    rows = []
    sig = signals.sort_values("decision_time")
    cols = (pd.DatetimeIndex(sig["decision_time"]).as_unit("ns").asi8, sig["direction"], sig["stop"], sig["atr"], sig["setup"])
    for dt_, d, stop, atr, setup in zip(*cols):
        i0 = int(np.searchsorted(p.t, dt_, side="left"))
        if i0 >= n or p.t[i0] - dt_ > max_delay or p.t[i0] < busy_until:
            continue
        if no_entry is not None and no_entry[i0]:
            continue
        d = int(d)
        entry = (p.ask_o[i0] + costs.entry_slippage) if d == 1 else (p.bid_o[i0] - costs.entry_slippage)
        risk = d * (entry - stop)
        if risk <= 0:
            continue
        tr = _manage(p, i0, d, entry, float(stop), risk, spec, costs, flat, fri, mgmt)
        j = tr["j"]
        busy_until = p.t[j] + 1
        hi = p.bid_h if d == 1 else p.ask_h
        lo = p.bid_l if d == 1 else p.ask_l
        fav = (hi[i0 : j + 1].max() - entry) if d == 1 else (entry - lo[i0 : j + 1].min())
        adv = (entry - lo[i0 : j + 1].min()) if d == 1 else (hi[i0 : j + 1].max() - entry)
        entry_time, exit_time = p.index[i0], p.index[j]
        nights = count_rollovers(entry_time, exit_time, costs.triple_swap_weekday)
        f = tr["partial_frac"]
        r_main = d * (tr["exit_px"] - entry) / risk
        r_gross = f * tr["partial_r"] + (1 - f) * r_main if f else r_main
        swap_nights = (1 - f) * nights + f * tr["partial_nights"] if f else nights
        cost_r = (costs.commission_rt + swap_nights * costs.swap_per_rollover) / risk
        target = entry + d * spec.rr * risk if spec.rr is not None else np.nan
        rows.append(
            (symbol, setup, d, pd.Timestamp(dt_, tz="UTC"), atr, entry_time, exit_time, entry, stop, target, tr["exit_px"],
             risk, tr["reason"], r_gross, cost_r, r_gross - cost_r, max(fav, 0.0) / risk, max(adv, 0.0) / risk,
             p.ask_o[i0] - p.bid_o[i0], nights, spec.name, len(np.unique(p.t[i0 : j + 1] // M15_NS)), tr["moves"],
             tr["partial_r"] if f else np.nan)
        )
    return pd.DataFrame(rows, columns=MANAGED_COLS)


def _manage(p: M1Path, i0: int, d: int, entry: float, stop: float, risk: float, spec: ExitSpec, costs: CostModel,
            flat: np.ndarray, fri: np.ndarray, mgmt: ManagementFrame | None) -> dict:
    """Walk one trade forward, one M15 bar at a time. Returns the exit and what management did."""
    n = len(p.t)
    target = entry + d * spec.rr * risk if spec.rr is not None else None
    partial = entry + d * spec.partial_at_r * risk if spec.partial_at_r is not None else None
    out = {"partial_frac": 0.0, "partial_r": np.nan, "partial_nights": 0, "moves": 0, "bars": 0}
    best = entry  # most favourable exit-side price seen on closed data
    trend = None
    if mgmt is not None:
        k = mgmt.row(int(p.t[i0]))
        trend = int(mgmt.h1_trend[k]) if k >= 0 else 0
    s = i0
    while True:
        # The M1 bars of the M15 bar in progress: up to the next 15-minute close strictly after bar s opens.
        # Without M15 management, scan in large chunks instead (same result, far fewer steps).
        close_ns = (p.t[s] // M15_NS + 1) * M15_NS
        e = min(int(np.searchsorted(p.t, close_ns, side="left")), n) if mgmt is not None else min(s + 2048, n)
        hit = _scan_segment(p, s, e, i0, d, stop, target, partial, flat, costs.stop_slippage)
        if hit is not None and hit[2] == "partial":
            j = hit[0]
            out.update(partial_frac=spec.partial_frac, partial_r=d * (hit[1] - entry) / risk,
                       partial_nights=count_rollovers(p.index[i0], p.index[j], costs.triple_swap_weekday))
            partial = None
            # The rest of the bar that filled the partial: the stop was not touched there, so only the
            # target or a scheduled close can still fire.
            hit = _scan_segment(p, j, e, i0, d, stop, target, None, flat, costs.stop_slippage, skip_stop_at=j)
        if hit is not None:
            reason = hit[2]
            if reason == "scheduled":
                reason = "friday_flatten" if fri[hit[0]] else "session_exit"
            elif reason in ("stop", "gap_stop") and out["moves"]:
                reason = "managed_" + reason  # a breakeven or trailed stop, not the initial one
            return {**out, "j": hit[0], "exit_px": hit[1], "reason": reason}
        seg_hi = (p.bid_h if d == 1 else p.ask_h)[s:e]
        seg_lo = (p.bid_l if d == 1 else p.ask_l)[s:e]
        best = max(best, seg_hi.max()) if d == 1 else min(best, seg_lo.min())
        if e >= n:
            j = n - 1
            return {**out, "j": j, "exit_px": p.bid_c[j] if d == 1 else p.ask_c[j], "reason": "end_of_data"}
        if mgmt is not None:
            out["bars"] += 1
            decision, trend = _decide(mgmt, int(close_ns), d, entry, stop, risk, best, out["bars"], spec, costs,
                                      p.bid_c[e - 1] if d == 1 else p.ask_c[e - 1], trend)
            if isinstance(decision, str):
                return {**out, "j": e, "exit_px": p.bid_o[e] if d == 1 else p.ask_o[e], "reason": decision}
            if decision is not None:
                stop = decision
                out["moves"] += 1
        s = e


def _scan_segment(p: M1Path, s: int, e: int, i0: int, d: int, stop: float, target: float | None, partial: float | None,
                  flat: np.ndarray, stop_slip: float, skip_stop_at: int | None = None):
    """First exit event in M1 bars [s, e): (index, price, reason) or None. Stop beats target in the same minute."""
    if e <= s:
        return None
    if d == 1:
        sl = p.bid_l[s:e] <= stop
        tp = p.bid_h[s:e] >= target if target is not None else np.zeros(e - s, bool)
        pt = p.bid_h[s:e] >= partial if partial is not None else np.zeros(e - s, bool)
    else:
        sl = p.ask_h[s:e] >= stop
        tp = p.ask_l[s:e] <= target if target is not None else np.zeros(e - s, bool)
        pt = p.ask_l[s:e] <= partial if partial is not None else np.zeros(e - s, bool)
    fl = flat[s:e].copy()
    if s == i0:
        fl[0] = False  # never flatten on the entry bar itself
    if skip_stop_at is not None and skip_stop_at == s:
        sl[0] = False
        fl[0] = False
    ev = sl | tp | pt | fl
    if not ev.any():
        return None
    k = int(np.argmax(ev))
    j = s + k
    if sl[k]:
        o = p.bid_o[j] if d == 1 else p.ask_o[j]
        gapped = j > i0 and d * (o - stop) < 0
        return j, (o if gapped else stop) - d * stop_slip, "gap_stop" if gapped else "stop"
    if pt[k] and not tp[k]:
        return j, partial, "partial"
    if tp[k]:
        return j, target, "target"
    return j, (p.bid_o[j] if d == 1 else p.ask_o[j]), "scheduled"


def _decide(mg: ManagementFrame, close_ns: int, d: int, entry: float, stop: float, risk: float, best: float,
            bars: int, spec: ExitSpec, costs: CostModel, px_now: float, prev_trend: int):
    """At an M15 close: (a close reason, a new tighter stop or None, the closed-H1 trend now).

    Invalidation needs a flip: the trend turning against the position after
    entry. A counter-trend entry (a sweep reversal) is not invalidated by the
    trend it was already fading.
    """
    i = mg.row(close_ns)
    if i < 0:
        return None, prev_trend
    trend = int(mg.h1_trend[i])
    mfe_r = d * (best - entry) / risk
    if spec.invalidation and trend == -d and prev_trend != -d:
        return "invalidation", trend
    if spec.time_stop_bars and bars >= spec.time_stop_bars and mfe_r < spec.time_stop_min_r:
        return "time_stop", trend
    cands = []
    if spec.breakeven_at_r is not None and mfe_r >= spec.breakeven_at_r:
        cands.append(entry + d * (costs.commission_rt + costs.stop_slippage))
    atr = mg.atr[i]
    if spec.atr_trail_mult and mfe_r >= spec.atr_trail_after_r and np.isfinite(atr):
        cands.append(best - d * spec.atr_trail_mult * atr)
    if spec.structure_trail and mfe_r >= spec.structure_after_r:
        level = mg.swing_low[i] if d == 1 else mg.swing_high[i]
        if np.isfinite(level) and np.isfinite(atr):
            cands.append(level - d * spec.structure_buffer_atr * atr)
    if not cands:
        return None, trend
    new = max(cands) if d == 1 else min(cands)
    # Only tighten, and never to a price the market is already through (the broker would reject it).
    if d * (new - stop) > 0 and d * (px_now - new) > 0:
        return float(new), trend
    return None, trend
