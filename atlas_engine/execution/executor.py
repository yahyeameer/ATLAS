"""Order execution rules (PRD §18, §21). Broker-neutral: it speaks to the adapter only.

- Every entry carries its SL and TP in the same request, the strategy's magic
  number, and a client order ID in the comment derived from the decision ID.
- ``order_check`` runs before every ``order_send``.
- SL/TP must clear the symbol's stops level; the spread must be at most the
  configured share of the stop distance; the filling mode comes from the
  symbol; deviation is capped.
- When ``order_send`` gives no answer (timeout, lost connection) the executor
  looks for the client order ID in positions and deal history before it
  retries, and retries at most once. The same decision can never open twice.
- A fill whose SL did not stick at the broker gets it attached at once; if
  that fails, the position is closed. ATLAS never holds an unprotected position.
- Stop changes only tighten, at most once per bar, and respect the freeze level.
"""

from __future__ import annotations

import base64
import hashlib
from dataclasses import asdict, dataclass

from atlas_engine.adapters.broker import BrokerPosition, BrokerUnavailable, SymbolRules, Tick

from .settings import ExecutionSettings


def client_order_id(decision_id: str) -> str:
    """Stable, short (20 chars) and unique per decision: the idempotency key sent in the order comment."""
    digest = base64.b32encode(hashlib.sha256(decision_id.encode()).digest()).decode()
    return "ATL-" + digest[:16]


@dataclass(frozen=True)
class EntryOrder:
    decision_id: str
    symbol: str
    setup: str
    direction: int
    volume: float
    stop: float
    target: float
    magic: int
    expected_price: float  # price when the decision was taken

    @property
    def client_id(self) -> str:
        return client_order_id(self.decision_id)


@dataclass(frozen=True)
class ExecResult:
    status: str  # filled | partial | duplicate | rejected | unprotected_closed | closed | failed
    client_id: str
    reason: str = ""
    ticket: int | None = None
    volume: float = 0.0
    price: float | None = None
    requested_price: float | None = None
    slippage_points: float | None = None
    spread_at_fill: float | None = None
    sl: float | None = None
    tp: float | None = None
    retcode: int | None = None
    attempts: int = 0

    @property
    def opened(self) -> bool:
        return self.status in ("filled", "partial", "duplicate")

    def to_dict(self) -> dict:
        return asdict(self)


