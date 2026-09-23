"""Every internal limit in PRD §19 and §20, one test (or more) each."""

import dataclasses
import datetime as dt

import pandas as pd
import pytest

from atlas_engine.config import load_engine_config
from atlas_engine.exposure import CorrelationMatrix
from atlas_engine.risk import RiskEngine, RiskState
from conftest import T, UTC, position, proposal, snap

H = dt.timedelta(hours=1)


def open_ok(engine, **kw):
    d = engine.check_entry(proposal(**kw), snap())
    assert d.allowed, d.reasons
    engine.record_open(T)
    return d


# -- baseline ------------------------------------------------------------------

def test_clean_account_allows_a_standard_trade(engine):
    d = engine.check_entry(proposal(), snap())
    assert d.allowed and d.reasons == ()
    assert d.volume == pytest.approx(0.25)
    assert d.risk_amount <= 40.0 and d.multiplier == 1.0
    assert d.to_dict()["checks"]["status"] == "ok"


def test_decision_is_deterministic(engine, cfg):
    a = engine.check_entry(proposal(), snap()).to_dict()
    b = RiskEngine(cfg).check_entry(proposal(), snap()).to_dict()
    assert a == b


# -- daily loss (§19 rows 1-2) -----------------------------------------------

def test_lines_for_ftmo_10k(engine):
    engine.observe(snap())
    lines = engine.lines()
    assert lines["firm_daily_floor"] == pytest.approx(9_500)
    assert lines["daily_soft"] == pytest.approx(9_750)  # 50% of $500
    assert lines["daily_hard"] == pytest.approx(9_625)  # 75% of $500
    assert lines["firm_max_loss_floor"] == pytest.approx(9_000)
    assert lines["drawdown_stop"] == pytest.approx(9_400)  # 60% of $1,000
    assert lines["drawdown_halve"] == pytest.approx(9_700)  # 50% of the internal $600


def test_soft_daily_stop_blocks_new_trades_but_does_not_flatten(engine):
    engine.observe(snap())
    a = engine.observe(snap(10_000, 9_750))  # floating loss counts
    assert a.status == "day_stopped" and not a.new_trades_allowed and not a.flatten
    d = engine.check_entry(proposal(), snap(10_000, 9_760))
    assert not d.allowed and "account_day_stopped" in d.reasons  # latched even after recovering a little


def test_hard_daily_stop_flattens_and_disables(engine):
    engine.observe(snap())
    a = engine.observe(snap(10_000, 9_620))
    assert a.status == "day_killed" and a.flatten and not a.new_trades_allowed
    assert [e["event"] for e in engine.events] == ["day_killed"]


def test_daily_latches_clear_at_the_next_server_day(engine):
    engine.observe(snap())
    engine.observe(snap(9_620, 9_620))
    next_day = dt.datetime(2026, 9, 22, 22, 0, tzinfo=UTC)  # 00:00 Prague
    a = engine.observe(snap(9_620, 9_620, time=next_day))
    assert a.status == "ok" and a.new_trades_allowed
    assert engine.state.day_start_balance == 9_620
    assert engine.lines()["daily_soft"] == pytest.approx(9_370)


def test_daily_reference_protects_carried_floating_profit(engine):
    # Day opens with $300 floating profit: the internal line counts from equity,
    # FTMO's from balance.
    engine.observe(snap(10_000, 10_300))
    lines = engine.lines()
    assert lines["firm_daily_floor"] == pytest.approx(9_500)
    assert lines["daily_soft"] == pytest.approx(10_050)


def test_day_start_below_balance_uses_the_balance(engine):
    engine.observe(snap(10_000, 9_900))  # floating loss carried overnight
    assert engine.lines()["daily_soft"] == pytest.approx(9_750)


# -- drawdown (§19 row 3, §20 m) ------------------------------------------------

def test_drawdown_stop_latches_until_operator(engine):
    engine.observe(snap(9_500, 9_500))
    next_day = T + dt.timedelta(days=1)
    a = engine.observe(snap(9_400, 9_400, time=next_day))
    assert a.status == "drawdown_stopped" and a.requires_operator and not a.flatten
    a = engine.observe(snap(9_450, 9_450, time=next_day + dt.timedelta(days=1)))
    assert a.status == "drawdown_stopped"  # a new day does not clear it
    engine.operator_reenable(next_day, "yahye")
    assert engine.observe(snap(9_450, 9_450, time=next_day + dt.timedelta(days=1))).status == "ok"
    # still past the line -> re-latches at once
    assert engine.observe(snap(9_390, 9_390, time=next_day + dt.timedelta(days=1))).status == "drawdown_stopped"


