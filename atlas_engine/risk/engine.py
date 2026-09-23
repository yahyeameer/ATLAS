"""The risk engine: account limits, per-trade checks and lot sizing (PRD §19, §20).

Deterministic and strategy-agnostic. The engine loop calls ``observe`` on
every equity update, ``check_entry`` before any order, and ``record_open`` /
``record_close`` as fills arrive. It never places, closes or modifies an
order itself: it returns what the execution layer must do (flatten, stop new
trades) and a journal-ready record of every decision.

Latches:

- ``day_stopped``  internal daily loss >= soft share of the firm's daily loss.
  No new trades until the next server day.
- ``day_killed``   internal daily loss >= hard share. Flatten, no new trades
  until the next server day.
- ``drawdown_stopped``  loss from the firm's max-loss reference >= the stop
  share. No new trades until the operator re-enables (``operator_reenable``).
- ``firm_breached``  a firm rule was violated. Permanent for this account.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass, field

from atlas_engine.config import EngineConfig
from atlas_engine.exposure.netting import CorrelationMatrix, Exposure, correlated_with, currency_risk, legs
from atlas_engine.sizing.lots import ContractSpec, contract, size_position

_EPS = 1e-9


@dataclass(frozen=True)
class OpenPosition:
    ticket: str
    symbol: str
    setup: str
    direction: int
    volume: float
    entry: float
    stop: float
    risk_amount: float  # money lost if the stop fills now; 0 once the stop is at or past entry


@dataclass(frozen=True)
class AccountSnapshot:
    time: dt.datetime
    balance: float
    equity: float  # balance + floating P/L, swaps, commissions
    positions: tuple[OpenPosition, ...] = ()


@dataclass(frozen=True)
class TradeProposal:
    symbol: str
    setup: str
    direction: int
    entry: float
    stop: float
    ev_scale: float = 1.0  # optional EV scaling; clamped to [0, 1], never above base risk
    rates: dict[str, float] = field(default_factory=dict)  # mid prices for currency conversion
    news_events: tuple[dt.datetime, ...] = ()  # restricted releases near entry (prop rules)
    spec: ContractSpec | None = None  # broker's contract spec; static table if None


@dataclass
class RiskState:
    initial_balance: float
    day: dt.date | None = None
    day_start_balance: float = 0.0
    day_start_equity: float = 0.0
    high_water: float = 0.0
    trades_today: int = 0
    loss_streak: int = 0
    reduced_trades_left: int = 0
    day_stopped: bool = False
    day_killed: bool = False
    drawdown_stopped: bool = False
    firm_breached: list[str] = field(default_factory=list)
    trading_days: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["day"] = self.day.isoformat() if self.day else None
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "RiskState":
        d = dict(d)
        d["day"] = dt.date.fromisoformat(d["day"]) if d.get("day") else None
        return cls(**d)


@dataclass(frozen=True)
class Assessment:
    status: str  # ok | day_stopped | day_killed | drawdown_stopped | firm_breached
    new_trades_allowed: bool
    flatten: bool
    requires_operator: bool
    multiplier: float
    metrics: dict

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    volume: float
    risk_amount: float
    risk_pct: float
    multiplier: float
    reasons: tuple[str, ...]
    checks: dict

    def to_dict(self) -> dict:
        d = asdict(self)
        d["reasons"] = list(self.reasons)
        return d


class RiskEngine:
    def __init__(self, cfg: EngineConfig, state: RiskState | None = None):
        self.cfg = cfg
        self.state = state or RiskState(cfg.initial_balance, high_water=cfg.initial_balance)
        self.events: list[dict] = []  # latch changes for the journal; the caller drains it

    # -- account monitoring ---------------------------------------------------

    def observe(self, snap: AccountSnapshot) -> Assessment:
        s, p, r = self.state, self.cfg.prop, self.cfg.risk
        day = p.server_day(snap.time)
        if s.day != day:
            self._roll_day(day, snap)
        if p.max_loss_type == "trailing_intraday":
            s.high_water = max(s.high_water, snap.equity)
        lines = self.lines()

        breached = p.breached(snap.equity, s.initial_balance, s.day_start_balance, s.day_start_equity, s.high_water)
        for rule in breached:
            if rule not in s.firm_breached:
                s.firm_breached.append(rule)
                self._event(snap.time, "firm_breached", rule=rule, equity=snap.equity)
        if snap.equity <= lines["daily_hard"] + _EPS and not s.day_killed:
            s.day_killed = s.day_stopped = True
            self._event(snap.time, "day_killed", equity=snap.equity, line=lines["daily_hard"])
        elif snap.equity <= lines["daily_soft"] + _EPS and not s.day_stopped:
            s.day_stopped = True
            self._event(snap.time, "day_stopped", equity=snap.equity, line=lines["daily_soft"])
        if snap.equity <= lines["drawdown_stop"] + _EPS and not s.drawdown_stopped:
            s.drawdown_stopped = True
            self._event(snap.time, "drawdown_stopped", equity=snap.equity, line=lines["drawdown_stop"])

        if s.firm_breached:
            status = "firm_breached"
        elif s.day_killed:
            status = "day_killed"
        elif s.drawdown_stopped:
            status = "drawdown_stopped"
        elif s.day_stopped:
            status = "day_stopped"
        else:
            status = "ok"
        halved = snap.equity <= lines["drawdown_halve"] + _EPS or s.reduced_trades_left > 0
        return Assessment(
            status=status,
            new_trades_allowed=status == "ok",
            flatten=s.day_killed or bool(s.firm_breached),
            requires_operator=s.drawdown_stopped or bool(s.firm_breached),
            multiplier=r.reduced_risk_multiplier if halved else 1.0,
            metrics={"equity": snap.equity, "balance": snap.balance, "day": str(s.day),
                     "day_start_balance": s.day_start_balance, "day_start_equity": s.day_start_equity,
                     "trades_today": s.trades_today, "loss_streak": s.loss_streak,
                     "reduced_trades_left": s.reduced_trades_left, **lines},
        )

    def lines(self) -> dict[str, float]:
        """Equity levels of every limit for the current server day."""
        s, p, r = self.state, self.cfg.prop, self.cfg.risk
        firm_daily = p.daily_floor(s.initial_balance, s.day_start_balance, s.day_start_equity)
        daily_amt = p.daily_loss_amount(s.initial_balance, s.day_start_balance, s.day_start_equity)
        # Internal daily loss counts from the higher of day-start balance and
        # equity, so floating profit carried overnight is protected too.
        ref = max(s.day_start_balance, s.day_start_equity)
        firm_max = p.max_loss_floor(s.initial_balance, s.high_water)
        max_amt = p.max_loss_amount(s.initial_balance)
        stop_share = r.drawdown_stop_pct_of_firm
        return {
            "firm_daily_floor": firm_daily,
            "firm_max_loss_floor": firm_max,
            "daily_soft": max(firm_daily + (1 - r.daily_loss_soft_pct_of_firm) * daily_amt,
                              ref - r.daily_loss_soft_pct_of_firm * daily_amt),
            "daily_hard": max(firm_daily + (1 - r.daily_loss_hard_pct_of_firm) * daily_amt,
                              ref - r.daily_loss_hard_pct_of_firm * daily_amt),
            "drawdown_stop": firm_max + (1 - stop_share) * max_amt,
            "drawdown_halve": firm_max + (1 - r.drawdown_halve_pct_of_internal * stop_share) * max_amt,
        }

    def _roll_day(self, day: dt.date, snap: AccountSnapshot) -> None:
        s = self.state
        if self.cfg.prop.max_loss_type == "trailing_eod" and s.day is not None:
            s.high_water = max(s.high_water, snap.balance)
        s.day, s.day_start_balance, s.day_start_equity = day, snap.balance, snap.equity
        s.trades_today = 0
        if s.day_stopped or s.day_killed:
            self._event(snap.time, "day_reset")
        s.day_stopped = s.day_killed = False

    # -- per-trade checks -----------------------------------------------------

    def check_entry(self, prop: TradeProposal, snap: AccountSnapshot,
                    correlation: CorrelationMatrix | None = None) -> RiskDecision:
        cfg, r, s = self.cfg, self.cfg.risk, self.state
        a = self.observe(snap)
        reasons: list[str] = []
        checks: dict = {"status": a.status}
        if not a.new_trades_allowed:
            reasons.append(f"account_{a.status}")
        if cfg.mode not in ("evaluation", "funded", "paper"):
            reasons.append("mode_not_tradeable")
        if prop.symbol.upper() not in cfg.symbols:
            reasons.append("symbol_not_enabled")
        if prop.direction not in (1, -1):
            reasons.append("invalid_direction")
        sl_distance = prop.direction * (prop.entry - prop.stop)
        if not sl_distance > 0:
            reasons.append("stop_on_wrong_side")
        if any(p.symbol == prop.symbol and p.setup == prop.setup for p in snap.positions):
            reasons.append("position_exists_for_setup")  # one per setup per symbol; no pyramiding
        if s.trades_today >= r.max_trades_per_day:
            reasons.append("max_trades_per_day")
        restr = cfg.prop.restrictions_for(cfg.mode)
        if restr.news_blocked(snap.time, list(prop.news_events)):
            reasons.append("prop_news_window")

        m = a.multiplier * min(max(prop.ev_scale, 0.0), 1.0)
        checks["multiplier"] = m
        spec = prop.spec or contract(prop.symbol)
        size = size_position(snap.equity, r.risk_per_trade_pct, m, max(sl_distance, 0.0), spec, cfg.currency,
                             prop.rates, cfg.prop.max_lot, r.max_sizing_overshoot)
        checks["sizing"] = size.to_dict()
        if not size.ok and "stop_on_wrong_side" not in reasons:
            reasons.append(size.reason or "sizing_failed")
        new_risk = size.risk_amount if size.ok else 0.0
        new_pct = new_risk / snap.equity * 100 if snap.equity > 0 else 0.0

        open_risk = sum(max(p.risk_amount, 0.0) for p in snap.positions)
        checks["open_risk_pct"] = (open_risk + new_risk) / snap.equity * 100 if snap.equity > 0 else None
        if open_risk + new_risk > r.max_open_risk_pct / 100 * snap.equity + _EPS:
            reasons.append("max_open_risk")

        # A trade may never make a firm breach possible: if every open stop and
        # this one filled now, equity must stay above both firm floors.
        lines = self.lines()
        worst = snap.equity - open_risk - new_risk
        checks["worst_case_equity"] = worst
        if worst <= max(lines["firm_daily_floor"], lines["firm_max_loss_floor"]) + _EPS:
            reasons.append("firm_limit_headroom")

        if prop.direction in (1, -1) and size.ok:
            new_exp = Exposure(prop.symbol, prop.direction, new_pct)
            open_exp = [Exposure(p.symbol, p.direction, max(p.risk_amount, 0.0) / snap.equity * 100) for p in snap.positions]
            before, after = currency_risk(open_exp), currency_risk(open_exp + [new_exp])
            checks["currency_risk_pct"] = after
            cap = r.max_currency_risk_pct
            for ccy in legs(new_exp):
                if abs(after[ccy]) > cap + _EPS and abs(after[ccy]) > abs(before.get(ccy, 0.0)) + _EPS:
                    reasons.append(f"currency_risk_{ccy}")
            matrix = correlation
            if matrix is not None and (snap.time.date() - matrix.as_of).days > r.correlation_max_age_days:
                matrix, checks["correlation_stale"] = None, True
            cluster, rule = correlated_with(new_exp, open_exp, matrix, r.correlation_threshold)
            cluster_pct = new_pct + sum(e.risk_pct for e in cluster)
            checks["correlation"] = {"rule": rule, "cluster": [e.symbol for e in cluster], "cluster_risk_pct": cluster_pct}
            if cluster and cluster_pct > r.one_bet_max_risk_pct * r.max_sizing_overshoot + _EPS:
                reasons.append("correlated_one_bet")

        ok = not reasons
        return RiskDecision(ok, size.volume if ok else 0.0, new_risk if ok else 0.0, new_pct if ok else 0.0,
                            m, tuple(reasons), checks)

    # -- fills ----------------------------------------------------------------

    def record_open(self, time: dt.datetime) -> None:
        s = self.state
        day = self.cfg.prop.server_day(time)
        if s.day != day:
            raise ValueError("observe() the account for this server day before recording a fill")
        s.trades_today += 1
        if str(day) not in s.trading_days:
            s.trading_days.append(str(day))
        if s.reduced_trades_left > 0:
            s.reduced_trades_left -= 1

    def record_close(self, time: dt.datetime, pnl: float) -> None:
        s, r = self.state, self.cfg.risk
        if pnl < 0:
            s.loss_streak += 1
            if s.loss_streak >= r.loss_streak_halve_after:
                s.reduced_trades_left = r.loss_streak_halve_trades
                s.loss_streak = 0
                self._event(time, "loss_streak_halving", trades=r.loss_streak_halve_trades)
        else:
            s.loss_streak = 0

    # -- operator ---------------------------------------------------------------

    def operator_reenable(self, time: dt.datetime, operator: str) -> None:
        """Clear the drawdown stop. The caller must have verified the operator's
        signature (PRD §11 layer 4); Hermes and agents never reach this. The
        stop re-latches at once if equity is still past the line."""
        if self.state.firm_breached:
            raise PermissionError("a firm breach cannot be re-enabled")
        self.state.drawdown_stopped = False
        self._event(time, "drawdown_reenabled", operator=operator)

    # -- evaluation progress --------------------------------------------------

    def phase_progress(self, balance: float) -> dict:
        s, ph = self.state, self.cfg.prop.phases[self.cfg.phase]
        target = None if ph.profit_target_pct is None else s.initial_balance * (1 + ph.profit_target_pct / 100)
        return {"phase": self.cfg.phase, "target_balance": target, "trading_days": len(s.trading_days),
                "min_trading_days": ph.min_trading_days,
                "passed": target is not None and balance >= target and len(s.trading_days) >= ph.min_trading_days
                and not s.firm_breached}

    def _event(self, time: dt.datetime, kind: str, **data) -> None:
        self.events.append({"time": time.isoformat(), "event": kind, **data})