class Executor:
    def __init__(self, broker, settings: ExecutionSettings):
        self.broker = broker
        self.settings = settings

    # ------------------------------------------------------------------ lookups

    def find_open(self, client_id: str) -> BrokerPosition | None:
        """The live position this client order ID opened, if any."""
        return next((p for p in self.broker.positions() if p.comment == client_id), None)

    def entered_before(self, client_id: str, now) -> bool:
        """Whether any entry deal ever carried this client order ID (it may be closed already)."""
        return any(d.comment == client_id and d.entry == "in" for d in self.broker.recent_deals(now))

    # ------------------------------------------------------------------ validation

    def validate_entry(self, o: EntryOrder, rules: SymbolRules, tick: Tick) -> list[str]:
        problems = []
        if o.direction not in (1, -1):
            return ["invalid_direction"]
        if rules.trade_mode == "disabled" or rules.trade_mode == "close_only":
            problems.append(f"symbol_trade_mode_{rules.trade_mode}")
        if (rules.trade_mode == "long_only" and o.direction == -1) or (rules.trade_mode == "short_only" and o.direction == 1):
            problems.append(f"symbol_trade_mode_{rules.trade_mode}")
        price = tick.price(o.direction)
        min_dist = rules.stops_level * rules.point
        stop_dist = o.direction * (price - o.stop)
        if not o.stop or stop_dist <= 0:
            problems.append("missing_or_wrong_side_stop")
        elif stop_dist + 1e-12 < min_dist:
            problems.append("stop_inside_stops_level")
        if not o.target or o.direction * (o.target - price) <= 0:
            problems.append("missing_or_wrong_side_target")
        elif o.direction * (o.target - price) + 1e-12 < min_dist:
            problems.append("target_inside_stops_level")
        if stop_dist > 0 and tick.spread > self.settings.max_spread_to_stop_ratio * stop_dist + 1e-12:
            problems.append("spread_too_wide_for_stop")
        spec = rules.spec
        steps = o.volume / spec.volume_step
        if not spec.volume_min - 1e-9 <= o.volume <= spec.volume_max + 1e-9 or abs(steps - round(steps)) > 1e-6:
            problems.append("invalid_volume")
        return problems

    # ------------------------------------------------------------------ entries

    def open(self, o: EntryOrder, now) -> ExecResult:
        cid = o.client_id
        existing = self.find_open(cid)
        if existing is not None:
            return self._filled(o, existing, "duplicate", None, 0, reason="client order ID already open")
        if self.entered_before(cid, now):
            return ExecResult("duplicate", cid, "this decision already traded and closed", attempts=0)
        rules = self.broker.symbol_rules(o.symbol)
        dev = self.settings.max_deviation_points
        attempts = 0
        last_reason = ""
        while attempts < 2:
            tick = self.broker.tick(o.symbol)
            price = tick.price(o.direction)
            if attempts and abs(price - o.expected_price) > dev * rules.point + 1e-12:
                return ExecResult("rejected", cid, f"price moved past deviation after: {last_reason}", attempts=attempts)
            problems = self.validate_entry(o, rules, tick)
            if problems:
                return ExecResult("rejected", cid, ",".join(problems), attempts=attempts)
            req = self.broker.market_request(rules, o.direction, o.volume, price, deviation=dev, magic=o.magic,
                                             comment=cid, sl=o.stop, tp=o.target)
            ok, code, comment = self.broker.check(req)
            if not ok:
                return ExecResult("rejected", cid, f"order_check {code}: {comment}", retcode=code, attempts=attempts)
            attempts += 1
            res = self.broker.send(req)
            if res.status in ("done", "partial"):
                pos = self.broker.position(res.order) if res.order else None
                pos = pos or self.find_open(cid)
                if pos is None:
                    return ExecResult("failed", cid, "broker reported a fill but no position carries the client ID",
                                      retcode=res.retcode, attempts=attempts)
                return self._filled(o, pos, "partial" if pos.volume + 1e-9 < o.volume else "filled", price, attempts,
                                    retcode=res.retcode, rules=rules)
            if res.status == "unknown":
                # PRD §21: on a timeout, look before any retry.
                pos = self.find_open(cid)
                if pos is not None:
                    return self._filled(o, pos, "partial" if pos.volume + 1e-9 < o.volume else "filled", price,
                                        attempts, reason="filled despite the timeout", rules=rules)
                if self.entered_before(cid, now):
                    return ExecResult("duplicate", cid, "filled during the timeout and already closed",
                                      attempts=attempts)
                last_reason = res.comment
                continue
            if res.status == "requote":
                last_reason = f"requote {res.retcode}"
                continue
            return ExecResult("rejected", cid, f"order_send {res.retcode}: {res.comment}", retcode=res.retcode,
                              attempts=attempts)
        return ExecResult("rejected", cid, f"gave up after {attempts} attempts: {last_reason}", attempts=attempts)

    def _filled(self, o: EntryOrder, pos: BrokerPosition, status: str, requested: float | None, attempts: int,
                retcode: int | None = None, reason: str = "", rules: SymbolRules | None = None) -> ExecResult:
        rules = rules or self.broker.symbol_rules(o.symbol)
        if not pos.sl:
            fixed = self._attach(rules, pos, o.stop, o.target)
            if not fixed:
                closed = self.close(pos, "sl_not_attached")
                return ExecResult("unprotected_closed", o.client_id,
                                  f"broker dropped the SL and it could not be attached; close: {closed.status}",
                                  ticket=pos.ticket, volume=pos.volume, price=pos.price_open, attempts=attempts)
            pos = self.broker.position(pos.ticket) or pos
            reason = (reason + "; " if reason else "") + "SL re-attached after fill"
        slip = None
        if requested is not None:
            slip = round(o.direction * (pos.price_open - requested) / rules.point, 1)  # >0 = worse than asked
        spread = None
        try:
            t = self.broker.tick(o.symbol)
            spread = t.spread
        except BrokerUnavailable:
            pass
        return ExecResult(status, o.client_id, reason, pos.ticket, pos.volume, pos.price_open, requested, slip, spread,
                          pos.sl, pos.tp, retcode, attempts)

    def _attach(self, rules: SymbolRules, pos: BrokerPosition, sl: float, tp: float) -> bool:
        for _ in range(2):
            res = self.broker.send(self.broker.sltp_request(rules, pos, sl, tp))
            if res.status == "done":
                return True
            if res.status == "unknown":
                now = self.broker.position(pos.ticket)
                if now is not None and now.sl:
                    return True
        return False

    # ------------------------------------------------------------------ management

    def modify_stop(self, pos: BrokerPosition, new_sl: float) -> ExecResult:
        """Move the SL. Only tightening is allowed (PRD §18); the caller enforces once per bar."""
        cid = pos.comment
        rules = self.broker.symbol_rules(pos.symbol)
        tick = self.broker.tick(pos.symbol)
        price = tick.price(pos.direction, closing=True)
        if pos.sl and pos.direction * (new_sl - pos.sl) <= 0:
            return ExecResult("rejected", cid, "stops only tighten", ticket=pos.ticket)
        if pos.direction * (price - new_sl) < rules.stops_level * rules.point - 1e-12:
            return ExecResult("rejected", cid, "new stop inside the stops level", ticket=pos.ticket)
        frz = rules.freeze_level * rules.point
        if frz and any(lvl and abs(price - lvl) <= frz for lvl in (pos.sl, pos.tp)):
            return ExecResult("rejected", cid, "position is inside the freeze level", ticket=pos.ticket)
        res = self.broker.send(self.broker.sltp_request(rules, pos, new_sl, pos.tp))
        if res.status == "done":
            return ExecResult("filled", cid, "stop moved", ticket=pos.ticket, sl=new_sl, tp=pos.tp, retcode=res.retcode,
                              attempts=1)
        return ExecResult("rejected", cid, f"modify {res.retcode}: {res.comment}", ticket=pos.ticket,
                          retcode=res.retcode, attempts=1)

    def restore_stops(self, pos: BrokerPosition, sl: float, tp: float) -> bool:
        return self._attach(self.broker.symbol_rules(pos.symbol), pos, sl, tp)

    def close(self, pos: BrokerPosition, reason: str) -> ExecResult:
        rules = self.broker.symbol_rules(pos.symbol)
        attempts = 0
        while attempts < 2:
            tick = self.broker.tick(pos.symbol)
            req = self.broker.market_request(rules, -pos.direction, pos.volume, tick.price(pos.direction, closing=True),
                                             deviation=max(self.settings.max_deviation_points, 10), magic=pos.magic,
                                             comment=pos.comment, position=pos.ticket)
            attempts += 1
            res = self.broker.send(req)
            if res.status in ("done", "partial") or res.status == "unknown":
                still = self.broker.position(pos.ticket)
                if still is None:
                    return ExecResult("closed", pos.comment, reason, pos.ticket, pos.volume, res.price or None,
                                      retcode=res.retcode, attempts=attempts)
                pos = still
                continue
            if res.status == "requote":
                continue
            if res.status == "gone":
                return ExecResult("closed", pos.comment, reason + " (already closed)", pos.ticket, attempts=attempts)
            return ExecResult("failed", pos.comment, f"close {res.retcode}: {res.comment}", pos.ticket,
                              retcode=res.retcode, attempts=attempts)
        return ExecResult("failed", pos.comment, f"close gave up after {attempts} attempts", pos.ticket,
                          attempts=attempts)

    def flatten(self, positions: list[BrokerPosition], reason: str) -> list[ExecResult]:
        return [self.close(p, reason) for p in positions]