def test_drawdown_beyond_half_the_internal_limit_halves_risk(engine):
    engine.observe(snap(9_700, 9_700))
    d = engine.check_entry(proposal(), snap(9_700, 9_700))
    assert d.allowed and d.multiplier == 0.5
    assert d.risk_amount <= 9_700 * 0.004 * 0.5


# -- open risk, trades per day, loss streak (§19 rows 4-6) ------------------------

def test_open_risk_cap(engine):
    positions = [position("EURUSD", f"s{i}", 1 if i % 2 else -1, 40.0, str(i)) for i in range(3)]
    d = engine.check_entry(proposal("GBPUSD", direction=-1), snap(positions=positions + [position("USDJPY", "x", 1, 40.0, "9")]))
    assert "max_open_risk" in d.reasons  # 160 + ~40 > 150


def test_max_trades_per_day(engine):
    for _ in range(6):
        engine.observe(snap())
        engine.record_open(T)
    d = engine.check_entry(proposal(), snap())
    assert d.reasons == ("max_trades_per_day",)


def test_four_losses_halve_the_next_ten_trades(engine):
    engine.observe(snap())
    for _ in range(4):
        engine.record_close(T, -40.0)
    assert engine.state.reduced_trades_left == 10
    for i in range(10):
        d = engine.check_entry(proposal(), snap(time=T + dt.timedelta(days=i)))
        assert d.multiplier == 0.5
        engine.record_open(T + dt.timedelta(days=i))
    assert engine.check_entry(proposal(), snap(time=T + dt.timedelta(days=11))).multiplier == 1.0


def test_a_win_resets_the_streak(engine):
    for pnl in (-1, -1, -1, 5, -1, -1, -1):
        engine.record_close(T, pnl)
    assert engine.state.reduced_trades_left == 0 and engine.state.loss_streak == 3


def test_halving_does_not_compound(engine):
    engine.observe(snap(9_650, 9_650))
    for _ in range(4):
        engine.record_close(T, -1)
    assert engine.check_entry(proposal(), snap(9_650, 9_650)).multiplier == 0.5


def test_ev_scaling_never_exceeds_base_risk(engine):
    big = engine.check_entry(proposal(ev_scale=3.0), snap())
    assert big.multiplier == 1.0
    small = engine.check_entry(proposal(ev_scale=0.5), snap())
    assert small.multiplier == 0.5


# -- currency netting and correlation (§20) -------------------------------------

def test_currency_cap_on_stacked_usd_shorts(cfg):
    e = RiskEngine(dataclasses.replace(cfg, symbols=("EURUSD", "GBPUSD", "USDJPY"),
                                       risk=dataclasses.replace(cfg.risk, one_bet_max_risk_pct=2.0)))
    positions = [position("EURUSD", "a", 1, 40.0, "1"), position("GBPUSD", "b", 1, 40.0, "2")]
    d = e.check_entry(proposal("EURUSD", "c", 1), snap(positions=positions))
    assert "currency_risk_USD" in d.reasons and "currency_risk_EUR" not in d.reasons


def test_trade_that_reduces_a_currency_breach_is_not_blocked_by_it(cfg):
    e = RiskEngine(dataclasses.replace(cfg, symbols=("EURUSD", "GBPUSD", "USDJPY"),
                                       risk=dataclasses.replace(cfg.risk, max_currency_risk_pct=0.5)))
    positions = [position("EURUSD", "a", 1, 40.0, "1"), position("GBPUSD", "b", 1, 40.0, "2")]  # USD -0.8%
    d = e.check_entry(proposal("USDJPY", "c", 1), snap(positions=positions))  # long USD
    assert not any(r == "currency_risk_USD" for r in d.reasons)


def corr(c, as_of=dt.date(2026, 9, 21)):
    m = pd.DataFrame([[1, c], [c, 1]], index=["EURUSD", "GBPUSD"], columns=["EURUSD", "GBPUSD"], dtype=float)
    return CorrelationMatrix(m, as_of)


def test_correlated_pairs_are_one_bet(engine):
    positions = [position("EURUSD", "a", 1, 40.0)]
    d = engine.check_entry(proposal("GBPUSD", "b", 1), snap(positions=positions), corr(0.85))
    assert "correlated_one_bet" in d.reasons
    d = engine.check_entry(proposal("GBPUSD", "b", 1), snap(positions=positions), corr(0.5))
    assert "correlated_one_bet" not in d.reasons


