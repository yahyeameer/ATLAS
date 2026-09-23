"""Prop evaluation Monte Carlo through the real risk engine (PRD §15, §19; T3 exit gate).

``metrics.daily_breach_probability`` bootstraps closed P/L only, at a fixed
risk. This module replays bootstrapped trading days through
``atlas_engine.risk.RiskEngine`` with the firm's rules from ``config/``:

- Days are resampled whole (including days with no trades), so a strategy's
  clustering of trades within a day is kept. Trades keep their time of day
  and can overlap.
- Floating loss is modelled from each trade's MAE: at every exit, all open
  trades are assumed to sit at their worst excursion at the same moment.
  That is pessimistic on purpose.
- With the engine, every entry goes through ``check_entry`` (open-risk cap,
  trades per day, daily soft stop, drawdown stop, loss-streak halving,
  firm headroom). A daily hard stop flattens the open trades at their MAE.
- Without the engine ("firm rules only") every trade is taken at fixed
  risk, which is what ``daily_breach_probability`` assumes.

A path ends on a firm breach, on passing the phase target with the minimum
trading days, on the internal drawdown stop (which needs the operator), or
at the horizon.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from atlas_engine.config import EngineConfig
from atlas_engine.risk import AccountSnapshot, OpenPosition, RiskEngine, TradeProposal
from atlas_engine.sizing.lots import ContractSpec

# A frictionless contract: costs are already inside each trade's R, and a
# tiny volume step keeps rounding from distorting risk.
_SIM_SPEC = ContractSpec("EURUSD", "EUR", "USD", 0.00001, 100_000, volume_min=1e-6, volume_step=1e-6, volume_max=1e9)
_SIM_STOP = 0.0010
_SIM_START = dt.date(2030, 1, 7)  # synthetic calendar; only day boundaries matter

OUTCOMES = ("passed", "firm_breach", "drawdown_stop", "timeout")


def day_blocks(trades: pd.DataFrame, cfg: EngineConfig) -> list[list[tuple]]:
    """Trades grouped by the firm's server day.

    Each trade is ``(entry_s, exit_s, r, mae_r, symbol, direction)`` with
    times as seconds from the start of its server day. Without ``symbol`` /
    ``direction`` columns every trade counts as a long EURUSD, which makes
    overlapping trades one bet (the strictest reading).

    Business days without trades are included as empty blocks. A trade still
    open at the end of its entry day is closed at the day's end.
    """
    if trades.empty:
        return []
    tz, reset = ZoneInfo(cfg.prop.reset_tz), cfg.prop.reset_time
    shift = pd.Timedelta(hours=reset.hour, minutes=reset.minute)
    entry = pd.DatetimeIndex(pd.to_datetime(trades["entry_time"], utc=True)).tz_convert(tz) - shift
    exit_ = pd.DatetimeIndex(pd.to_datetime(trades["exit_time"], utc=True)).tz_convert(tz) - shift
    day = entry.normalize()
    e_off = (entry - day).total_seconds().to_numpy()
    x_off = np.minimum((exit_ - day).total_seconds().to_numpy(), 86_399.0)
    x_off = np.maximum(x_off, e_off + 1)
    r = trades["r"].to_numpy(float)
    if "mae_r" in trades.columns:
        mae = np.maximum(trades["mae_r"].fillna(1.0).to_numpy(float), np.maximum(-r, 0.0))
    else:
        mae = np.maximum(1.0, -r)  # unknown excursion: assume the full stop
    n = len(trades)
    sym = trades["symbol"].astype(str).str.upper().to_numpy() if "symbol" in trades.columns else np.full(n, "EURUSD")
    direction = trades["direction"].to_numpy(int) if "direction" in trades.columns else np.ones(n, int)
    blocks: dict[dt.date, list] = {}
    for d, a, b, rr, m, s, di in zip(day.date, e_off, x_off, r, mae, sym, direction):
        blocks.setdefault(d, []).append((float(a), float(b), float(rr), float(m), str(s), int(di)))
    all_days = pd.bdate_range(min(blocks), max(blocks)).date
    return [sorted(blocks.get(d, [])) for d in all_days]


def simulate_evaluation(
    trades: pd.DataFrame,
    cfg: EngineConfig,
    n_sims: int = 2_000,
    horizon_days: int = 60,
    use_engine: bool = True,
    seed: int = 0,
) -> dict:
    """Probability of each evaluation outcome for this trade list under ``cfg``."""
    blocks = day_blocks(trades, cfg)
    if not blocks:
        raise ValueError("no trades to simulate")
    # Every symbol in the trade list is enabled here; all are sized on the
    # frictionless contract above, while netting still uses their real legs.
    symbols = tuple(sorted({t[4] for b in blocks for t in b}))
    cfg = dataclasses.replace(cfg, symbols=symbols)
    rng = np.random.default_rng(seed)
    outcomes = {k: 0 for k in OUTCOMES}
    breach_rules = {"firm_daily_loss": 0, "firm_max_loss": 0}
    days_to_pass, final_returns, entries = [], [], 0
    blocked_by: dict[str, int] = {}
    for _ in range(n_sims):
        picks = rng.integers(0, len(blocks), size=horizon_days)
        res = _run_path([blocks[i] for i in picks], cfg, use_engine)
        outcomes[res["outcome"]] += 1
        for rule in res["breach_rules"]:
            breach_rules[rule] += 1
        if res["outcome"] == "passed":
            days_to_pass.append(res["days"])
        final_returns.append(res["return_pct"])
        for reason, c in res["blocked_by"].items():
            blocked_by[reason] = blocked_by.get(reason, 0) + c
        entries += res["entries"]
    n = float(n_sims)
    return {
        "sims": n_sims,
        "horizon_days": horizon_days,
        "engine": use_engine,
        "phase": cfg.phase,
        "risk_per_trade_pct": cfg.risk.risk_per_trade_pct,
        "trades": int(len(trades)),
        "days_sampled_from": len(blocks),
        **{f"p_{k}": outcomes[k] / n for k in OUTCOMES},
        "p_firm_daily_loss": breach_rules["firm_daily_loss"] / n,
        "p_firm_max_loss": breach_rules["firm_max_loss"] / n,
        "median_days_to_pass": float(np.median(days_to_pass)) if days_to_pass else None,
        "return_pct_p05": float(np.percentile(final_returns, 5)),
        "return_pct_p50": float(np.percentile(final_returns, 50)),
        "entries_blocked_share": sum(blocked_by.values()) / entries if entries else 0.0,
        "blocked_by_share": {k: v / entries for k, v in sorted(blocked_by.items(), key=lambda kv: -kv[1])},
    }


def _run_path(days: list[list], cfg: EngineConfig, use_engine: bool) -> dict:
    initial = cfg.initial_balance
    prop = cfg.prop
    phase = prop.phases[cfg.phase]
    target = None if phase.profit_target_pct is None else initial * (1 + phase.profit_target_pct / 100)
    engine = RiskEngine(cfg)
    tz = ZoneInfo(prop.reset_tz)
    balance, trading_days, entries = initial, 0, 0
    blocked: dict[str, int] = {}
    day_start_balance = initial
    for k, block in enumerate(days):
        start = dt.datetime.combine(_SIM_START + dt.timedelta(days=k), prop.reset_time, tz)
        engine.observe(AccountSnapshot(start, balance, balance))
        day_start_balance = balance
        events = sorted([(tr[0], 1, i) for i, tr in enumerate(block)] + [(tr[1], 0, i) for i, tr in enumerate(block)])
        open_: dict[int, tuple[float, OpenPosition]] = {}  # index -> (risk money, position)
        traded_today, killed = False, False
        for t, is_entry, i in events:
            now = start + dt.timedelta(seconds=t)
            if is_entry:
                if killed:
                    continue
                entries += 1
                risk, pos = _enter(engine, cfg, now, balance, open_, use_engine, i, block[i], blocked)
                if risk is None:
                    continue
                open_[i] = (risk, pos)
                traded_today = True
                continue
            if i not in open_:
                continue
            worst = balance - sum(risk * block[j][3] for j, (risk, _) in open_.items())
            rules = prop.breached(worst, initial, day_start_balance, day_start_balance, engine.state.high_water)
            if rules:
                return _result("firm_breach", k + 1, worst, initial, blocked, entries, rules)
            if use_engine:
                a = engine.observe(AccountSnapshot(now, balance, worst, tuple(p for _, p in open_.values())))
                if a.flatten:  # hard daily stop: close everything at its worst excursion
                    for j, (risk, _) in list(open_.items()):
                        balance -= risk * block[j][3]
                        engine.record_close(now, -1.0)
                    open_.clear()
                    killed = True
                    continue
            risk, _ = open_.pop(i)
            pnl = risk * block[i][2]
            balance += pnl
            if use_engine:
                engine.record_close(now, pnl)
        if traded_today:
            trading_days += 1
        end = start + dt.timedelta(seconds=86_399)
        if use_engine:
            a = engine.observe(AccountSnapshot(end, balance, balance))
            if a.status == "drawdown_stopped":
                return _result("drawdown_stop", k + 1, balance, initial, blocked, entries)
        if target is not None and balance >= target and trading_days >= phase.min_trading_days:
            return _result("passed", k + 1, balance, initial, blocked, entries)
    return _result("timeout", len(days), balance, initial, blocked, entries)


def _enter(engine: RiskEngine, cfg: EngineConfig, now, balance: float, open_: dict, use_engine: bool, i: int,
           trade: tuple, blocked: dict):
    symbol, direction = trade[4], trade[5]
    stop = 1.0 - direction * _SIM_STOP
    if not use_engine:
        risk = balance * cfg.risk.risk_per_trade_pct / 100
    else:
        positions = tuple(p for _, p in open_.values())
        prop = TradeProposal(symbol, f"s{i}", direction, 1.0, stop, spec=_SIM_SPEC)
        d = engine.check_entry(prop, AccountSnapshot(now, balance, balance, positions))
        if not d.allowed:
            for reason in d.reasons:
                blocked[reason] = blocked.get(reason, 0) + 1
            return None, None
        engine.record_open(now)
        risk = d.risk_amount
    return risk, OpenPosition(str(i), symbol, f"s{i}", direction, 0.0, 1.0, stop, risk)


def _result(outcome, days, balance, initial, blocked, entries, rules=()):
    return {"outcome": outcome, "days": days, "return_pct": (balance / initial - 1) * 100,
            "blocked_by": blocked, "entries": entries, "breach_rules": list(rules)}


# -- reference trade profiles for the T3 gate ------------------------------------

PROFILES = {
    # name: (win rate at 2R, cost in R, mean trades per day)
    "edge_0.20R": (0.42, 0.06, 1.5),
    "breakeven": (0.353, 0.06, 1.5),
    "losing_-0.15R": (0.30, 0.06, 1.5),
    "busy_breakeven": (0.353, 0.06, 4.0),
}


def reference_trades(profile: str, days: int = 500, seed: int = 0) -> pd.DataFrame:
    """Synthetic trade lists with a known expectancy; not market data.

    Winners reach +2R after an adverse excursion of up to 0.95R; losers lose
    1R, and 5% of losers gap through the stop (-1.2 to -2R). Entries are
    spread over 07:00-17:00 UTC, holding 15 minutes to 6 hours, on EURUSD or
    GBPUSD in a random direction.
    """
    win, cost, per_day = PROFILES[profile]
    rng = np.random.default_rng(seed)
    rows = []
    for d in pd.bdate_range("2030-01-07", periods=days):
        for _ in range(min(rng.poisson(per_day), 8)):
            entry = d.tz_localize("UTC") + pd.Timedelta(minutes=int(rng.integers(7 * 60, 17 * 60)))
            exit_ = entry + pd.Timedelta(minutes=int(rng.integers(15, 360)))
            if rng.random() < win:
                r, mae = 2.0, rng.uniform(0.0, 0.95)
            else:
                r = -1.0 if rng.random() > 0.05 else -rng.uniform(1.2, 2.0)
                mae = -r
            symbol, direction = rng.choice(["EURUSD", "GBPUSD"]), int(rng.choice([1, -1]))
            rows.append((symbol, direction, entry, exit_, r - cost, mae))
    return pd.DataFrame(rows, columns=["symbol", "direction", "entry_time", "exit_time", "r", "mae_r"])
