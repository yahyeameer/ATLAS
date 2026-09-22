"""Bid/ask trade simulator on M1 bars (PRD §22 realism rules).

- Signals are decided at an M15 close; entry is the open of the first M1 bar
  at or after the decision time. Buys fill at the ask, sells at the bid.
- Long exits are triggered and filled on the bid, shorts on the ask.
- Spread is stressed around the mid by ``spread_mult`` (1.5 by default).
- SL and TP touched in the same M1 bar count as SL. A bar that opens beyond
  the stop fills at that open (gap), plus stop slippage.
- One position per setup per symbol: signals during an open trade are skipped.
- Commission and swap are charged in price units and converted to R.
- Optional Friday flatten closes at market at the first bar at/after the cutoff.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from atlas_engine.features import sessions

TRADE_COLS = [
    "symbol", "setup", "direction", "decision_time", "atr", "entry_time", "exit_time", "entry", "stop", "target",
    "exit", "risk", "exit_reason", "r_gross", "cost_r", "r", "mfe_r", "mae_r", "spread_entry", "rollovers",
]


@dataclass(frozen=True)
class CostModel:
    spread_mult: float = 1.5
    commission_rt: float = 0.00007  # price units per unit volume, round turn ($7/lot on USD-quoted pairs)
    entry_slippage: float = 0.00001  # market entries
    stop_slippage: float = 0.00002  # stop exits
    swap_per_rollover: float = 0.00005  # charged on both sides, conservative
    triple_swap_weekday: int = 2  # Wednesday rollover charges three nights

    def with_spread(self, mult: float) -> "CostModel":
        return CostModel(mult, self.commission_rt, self.entry_slippage, self.stop_slippage, self.swap_per_rollover, self.triple_swap_weekday)


@dataclass(frozen=True)
class ExitPolicy:
    rr: float = 2.0
    friday_flatten_utc: str | None = "20:00"
    max_entry_delay_min: int = 5


class M1Path:
    """M1 arrays for fast forward scans, with spread stress applied once."""

    def __init__(self, m1: pd.DataFrame, spread_mult: float):
        # pandas 3 may infer microsecond resolution; all int64 times here are ns.
        self.index = m1.index.as_unit("ns")
        self.t = self.index.asi8
        k = spread_mult
        for c in "ohlc":
            bid, ask = m1[f"bid_{c}"].to_numpy(), m1[f"ask_{c}"].to_numpy()
            mid, half = (bid + ask) / 2, (ask - bid) / 2 * k
            setattr(self, f"bid_{c}", mid - half)
            setattr(self, f"ask_{c}", mid + half)
        mins = (m1.index.hour * 60 + m1.index.minute).to_numpy()
        self.friday_after = lambda cutoff: (m1.index.weekday.to_numpy() == 4) & (mins >= sessions._hhmm(cutoff))


def count_rollovers(entry: pd.Timestamp, exit_: pd.Timestamp, triple_weekday: int) -> int:
    """Nights charged between entry and exit (17:00 New York rollovers)."""
    d0, d1 = sessions.fx_day(pd.DatetimeIndex([entry, exit_]))
    nights = int(np.busday_count(d0, d1))
    mask = ["0"] * 7
    mask[triple_weekday] = "1"
    return nights + 2 * int(np.busday_count(d0, d1, weekmask="".join(mask)))


def simulate(
    signals: pd.DataFrame,
    m1: pd.DataFrame,
    costs: CostModel = CostModel(),
    exits: ExitPolicy = ExitPolicy(),
    symbol: str = "",
    path: M1Path | None = None,
) -> pd.DataFrame:
    if signals.empty or m1.empty:
        return pd.DataFrame(columns=TRADE_COLS)
    p = path or M1Path(m1, costs.spread_mult)
    fri = p.friday_after(exits.friday_flatten_utc) if exits.friday_flatten_utc else None
    n = len(p.t)
    busy_until = np.iinfo(np.int64).min
    max_delay = np.int64(exits.max_entry_delay_min * 60 * 1_000_000_000)
    rows = []
    sig = signals.sort_values("decision_time")
    cols = (pd.DatetimeIndex(sig["decision_time"]).as_unit("ns").asi8, sig["direction"], sig["stop"], sig["atr"], sig["setup"])
    for dt_, d, stop, atr, setup in zip(*cols):
        i0 = int(np.searchsorted(p.t, dt_, side="left"))
        if i0 >= n or p.t[i0] - dt_ > max_delay or p.t[i0] < busy_until:
            continue
        if fri is not None and fri[i0]:
            continue
        d = int(d)
        entry = (p.ask_o[i0] + costs.entry_slippage) if d == 1 else (p.bid_o[i0] - costs.entry_slippage)
        risk = d * (entry - stop)
        if risk <= 0:
            continue
        target = entry + d * exits.rr * risk
        j, exit_px, reason = _scan(p, i0, d, stop, target, fri, costs.stop_slippage)
        busy_until = p.t[j] + 1
        hi = p.bid_h if d == 1 else p.ask_h
        lo = p.bid_l if d == 1 else p.ask_l
        fav = (hi[i0 : j + 1].max() - entry) if d == 1 else (entry - lo[i0 : j + 1].min())
        adv = (entry - lo[i0 : j + 1].min()) if d == 1 else (hi[i0 : j + 1].max() - entry)
        entry_time, exit_time = p.index[i0], p.index[j]
        nights = count_rollovers(entry_time, exit_time, costs.triple_swap_weekday)
        r_gross = d * (exit_px - entry) / risk
        cost_r = (costs.commission_rt + nights * costs.swap_per_rollover) / risk
        rows.append(
            (symbol, setup, d, pd.Timestamp(dt_, tz="UTC"), atr, entry_time, exit_time, entry, stop, target, exit_px, risk,
             reason, r_gross, cost_r, r_gross - cost_r, max(fav, 0.0) / risk, max(adv, 0.0) / risk,
             p.ask_o[i0] - p.bid_o[i0], nights)
        )
    return pd.DataFrame(rows, columns=TRADE_COLS)


def _scan(p: M1Path, i0: int, d: int, stop: float, target: float, fri: np.ndarray | None, stop_slip: float):
    """Find the exit bar. Returns (index, fill price, reason)."""
    n = len(p.t)
    chunk = 2048
    start = i0
    while start < n:
        end = min(start + chunk, n)
        if d == 1:
            sl = p.bid_l[start:end] <= stop
            tp = p.bid_h[start:end] >= target
        else:
            sl = p.ask_h[start:end] >= stop
            tp = p.ask_l[start:end] <= target
        flat = fri[start:end].copy() if fri is not None else np.zeros(end - start, bool)
        if start == i0:
            flat[0] = False  # never flatten on the entry bar itself
        hit = sl | tp | flat
        if hit.any():
            k = int(np.argmax(hit))
            j = start + k
            if sl[k]:
                o = p.bid_o[j] if d == 1 else p.ask_o[j]
                gapped = j > i0 and d * (o - stop) < 0
                px = (o if gapped else stop) - d * stop_slip
                return j, px, "gap_stop" if gapped else "stop"
            if tp[k]:
                return j, target, "target"
            return j, (p.bid_o[j] if d == 1 else p.ask_o[j]), "friday_flatten"
        start = end
    j = n - 1
    return j, (p.bid_c[j] if d == 1 else p.ask_c[j]), "end_of_data"
