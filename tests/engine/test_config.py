"""Engine config loading: strict keys, limits inside the firm's, fingerprints."""

import os

import pytest

from atlas_engine.config import ConfigError, load_engine_config


def test_repo_config_loads(cfg):
    assert cfg.mode == "evaluation" and cfg.phase == "challenge"
    assert cfg.initial_balance == 10_000 and cfg.currency == "USD"
    assert cfg.symbols == ("EURUSD", "GBPUSD")
    assert cfg.prop.firm == "FTMO"
    r = cfg.risk
    # PRD §19 / §27 defaults
    assert (r.risk_per_trade_pct, r.daily_loss_soft_pct_of_firm, r.daily_loss_hard_pct_of_firm) == (0.40, 0.50, 0.75)
    assert (r.drawdown_stop_pct_of_firm, r.max_open_risk_pct, r.max_currency_risk_pct) == (0.60, 1.5, 1.0)
    assert (r.max_trades_per_day, r.loss_streak_halve_after, r.loss_streak_halve_trades) == (6, 4, 10)
    assert r.one_bet_max_risk_pct == r.risk_per_trade_pct
    assert len(cfg.checksum) == 64


@pytest.mark.parametrize("file, changes, match", [
    ("risk.yaml", {"risk_per_trade_pct": 0.8}, "risk_per_trade_pct"),
    ("risk.yaml", {"daily_loss_soft_pct_of_firm": 0.8}, "soft"),
    ("risk.yaml", {"daily_loss_hard_pct_of_firm": 1.0}, "hard"),
    ("risk.yaml", {"drawdown_stop_pct_of_firm": 1.2}, "drawdown_stop"),
    ("risk.yaml", {"max_open_risk_pct": 3.0}, "daily soft stop plus max open risk"),
    ("risk.yaml", {"max_open_risk_pct": 0.3}, "at least one trade"),
    ("risk.yaml", {"max_sizing_overshoot": 1.5}, "max_sizing_overshoot"),
    ("risk.yaml", {"reduced_risk_multiplier": 1.0}, "reduced_risk_multiplier"),
    ("risk.yaml", {"correlation.threshold": 1.5}, "threshold"),
    ("risk.yaml", {"max_leverage": 30}, "unknown key"),
    ("risk.yaml", {"max_trades_per_day": "__DELETE__"}, "missing max_trades_per_day"),
    ("atlas.yaml", {"mode": "live"}, "mode must be"),
    ("atlas.yaml", {"phase": "phase3"}, "phase"),
    ("atlas.yaml", {"symbols": ["EURUSD", "DOGEUSD"]}, "unknown symbols"),
    ("atlas.yaml", {"account.initial_balance": 0}, "initial_balance"),
    ("atlas.yaml", {"prop_rules": "prop_rules/missing.yaml"}, "missing config file"),
    ("prop_rules/ftmo_2step.yaml", {"eas_allowed": False}, "does not allow EAs"),
    ("prop_rules/ftmo_2step.yaml", {"daily_loss.pct": 3.0}, "daily soft stop plus max open risk"),
])
def test_invalid_configs_are_refused(config_dir, file, changes, match):
    changes = {k: (config_dir.DELETE if v == "__DELETE__" else v) for k, v in changes.items()}
    with pytest.raises(ConfigError, match=match):
        load_engine_config(config_dir(file, changes))


def test_higher_risk_allowed_only_in_evaluation(config_dir):
    d = config_dir("risk.yaml", {"risk_per_trade_pct": 0.6, "correlation.one_bet_max_risk_pct": None})
    assert load_engine_config(d).risk.risk_per_trade_pct == 0.6
    config_dir("atlas.yaml", {"mode": "funded", "phase": "funded"})
    with pytest.raises(ConfigError, match=r"\(0, 0.5\] in funded"):
        load_engine_config(d)


def test_changed_files_detects_edits_after_load(config_dir):
    d = config_dir.dir
    cfg = load_engine_config(d)
    assert cfg.changed_files() == []
    config_dir("risk.yaml", {"max_trades_per_day": 7})
    assert [os.path.basename(p) for p in cfg.changed_files()] == ["risk.yaml"]


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write any file")
def test_require_read_only_refuses_writable_config(config_dir):
    with pytest.raises(ConfigError, match="read-only"):
        load_engine_config(config_dir.dir, require_read_only=True)
    for p in config_dir.dir.rglob("*.yaml"):
        p.chmod(0o444)
    assert load_engine_config(config_dir.dir, require_read_only=True)


def test_require_read_only_checks_every_file(config_dir, monkeypatch):
    seen = []
    monkeypatch.setattr(os, "access", lambda p, mode: seen.append(os.path.basename(p)) or p.endswith("risk.yaml"))
    with pytest.raises(ConfigError, match="risk.yaml"):
        load_engine_config(config_dir.dir, require_read_only=True)
    assert sorted(seen) == ["atlas.yaml", "ftmo_2step.yaml", "risk.yaml"]
