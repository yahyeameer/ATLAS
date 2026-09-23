"""FTMO 2-Step rules as the YAML states them, and the generic rule arithmetic."""

import datetime as dt

import pytest
import yaml

from atlas_engine.prop_rules.rules import load_prop_rules, parse_prop_rules
from conftest import REPO_CONFIG, UTC

FTMO = REPO_CONFIG / "prop_rules" / "ftmo_2step.yaml"


@pytest.fixture
def ftmo():
    return load_prop_rules(FTMO)


def raw():
    return yaml.safe_load(FTMO.read_text())


def test_ftmo_values_match_the_published_objectives(ftmo):
    assert (ftmo.daily_loss_pct, ftmo.daily_loss_base, ftmo.daily_loss_reference) == (5.0, "initial_balance", "day_start_balance")
    assert ftmo.daily_includes_floating
    assert (ftmo.max_loss_pct, ftmo.max_loss_type) == (10.0, "static")
    assert ftmo.phases["challenge"].profit_target_pct == 10.0
    assert ftmo.phases["verification"].profit_target_pct == 5.0
    assert ftmo.phases["funded"].profit_target_pct is None
    assert all(ftmo.phases[p].min_trading_days == 4 for p in ("challenge", "verification"))
    assert ftmo.eas_allowed and ftmo.max_server_requests_per_day == 2000
    assert ftmo.reset_tz == "Europe/Prague" and ftmo.reset_time == dt.time(0, 0)


def test_daily_floor_is_day_start_balance_minus_five_percent_of_initial(ftmo):
    # Day opens at 10,400 after a good day: floor is 10,400 - 500, not 10,400 x 0.95.
    assert ftmo.daily_floor(10_000, 10_400, 10_350) == pytest.approx(9_900)
    assert ftmo.daily_floor(10_000, 9_700, 9_650) == pytest.approx(9_200)


def test_static_max_loss_ignores_high_water(ftmo):
    assert ftmo.max_loss_floor(10_000, high_water=12_000) == pytest.approx(9_000)


def test_breach_includes_touching_the_floor(ftmo):
    assert ftmo.breached(9_500.0, 10_000, 10_000, 10_000, 10_000) == ["firm_daily_loss"]
    assert ftmo.breached(9_500.01, 10_000, 10_000, 10_000, 10_000) == []
    assert ftmo.breached(9_000.0, 10_000, 9_400, 9_400, 10_000) == ["firm_max_loss"]


@pytest.mark.parametrize("utc, day", [
    # CEST (UTC+2): the server day starts at 22:00 UTC.
    (dt.datetime(2026, 9, 22, 21, 59, tzinfo=UTC), dt.date(2026, 9, 22)),
    (dt.datetime(2026, 9, 22, 22, 0, tzinfo=UTC), dt.date(2026, 9, 23)),
    # CET (UTC+1) in winter: it starts at 23:00 UTC.
    (dt.datetime(2026, 12, 1, 22, 30, tzinfo=UTC), dt.date(2026, 12, 1)),
    (dt.datetime(2026, 12, 1, 23, 0, tzinfo=UTC), dt.date(2026, 12, 2)),
])
def test_server_day_follows_prague_midnight_across_dst(ftmo, utc, day):
    assert ftmo.server_day(utc) == day


def test_next_reset(ftmo):
    assert ftmo.next_reset(dt.datetime(2026, 9, 22, 10, tzinfo=UTC)) == dt.datetime(2026, 9, 22, 22, tzinfo=UTC)


def test_naive_timestamps_are_refused(ftmo):
    with pytest.raises(ValueError, match="timezone-aware"):
        ftmo.server_day(dt.datetime(2026, 9, 22, 10))


def test_news_and_weekend_rules_apply_on_funded_only(ftmo):
    ev, fu = ftmo.restrictions_for("evaluation"), ftmo.restrictions_for("funded")
    news = dt.datetime(2026, 9, 22, 12, 30, tzinfo=UTC)
    assert not ev.news_blocked(news, [news]) and ev.weekend_holding
    assert fu.news_blocked(news - dt.timedelta(minutes=2), [news])
    assert fu.news_blocked(news + dt.timedelta(minutes=2), [news])
    assert not fu.news_blocked(news - dt.timedelta(minutes=3), [news])
    assert not fu.weekend_holding and fu.max_market_break_hours == 2
    assert ftmo.restrictions_for("paper") is ev


def test_trailing_eod_rises_with_high_water_and_can_lock():
    r = raw()
    r["max_loss"] = {"pct": 10.0, "type": "trailing_eod", "lock_at_initial": False}
    rules = parse_prop_rules(r)
    assert rules.max_loss_floor(10_000, 10_600) == pytest.approx(9_600)
    r["max_loss"]["lock_at_initial"] = True
    locked = parse_prop_rules(r)
    assert locked.max_loss_floor(10_000, 10_600) == pytest.approx(9_600)
    assert locked.max_loss_floor(10_000, 12_000) == pytest.approx(10_000)  # locked at the initial balance


@pytest.mark.parametrize("mutate, match", [
    (lambda r: r.update(bogus=1), "unknown key"),
    (lambda r: r["daily_loss"].update(pct=12), "daily_loss.pct <= max_loss.pct"),
    (lambda r: r["daily_loss"].update(pct=0), "0 < daily_loss.pct"),
    (lambda r: r["daily_loss"].update(reference="yesterday"), "reference one of"),
    (lambda r: r["max_loss"].update(type="relative"), "max_loss.type"),
    (lambda r: r["max_loss"].pop("pct"), "missing max_loss.pct"),
    (lambda r: r["restrictions"].pop("funded"), "missing restrictions.funded"),
    (lambda r: r["daily_loss"].update(reset_tz="Mars/Olympus"), "Mars/Olympus"),
])
def test_malformed_rules_are_refused(mutate, match):
    r = raw()
    mutate(r)
    with pytest.raises(Exception, match=match):
        parse_prop_rules(r)
