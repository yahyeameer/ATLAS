"""Randomised account paths: invariants that must hold for every decision."""

import datetime as dt

import numpy as np
import pandas as pd
import pytest

from atlas_engine.exposure import CorrelationMatrix
from atlas_engine.risk import AccountSnapshot, OpenPosition, RiskEngine, TradeProposal
from conftest import T


@pytest.mark.parametrize("seed", range(20))
def test_no_allowed_trade_can_breach_or_exceed_limits(cfg, seed):
    rng = np.random.default_rng(seed)
    win = 0.38 if seed % 2 else 0.18  # half the paths lose steadily and hit the stops
    # Low correlation lets positions stack so the open-risk and currency caps bind.
    low = pd.DataFrame([[1, 0.3], [0.3, 1]], index=["EURUSD", "GBPUSD"], columns=["EURUSD", "GBPUSD"], dtype=float)
    matrix = CorrelationMatrix(low, T.date()) if seed % 4 < 2 else None
    e = RiskEngine(cfg)
    balance = 10_000.0
    positions: list[OpenPosition] = []
    now = T
    for step in range(400):
        if matrix is not None:
            matrix = CorrelationMatrix(matrix.corr, now.date())
        now += dt.timedelta(minutes=int(rng.integers(5, 90)))
        floating = -sum(p.risk_amount * rng.uniform(0, 1.6) for p in positions)
        snap = AccountSnapshot(now, balance, balance + floating, tuple(positions))
        a = e.observe(snap)
        if a.status == "drawdown_stopped" and rng.random() < 0.02:
            e.operator_reenable(now, "operator")
            a = e.observe(snap)
        if a.flatten:
            balance += floating
            for _ in positions:
                e.record_close(now, -1)
            positions = []
            continue
        if positions and rng.random() < 0.4:  # close one
            p = positions.pop(int(rng.integers(len(positions))))
            pnl = p.risk_amount * (2.0 if rng.random() < win else -rng.choice([1.0, 1.0, 1.0, 1.6]))
            balance += pnl
            e.record_close(now, pnl)
            continue
        sym = str(rng.choice(["EURUSD", "GBPUSD"]))
        direction = int(rng.choice([1, -1]))
        entry = 1.1 if sym == "EURUSD" else 1.3
        stop = entry - direction * rng.uniform(0.0005, 0.0040)
        prop = TradeProposal(sym, f"s{step % 5}", direction, entry, stop, ev_scale=float(rng.uniform(0.3, 1.5)))
        d = e.check_entry(prop, snap, matrix)
        if not d.allowed:
            assert d.volume == 0 and d.risk_amount == 0 and d.reasons
            continue
        lines = e.lines()
        open_risk = sum(p.risk_amount for p in positions)
        assert a.new_trades_allowed and a.status == "ok"
        assert d.risk_amount <= snap.equity * cfg.risk.risk_per_trade_pct / 100 * d.multiplier * 1.10 + 1e-6
        assert d.multiplier <= 1.0
        assert open_risk + d.risk_amount <= snap.equity * cfg.risk.max_open_risk_pct / 100 + 1e-6
        worst = snap.equity - open_risk - d.risk_amount
        assert worst > max(lines["firm_daily_floor"], lines["firm_max_loss_floor"])
        assert e.state.trades_today < cfg.risk.max_trades_per_day
        assert not any(p.symbol == sym and p.setup == prop.setup for p in positions)
        e.record_open(now)
        positions.append(OpenPosition(str(step), sym, prop.setup, direction, d.volume, entry, stop, d.risk_amount))
    # stops overshooting by up to 60% on some losses still never broke a firm rule
    assert not e.state.firm_breached
