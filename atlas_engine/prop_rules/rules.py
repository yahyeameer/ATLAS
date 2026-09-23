"""Firm rules as data plus the arithmetic that applies them.

Everything here is the firm's own limit. ATLAS' internal limits, which sit
well inside these, are in ``atlas_engine.risk``. Money is in account currency.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

DAILY_BASES = {"initial_balance", "day_start_balance", "day_start_equity"}
DAILY_REFERENCES = {"day_start_balance", "day_start_equity", "day_start_max", "initial_balance"}
MAX_LOSS_TYPES = {"static", "trailing_eod", "trailing_intraday"}
MODES = {"evaluation", "funded"}


@dataclass(frozen=True)
class PhaseRules:
    profit_target_pct: float | None
    min_trading_days: int


@dataclass(frozen=True)
class Restrictions:
    news_window_min: tuple[int, int] | None  # minutes before, after a restricted release
    weekend_holding: bool
    max_market_break_hours: float | None

    def news_blocked(self, ts: dt.datetime, events: list[dt.datetime]) -> bool:
        """True if opening or closing at ``ts`` falls inside a restricted news window."""
        if not self.news_window_min:
            return False
        before, after = (dt.timedelta(minutes=m) for m in self.news_window_min)
        return any(e - before <= ts <= e + after for e in events)


@dataclass(frozen=True)
class PropRules:
    firm: str
    program: str
    checked: str
    daily_loss_pct: float
    daily_loss_base: str
    daily_loss_reference: str
    daily_includes_floating: bool
    reset_time: dt.time
    reset_tz: str
    max_loss_pct: float
    max_loss_type: str
    lock_at_initial: bool
    phases: dict[str, PhaseRules]
    trading_day: str
    restrictions: dict[str, Restrictions]
    consistency_rule: dict | None
    max_lot: float | None
    eas_allowed: bool
    max_server_requests_per_day: int | None

    # -- server day -------------------------------------------------------

    def server_day(self, ts: dt.datetime) -> dt.date:
        """The firm's trading day containing ``ts`` (daily loss resets at its start)."""
        if ts.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware (UTC everywhere, PRD §21)")
        local = ts.astimezone(ZoneInfo(self.reset_tz))
        shift = dt.timedelta(hours=self.reset_time.hour, minutes=self.reset_time.minute)
        return (local - shift).date()

    def next_reset(self, ts: dt.datetime) -> dt.datetime:
        """UTC time the next server day starts."""
        tz = ZoneInfo(self.reset_tz)
        day = self.server_day(ts) + dt.timedelta(days=1)
        return dt.datetime.combine(day, self.reset_time, tz).astimezone(dt.timezone.utc)

    # -- limits in money ---------------------------------------------------

    def daily_loss_amount(self, initial: float, day_start_balance: float, day_start_equity: float) -> float:
        base = {"initial_balance": initial, "day_start_balance": day_start_balance, "day_start_equity": day_start_equity}
        return self.daily_loss_pct / 100 * base[self.daily_loss_base]

    def daily_reference(self, initial: float, day_start_balance: float, day_start_equity: float) -> float:
        return {
            "day_start_balance": day_start_balance,
            "day_start_equity": day_start_equity,
            "day_start_max": max(day_start_balance, day_start_equity),
            "initial_balance": initial,
        }[self.daily_loss_reference]

    def daily_floor(self, initial: float, day_start_balance: float, day_start_equity: float) -> float:
        """Equity the account may not reach today."""
        ref = self.daily_reference(initial, day_start_balance, day_start_equity)
        return ref - self.daily_loss_amount(initial, day_start_balance, day_start_equity)

    def max_loss_amount(self, initial: float) -> float:
        return self.max_loss_pct / 100 * initial

    def max_loss_reference(self, initial: float, high_water: float) -> float:
        """Level the max-loss allowance is measured from.

        ``high_water`` is the highest end-of-day balance (trailing_eod) or the
        highest equity (trailing_intraday) seen; static ignores it.
        """
        if self.max_loss_type == "static":
            return initial
        ref = max(initial, high_water)
        if self.lock_at_initial:
            ref = min(ref, initial + self.max_loss_amount(initial))
        return ref

    def max_loss_floor(self, initial: float, high_water: float) -> float:
        return self.max_loss_reference(initial, high_water) - self.max_loss_amount(initial)

    def breached(self, equity: float, initial: float, day_start_balance: float, day_start_equity: float,
                 high_water: float) -> list[str]:
        """Firm rules that ``equity`` violates (empty when within limits)."""
        out = []
        if equity <= self.daily_floor(initial, day_start_balance, day_start_equity):
            out.append("firm_daily_loss")
        if equity <= self.max_loss_floor(initial, high_water):
            out.append("firm_max_loss")
        return out

    def restrictions_for(self, mode: str) -> Restrictions:
        return self.restrictions["funded" if mode == "funded" else "evaluation"]