def test_stale_matrix_falls_back_to_shared_currency_rule(engine):
    positions = [position("EURUSD", "a", 1, 40.0)]
    d = engine.check_entry(proposal("GBPUSD", "b", 1), snap(positions=positions), corr(0.1, dt.date(2026, 9, 1)))
    assert d.checks["correlation_stale"] and d.checks["correlation"]["rule"] == "shared_currency"
    assert "correlated_one_bet" in d.reasons


# -- other per-trade rules ----------------------------------------------------------

def test_one_position_per_setup_per_symbol(engine):
    d = engine.check_entry(proposal(setup="a"), snap(positions=[position("EURUSD", "a", 1, 40.0)]))
    assert "position_exists_for_setup" in d.reasons


def test_stop_on_wrong_side_is_refused(engine):
    d = engine.check_entry(proposal(stop_pips=-10), snap())
    assert d.reasons == ("stop_on_wrong_side",) and d.volume == 0


def test_symbol_not_enabled(engine):
    assert "symbol_not_enabled" in engine.check_entry(proposal("USDJPY", entry=150.0), snap()).reasons


def test_firm_headroom_blocks_a_trade_whose_stop_could_breach(cfg):
    # Loosen internal lines so only the firm headroom check stands in the way.
    risk = dataclasses.replace(cfg.risk, daily_loss_soft_pct_of_firm=0.98, daily_loss_hard_pct_of_firm=0.99,
                               max_open_risk_pct=10.0, max_currency_risk_pct=10.0, one_bet_max_risk_pct=10.0)
    e = RiskEngine(dataclasses.replace(cfg, risk=risk))
    e.observe(snap())
    d = e.check_entry(proposal(), snap(10_000, 9_515))  # half risk (~$19) would end at ~9,496
    assert d.reasons == ("firm_limit_headroom",)


def test_news_window_blocks_entries_on_funded_only(cfg):
    news = (T + dt.timedelta(minutes=1),)
    assert RiskEngine(cfg).check_entry(proposal(news_events=news), snap()).allowed
    funded = dataclasses.replace(cfg, mode="funded", phase="funded")
    assert "prop_news_window" in RiskEngine(funded).check_entry(proposal(news_events=news), snap()).reasons


def test_firm_breach_is_permanent(engine):
    engine.observe(snap())
    a = engine.observe(snap(10_000, 9_500))
    assert a.status == "firm_breached" and a.flatten and a.requires_operator
    with pytest.raises(PermissionError):
        engine.operator_reenable(T, "yahye")
    assert engine.observe(snap(10_000, 10_000, time=T + dt.timedelta(days=2))).status == "firm_breached"


def test_record_open_requires_an_observed_day(engine):
    with pytest.raises(ValueError, match="observe"):
        engine.record_open(T)


# -- persistence and evaluation progress ----------------------------------------------

def test_state_round_trips_through_dict(engine, cfg):
    engine.observe(snap())
    engine.record_open(T)
    for _ in range(4):
        engine.record_close(T, -1)
    restored = RiskEngine(cfg, RiskState.from_dict(engine.state.to_dict()))
    assert restored.state == engine.state
    assert restored.check_entry(proposal(), snap()).to_dict() == engine.check_entry(proposal(), snap()).to_dict()


def test_phase_progress_needs_target_and_min_days(engine):
    for i in range(3):
        engine.observe(snap(time=T + dt.timedelta(days=i)))
        engine.record_open(T + dt.timedelta(days=i))
    assert not engine.phase_progress(11_000)["passed"]
    engine.observe(snap(time=T + dt.timedelta(days=3)))
    engine.record_open(T + dt.timedelta(days=3))
    p = engine.phase_progress(11_000)
    assert p["passed"] and p["trading_days"] == 4 and p["target_balance"] == pytest.approx(11_000)
    assert not engine.phase_progress(10_999)["passed"]


def test_trailing_intraday_high_water_moves_the_drawdown_line(config_dir):
    cfg = load_engine_config(config_dir("prop_rules/ftmo_2step.yaml", {"max_loss.type": "trailing_intraday"}))
    e = RiskEngine(cfg)
    e.observe(snap(10_000, 10_500))
    assert e.lines()["firm_max_loss_floor"] == pytest.approx(9_500)
    assert e.lines()["drawdown_stop"] == pytest.approx(9_900)
