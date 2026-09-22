"""Trade statistics, Monte Carlo risk and the deflated Sharpe ratio (PRD §15)."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
from scipy import stats

from atlas_engine.features import sessions

EULER_GAMMA = 0.5772156649015329


def summary(trades: pd.DataFrame) -> dict:
    r = trades["r"].to_numpy(float) if len(trades) else np.array([])
    n = len(r)
    wins, losses = r[r > 0], r[r <= 0]
    return {
        "trades": n,
        "expectancy_r": float(r.mean()) if n else 0.0,
        "total_r": float(r.sum()),
        "profit_factor": profit_factor(r),
        "win_rate": float((r > 0).mean()) if n else 0.0,
        "avg_win_r": float(wins.mean()) if len(wins) else 0.0,
        "avg_loss_r": float(losses.mean()) if len(losses) else 0.0,
        "sharpe_per_trade": sharpe(r),
        "avg_cost_r": float(trades["cost_r"].mean()) if n else 0.0,
    }


def profit_factor(r: np.ndarray) -> float:
    gains, losses = r[r > 0].sum(), -r[r < 0].sum()
    if losses == 0:
        return math.inf if gains > 0 else 0.0
    return float(gains / losses)


def sharpe(r: np.ndarray) -> float:
    if len(r) < 2 or np.std(r, ddof=1) == 0:
        return 0.0
    return float(np.mean(r) / np.std(r, ddof=1))


def max_drawdown(equity: np.ndarray) -> np.ndarray:
    """Max peak-to-trough drop per row of an (n_paths, n_steps) equity array starting at 0."""
    eq = np.concatenate([np.zeros((equity.shape[0], 1)), equity], axis=1)
    return (np.maximum.accumulate(eq, axis=1) - eq).max(axis=1)


def mc_drawdown(r: np.ndarray, risk_pct: float, n_sims: int = 10_000, seed: int = 0, skip_frac: float = 0.0) -> dict:
    """Monte Carlo reshuffle of the trade sequence; drawdown in % of starting equity.

    With ``skip_frac`` each path also drops that fraction of trades at random
    (the PRD's "skip 10%" test) and reports the expectancy spread.
    """
    rng = np.random.default_rng(seed)
    n = len(r)
    if n == 0:
        return {"dd_p95_pct": 0.0, "dd_p50_pct": 0.0, "expectancy_p05": 0.0}
    paths = np.tile(r, (n_sims, 1))
    paths = rng.permuted(paths, axis=1)
    if skip_frac:
        keep = rng.random(paths.shape) >= skip_frac
        exp = np.where(keep, paths, 0).sum(axis=1) / np.maximum(keep.sum(axis=1), 1)
        paths = np.where(keep, paths, 0.0)
    else:
        exp = paths.mean(axis=1)
    dd = max_drawdown(np.cumsum(paths * risk_pct, axis=1))
    return {
        "dd_p95_pct": float(np.percentile(dd, 95)),
        "dd_p50_pct": float(np.percentile(dd, 50)),
        "expectancy_p05": float(np.percentile(exp, 5)),
    }


def daily_breach_probability(
    trades: pd.DataFrame, risk_pct: float, daily_limit_pct: float, eval_days: int = 30, n_sims: int = 10_000, seed: int = 0
) -> float:
    """P(any day in an evaluation breaches the daily-loss limit).

    Each trading day's worst intraday closed-P/L drawdown is bootstrapped
    (including days without trades). Floating loss is not modelled, so this
    understates intraday risk slightly; the internal buffers in §19 cover that.
    """
    if trades.empty:
        return 0.0
    t = trades.sort_values("exit_time")
    day = sessions.fx_day(pd.DatetimeIndex(t["exit_time"]))
    worst = (t["r"] * risk_pct).groupby(day.to_numpy()).apply(lambda s: min(0.0, s.cumsum().min()))
    all_days = pd.bdate_range(min(day), max(day))
    worst = worst.reindex([d.date() for d in all_days], fill_value=0.0).to_numpy()
    rng = np.random.default_rng(seed)
    draws = worst[rng.integers(0, len(worst), size=(n_sims, eval_days))]
    return float((draws.min(axis=1) <= -daily_limit_pct).mean())


def expected_max_sharpe(n_trials: int, var_trials: float) -> float:
    """Expected maximum Sharpe of ``n_trials`` skill-less strategies (Bailey & López de Prado)."""
    if n_trials < 2 or var_trials <= 0:
        return 0.0
    z1 = stats.norm.ppf(1 - 1.0 / n_trials)
    z2 = stats.norm.ppf(1 - 1.0 / (n_trials * math.e))
    return math.sqrt(var_trials) * ((1 - EULER_GAMMA) * z1 + EULER_GAMMA * z2)


def deflated_sharpe(r: np.ndarray, n_trials: int, var_trials: float) -> float:
    """Probability the true per-trade Sharpe exceeds the best of ``n_trials`` lucky ones."""
    n = len(r)
    if n < 3:
        return 0.0
    sr = sharpe(r)
    sr0 = expected_max_sharpe(n_trials, var_trials)
    skew = float(stats.skew(r))
    kurt = float(stats.kurtosis(r, fisher=False))
    denom = 1 - skew * sr + (kurt - 1) / 4 * sr**2
    if denom <= 0:
        return 0.0
    return float(stats.norm.cdf((sr - sr0) * math.sqrt(n - 1) / math.sqrt(denom)))


def breakdown(trades: pd.DataFrame, by: str) -> pd.DataFrame:
    if trades.empty:
        return pd.DataFrame()
    t = trades.copy()
    if by == "year":
        key = pd.DatetimeIndex(t["entry_time"]).year
    elif by == "session":
        key = session_label(pd.DatetimeIndex(t["entry_time"]))
    else:
        key = t[by]
    g = t.groupby(key)["r"]
    out = pd.DataFrame({"trades": g.size(), "expectancy_r": g.mean(), "total_r": g.sum()})
    total = t["r"].sum()
    out["share_of_profit"] = out["total_r"] / total if total > 0 else np.nan
    return out


def session_label(index: pd.DatetimeIndex) -> np.ndarray:
    lon = sessions.local_minutes(index, sessions.LONDON)
    ny = sessions.local_minutes(index, sessions.NEW_YORK)
    return np.select(
        [(lon >= 480) & (ny < 480), (ny >= 480) & (lon < 16 * 60 + 30), (ny >= 480) & (ny < 17 * 60)],
        ["london", "overlap", "new_york"],
        default="asia",
    )
