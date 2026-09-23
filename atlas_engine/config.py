"""Engine configuration loader (PRD §12 layer 2, §19, §27).

Reads ``config/atlas.yaml``, the firm's prop-rule YAML and ``config/risk.yaml``,
refuses unknown keys and any internal limit that is not inside the firm's,
and fingerprints the files so the engine can detect a change at runtime.
Config changes only by a signed operator commit; nothing here writes it.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from atlas_engine.market_data.symbols import SYMBOLS
from atlas_engine.prop_rules.rules import PropRules, _only, parse_prop_rules

MODES = {"evaluation", "funded", "paper"}
MAX_RISK_PER_TRADE = 0.50  # PRD §20: base 0.25-0.5%
MAX_RISK_PER_TRADE_EVAL = 0.75  # evaluation only, and only if Monte Carlo breach odds stay < 2%


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class RiskConfig:
    risk_per_trade_pct: float
    daily_loss_soft_pct_of_firm: float
    daily_loss_hard_pct_of_firm: float
    drawdown_stop_pct_of_firm: float
    drawdown_halve_pct_of_internal: float
    max_open_risk_pct: float
    max_currency_risk_pct: float
    max_trades_per_day: int
    loss_streak_halve_after: int
    loss_streak_halve_trades: int
    reduced_risk_multiplier: float
    max_sizing_overshoot: float
    correlation_threshold: float
    correlation_window_days: int
    correlation_max_age_days: int
    one_bet_max_risk_pct: float


@dataclass(frozen=True)
class EngineConfig:
    mode: str
    phase: str
    initial_balance: float
    currency: str
    symbols: tuple[str, ...]
    prop: PropRules
    risk: RiskConfig
    files: dict[str, str] = field(default_factory=dict)  # path -> sha256

    @property
    def checksum(self) -> str:
        h = hashlib.sha256()
        for path in sorted(self.files):
            h.update(f"{Path(path).name}:{self.files[path]}\n".encode())
        return h.hexdigest()

    def changed_files(self) -> list[str]:
        """Config files whose content differs from what was loaded."""
        return [p for p, digest in self.files.items() if not Path(p).exists() or _sha256(Path(p)) != digest]


def load_engine_config(root: str | Path = "config", require_read_only: bool = False) -> EngineConfig:
    """Load and validate the engine config under ``root``.

    With ``require_read_only`` (the live engine host), refuse to start if the
    running user can write any config file: config must be owned by a
    different OS user than the engine and the agents (PRD §12).
    """
    root = Path(root)
    main_path = root / "atlas.yaml"
    main = _read(main_path)
    _only(main, {"mode", "phase", "prop_rules", "risk", "account", "symbols",
                 "decision", "filters", "agent_intents", "execution"}, str(main_path), "")
    prop_path = root / _need(main, "prop_rules", main_path)
    risk_path = root / _need(main, "risk", main_path)
    prop = _wrap(lambda: parse_prop_rules(_read(prop_path), str(prop_path)))
    risk = _wrap(lambda: parse_risk(_read(risk_path), str(risk_path)))

    mode = _need(main, "mode", main_path)
    if mode not in MODES:
        raise ConfigError(f"{main_path}: mode must be one of {sorted(MODES)}")
    phase = main.get("phase", "funded" if mode == "funded" else "challenge")
    if phase not in prop.phases:
        raise ConfigError(f"{main_path}: phase {phase!r} is not a phase of {prop.firm} {prop.program}")
    acct = _need(main, "account", main_path)
    _only(acct, {"initial_balance", "currency"}, str(main_path), "account.")
    initial = float(_need(acct, "initial_balance", main_path))
    if initial <= 0:
        raise ConfigError(f"{main_path}: account.initial_balance must be positive")
    symbols = tuple(s.upper() for s in _need(main, "symbols", main_path))
    unknown = [s for s in symbols if s not in SYMBOLS]
    if unknown:
        raise ConfigError(f"{main_path}: unknown symbols {unknown}")
    if not prop.eas_allowed:
        raise ConfigError(f"{prop_path}: {prop.firm} does not allow EAs; ATLAS runs only where automation is permitted (PRD §19)")

    cfg = EngineConfig(mode, phase, initial, str(acct.get("currency", "USD")), symbols, prop, risk,
                       {str(p): _sha256(p) for p in (main_path, prop_path, risk_path)})
    validate(cfg)
    if require_read_only:
        writable = [p for p in cfg.files if os.access(p, os.W_OK)]
        if writable:
            raise ConfigError(f"config must be read-only to the engine user; writable: {writable}")
    return cfg


def parse_risk(raw: dict, where: str = "risk") -> RiskConfig:
    keys = {f for f in RiskConfig.__dataclass_fields__ if not f.startswith("correlation_") and f != "one_bet_max_risk_pct"}
    _only(raw, keys | {"correlation"}, where, "")
    corr = raw.get("correlation") or {}
    _only(corr, {"threshold", "window_days", "max_age_days", "one_bet_max_risk_pct"}, where, "correlation.")
    missing = sorted(keys - set(raw))
    if missing:
        raise ConfigError(f"{where}: missing {', '.join(missing)}")
    per_trade = float(raw["risk_per_trade_pct"])
    one_bet = corr.get("one_bet_max_risk_pct")
    return RiskConfig(
        risk_per_trade_pct=per_trade,
        daily_loss_soft_pct_of_firm=float(raw["daily_loss_soft_pct_of_firm"]),
        daily_loss_hard_pct_of_firm=float(raw["daily_loss_hard_pct_of_firm"]),
        drawdown_stop_pct_of_firm=float(raw["drawdown_stop_pct_of_firm"]),
        drawdown_halve_pct_of_internal=float(raw["drawdown_halve_pct_of_internal"]),
        max_open_risk_pct=float(raw["max_open_risk_pct"]),
        max_currency_risk_pct=float(raw["max_currency_risk_pct"]),
        max_trades_per_day=int(raw["max_trades_per_day"]),
        loss_streak_halve_after=int(raw["loss_streak_halve_after"]),
        loss_streak_halve_trades=int(raw["loss_streak_halve_trades"]),
        reduced_risk_multiplier=float(raw["reduced_risk_multiplier"]),
        max_sizing_overshoot=float(raw["max_sizing_overshoot"]),
        correlation_threshold=float(corr.get("threshold", 0.7)),
        correlation_window_days=int(corr.get("window_days", 60)),
        correlation_max_age_days=int(corr.get("max_age_days", 8)),
        one_bet_max_risk_pct=per_trade if one_bet is None else float(one_bet),
    )


def validate(cfg: EngineConfig) -> None:
    """Refuse any combination that is not strictly inside the firm's limits."""
    r, p = cfg.risk, cfg.prop
    cap = MAX_RISK_PER_TRADE_EVAL if cfg.mode == "evaluation" else MAX_RISK_PER_TRADE
    checks = [
        (0 < r.risk_per_trade_pct <= cap, f"risk_per_trade_pct must be in (0, {cap}] in {cfg.mode} mode"),
        (0 < r.daily_loss_soft_pct_of_firm < r.daily_loss_hard_pct_of_firm < 1,
         "need 0 < daily_loss_soft_pct_of_firm < daily_loss_hard_pct_of_firm < 1"),
        (0 < r.drawdown_stop_pct_of_firm < 1, "drawdown_stop_pct_of_firm must be in (0, 1)"),
        (0 < r.drawdown_halve_pct_of_internal < 1, "drawdown_halve_pct_of_internal must be in (0, 1)"),
        (r.risk_per_trade_pct <= r.max_open_risk_pct, "max_open_risk_pct must allow at least one trade"),
        (r.risk_per_trade_pct <= r.max_currency_risk_pct, "max_currency_risk_pct must allow at least one trade"),
        (r.risk_per_trade_pct <= r.one_bet_max_risk_pct, "correlation.one_bet_max_risk_pct must allow at least one trade"),
        (r.max_trades_per_day >= 1, "max_trades_per_day must be >= 1"),
        (r.loss_streak_halve_after >= 1 and r.loss_streak_halve_trades >= 1, "loss-streak settings must be >= 1"),
        (0 < r.reduced_risk_multiplier < 1, "reduced_risk_multiplier must be in (0, 1)"),
        (1 <= r.max_sizing_overshoot <= 1.10, "max_sizing_overshoot must be in [1, 1.10] (PRD §20)"),
        (0 < r.correlation_threshold < 1, "correlation.threshold must be in (0, 1)"),
        # Once the soft daily line stops new trades, the most still at risk is
        # the open risk to stops; together they must stay inside the firm's
        # daily loss. Same for the drawdown stop against the max loss.
        (r.daily_loss_soft_pct_of_firm * p.daily_loss_pct + r.max_open_risk_pct < p.daily_loss_pct,
         "daily soft stop plus max open risk must stay inside the firm's daily loss"),
        (r.drawdown_stop_pct_of_firm * p.max_loss_pct + r.max_open_risk_pct < p.max_loss_pct,
         "drawdown stop plus max open risk must stay inside the firm's max loss"),
    ]
    bad = [msg for ok, msg in checks if not ok]
    if bad:
        raise ConfigError("invalid risk config: " + "; ".join(bad))


def _read(path: Path) -> dict:
    if not path.exists():
        raise ConfigError(f"missing config file {path}")
    data = yaml.safe_load(path.read_text())
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: expected a mapping")
    return data


def _need(d: dict, key: str, where) -> object:
    if key not in d:
        raise ConfigError(f"{where}: missing {key}")
    return d[key]


def _wrap(fn):
    try:
        return fn()
    except ConfigError:
        raise
    except (ValueError, KeyError, TypeError) as e:
        raise ConfigError(str(e)) from e


def _sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()
