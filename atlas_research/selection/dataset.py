"""Meta-labelling dataset: every rules signal, its decision state and its outcome (PRD §16, §17).

Each candidate is a signal the setup emitted after the shared edge filters.
It is simulated on its own, ignoring the one-position rule, so every signal
gets a label, including ones the rules-only backtest skipped because a trade
was still open. The label is whether the target was hit before the stop
(``p_target_first``). Arms are scored later by re-simulating only the signals
they accept, so the one-position rule applies to each arm as it would live.

``recent_signal_r20`` is the mean R of the last 20 candidates of the same
setup that had closed by this candidate's decision time. The live engine can
track the same "shadow" outcomes, so the feature means the same in both.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from atlas_engine.decisions.state import state_frame, with_returns
from atlas_engine.setups import EdgeFilters, Setup

from ..backtest import TRADE_COLS, CostModel, ExitPolicy, M1Path, simulate
from ..t0 import Market

OUTCOME_COLS = ["entry_time", "exit_time", "exit_reason", "r", "cost_r", "risk"]
RECENT_N = 20


def label_signals(signals: pd.DataFrame, m1: pd.DataFrame, costs: CostModel, exits: ExitPolicy, symbol: str, path: M1Path) -> pd.DataFrame:
    """Simulate each signal independently. Returns outcome columns aligned to ``signals``' index (NaN if never filled)."""
    out = pd.DataFrame(index=signals.index, columns=OUTCOME_COLS)
    for i in signals.index:
        t = simulate(signals.loc[[i]], m1, costs, exits, symbol, path)
        if len(t):
            out.loc[i, OUTCOME_COLS] = t.iloc[0][OUTCOME_COLS].to_numpy()
    for c in ("r", "cost_r", "risk"):
        out[c] = pd.to_numeric(out[c])
    return out


def cost_r_estimate(stop_dist: np.ndarray, costs: CostModel) -> np.ndarray:
    """Costs known at decision time, in R: round-turn commission, entry and stop slippage, one night of swap.

    Spread is already inside the fills the label comes from, so it is not added again here.
    """
    per_trade = costs.commission_rt + costs.entry_slippage + costs.stop_slippage + costs.swap_per_rollover
    return per_trade / stop_dist


def recent_mean(decision_time: pd.Series, exit_time: pd.Series, r: pd.Series, n: int = RECENT_N) -> np.ndarray:
    """Mean of the last ``n`` outcomes whose exit is strictly before each decision time (NaN before any)."""
    closed = pd.DataFrame({"t": pd.DatetimeIndex(exit_time).as_unit("ns").asi8, "r": r.to_numpy(float)})
    closed = closed.dropna().sort_values("t", kind="mergesort")
    cum = np.concatenate([[0.0], np.cumsum(closed["r"].to_numpy())])
    k = np.searchsorted(closed["t"].to_numpy(), pd.DatetimeIndex(decision_time).as_unit("ns").asi8, side="left")
    lo = np.maximum(k - n, 0)
    cnt = k - lo
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(cnt > 0, (cum[k] - cum[lo]) / np.maximum(cnt, 1), np.nan)


def build_candidates(setup: Setup, markets: list[Market], params: dict, exits: ExitPolicy, filters: EdgeFilters, spread_mult: float) -> pd.DataFrame:
    """All candidates for one parameter set across markets, sorted by decision time."""
    parts = []
    for m in markets:
        sig = setup.signals(m.features, params, filters).reset_index(drop=True)
        if sig.empty:
            continue
        costs = m.costs.with_spread(spread_mult)
        if spread_mult not in m.paths:
            m.paths[spread_mult] = M1Path(m.m1, spread_mult)
        outcome = label_signals(sig, m.m1, costs, exits, m.symbol, m.paths[spread_mult])
        state = state_frame(with_returns(m.features), sig)
        stop_dist = state["stop_atr"].to_numpy() * m.features.set_index("close_time")["atr"].reindex(pd.DatetimeIndex(sig["decision_time"])).to_numpy()
        df = pd.concat([sig, outcome, state.drop(columns=["setup"])], axis=1)
        df["symbol"] = m.symbol
        df["target_r"] = exits.rr
        df["cost_r_est"] = cost_r_estimate(stop_dist, costs)
        parts.append(df)
    if not parts:
        return pd.DataFrame()
    c = pd.concat(parts, ignore_index=True)
    c = c.loc[c["exit_time"].notna()].copy()
    c["exit_time"] = pd.to_datetime(c["exit_time"], utc=True)
    c["entry_time"] = pd.to_datetime(c["entry_time"], utc=True)
    c["y"] = (c["exit_reason"] == "target").astype(int)
    c = c.sort_values("decision_time", kind="mergesort", ignore_index=True)
    c["recent_signal_r20"] = np.nan
    for _, g in c.groupby("setup"):
        c.loc[g.index, "recent_signal_r20"] = recent_mean(g["decision_time"], g["exit_time"], g["r"])
    c["candidate_id"] = [f"{s}-{sym}-{pd.Timestamp(t):%Y%m%d%H%M}" for s, sym, t in zip(c["setup"], c["symbol"], c["decision_time"])]
    return c


def signals_of(candidates: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """Back to per-symbol signal frames for the backtester."""
    cols = ["decision_time", "direction", "stop", "atr", "spread", "setup"]
    return {sym: g[cols].reset_index(drop=True) for sym, g in candidates.groupby("symbol")}


__all__ = ["TRADE_COLS", "build_candidates", "cost_r_estimate", "label_signals", "recent_mean", "signals_of"]