def load_prop_rules(path: str | Path) -> PropRules:
    raw = yaml.safe_load(Path(path).read_text())
    return parse_prop_rules(raw, str(path))


def parse_prop_rules(raw: dict, where: str = "prop rules") -> PropRules:
    def need(d: dict, key: str, ctx: str):
        if key not in d:
            raise ValueError(f"{where}: missing {ctx}{key}")
        return d[key]

    _only(raw, {"firm", "program", "checked", "daily_loss", "max_loss", "phases", "trading_day", "restrictions",
                "consistency_rule", "max_lot", "eas_allowed", "max_server_requests_per_day"}, where, "")
    dl, ml = need(raw, "daily_loss", ""), need(raw, "max_loss", "")
    _only(dl, {"pct", "base", "reference", "includes_floating", "reset_time", "reset_tz"}, where, "daily_loss.")
    _only(ml, {"pct", "type", "lock_at_initial"}, where, "max_loss.")
    daily_pct, max_pct = float(need(dl, "pct", "daily_loss.")), float(need(ml, "pct", "max_loss."))
    if not 0 < daily_pct <= max_pct <= 100:
        raise ValueError(f"{where}: need 0 < daily_loss.pct <= max_loss.pct <= 100")
    base, ref = need(dl, "base", "daily_loss."), need(dl, "reference", "daily_loss.")
    if base not in DAILY_BASES or ref not in DAILY_REFERENCES:
        raise ValueError(f"{where}: daily_loss.base must be one of {sorted(DAILY_BASES)}, reference one of {sorted(DAILY_REFERENCES)}")
    mtype = need(ml, "type", "max_loss.")
    if mtype not in MAX_LOSS_TYPES:
        raise ValueError(f"{where}: max_loss.type must be one of {sorted(MAX_LOSS_TYPES)}")
    tz = need(dl, "reset_tz", "daily_loss.")
    ZoneInfo(tz)  # raises on an unknown zone
    h, m = str(need(dl, "reset_time", "daily_loss.")).split(":")
    phases = {}
    for name, p in need(raw, "phases", "").items():
        _only(p, {"profit_target_pct", "min_trading_days"}, where, f"phases.{name}.")
        tgt = p.get("profit_target_pct")
        phases[name] = PhaseRules(None if tgt is None else float(tgt), int(p.get("min_trading_days", 0)))
    restrictions = {}
    for mode in MODES:
        r = need(need(raw, "restrictions", ""), mode, "restrictions.")
        _only(r, {"news_window_min", "weekend_holding", "max_market_break_hours"}, where, f"restrictions.{mode}.")
        win = r.get("news_window_min")
        restrictions[mode] = Restrictions(
            None if win is None else (int(win[0]), int(win[1])),
            bool(r.get("weekend_holding", True)),
            None if r.get("max_market_break_hours") is None else float(r["max_market_break_hours"]),
        )
    return PropRules(
        firm=str(need(raw, "firm", "")), program=str(raw.get("program", "")), checked=str(raw.get("checked", "")),
        daily_loss_pct=daily_pct, daily_loss_base=base, daily_loss_reference=ref,
        daily_includes_floating=bool(dl.get("includes_floating", True)),
        reset_time=dt.time(int(h), int(m)), reset_tz=tz,
        max_loss_pct=max_pct, max_loss_type=mtype, lock_at_initial=bool(ml.get("lock_at_initial", False)),
        phases=phases, trading_day=str(raw.get("trading_day", "position_opened")), restrictions=restrictions,
        consistency_rule=raw.get("consistency_rule"),
        max_lot=None if raw.get("max_lot") is None else float(raw["max_lot"]),
        eas_allowed=bool(need(raw, "eas_allowed", "")),
        max_server_requests_per_day=raw.get("max_server_requests_per_day"),
    )


def _only(d: dict, allowed: set[str], where: str, ctx: str) -> None:
    if not isinstance(d, dict):
        raise ValueError(f"{where}: {ctx or 'top level'} must be a mapping")
    extra = set(d) - allowed
    if extra:
        raise ValueError(f"{where}: unknown key(s) {', '.join(ctx + k for k in sorted(extra))}")
