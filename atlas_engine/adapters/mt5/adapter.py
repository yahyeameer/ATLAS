"""The MT5 adapter: the only code that calls the ``MetaTrader5`` package (PRD §21).

The package is Windows-only and drives a running MT5 terminal, so it runs on
the engine's Windows VPS. The adapter takes the module as an argument: on the
engine host that is ``import MetaTrader5``, in tests it is
``atlas_engine.adapters.mt5.fake.FakeMT5``, which implements the same calls
and constants. Nothing here decides anything; it converts the terminal's
records into ``atlas_engine.adapters.broker`` types with UTC times and turns
"the call returned None" into ``BrokerUnavailable``.

Credentials never pass through code in this repo: ``connect`` reads them from
the engine host's environment (``ATLAS_MT5_LOGIN``, ``ATLAS_MT5_PASSWORD``,
``ATLAS_MT5_SERVER``, ``ATLAS_MT5_PATH``), or uses the terminal's saved login
when they are unset.
"""

from __future__ import annotations

import datetime as dt
import os
import time
from typing import Any

from atlas_engine.adapters.broker import (
    AccountInfo, BrokerDeal, BrokerPosition, BrokerUnavailable, SendResult, SymbolRules, Tick,
)
from atlas_engine.sizing.lots import ContractSpec, contract

from .clock import ServerClock

# SYMBOL_FILLING_* flags in symbol_info().filling_mode (MQL5 docs; the Python package does not export them).
SYMBOL_FILLING_FOK = 1
SYMBOL_FILLING_IOC = 2

TRADE_MODES = {0: "disabled", 1: "long_only", 2: "short_only", 3: "close_only", 4: "full"}
DEAL_ENTRIES = {0: "in", 1: "out", 2: "inout", 3: "out_by"}
DEAL_REASONS = {0: "client", 1: "mobile", 2: "web", 3: "expert", 4: "sl", 5: "tp", 6: "so",
                7: "rollover", 8: "vmargin", 9: "split"}
COMMENT_MAX = 31  # MT5 keeps at most 31 characters of an order comment


class MT5Adapter:
    def __init__(self, mt5: Any, clock: ServerClock | None = None, commission_per_lot: dict[str, float] | None = None,
                 history_days: int = 7):
        self.mt5 = mt5
        self.clock = clock or ServerClock()
        self.commission = commission_per_lot or {}
        self.history_days = history_days
        self.last_ok: float | None = None  # monotonic time of the last call the terminal answered
        self.requests_sent = 0  # order_send calls, for the firm's daily request cap

    # ------------------------------------------------------------------ connection

    def connect(self) -> None:
        kw: dict[str, Any] = {}
        if os.environ.get("ATLAS_MT5_PATH"):
            kw["path"] = os.environ["ATLAS_MT5_PATH"]
        if os.environ.get("ATLAS_MT5_LOGIN"):
            kw.update(login=int(os.environ["ATLAS_MT5_LOGIN"]), password=os.environ.get("ATLAS_MT5_PASSWORD", ""),
                      server=os.environ.get("ATLAS_MT5_SERVER", ""))
        if not self.mt5.initialize(**kw):
            raise BrokerUnavailable(f"MT5 initialize failed: {self.mt5.last_error()}")
        self._ok()

    def shutdown(self) -> None:
        self.mt5.shutdown()

    def connected(self) -> bool:
        """True when the terminal answers and is connected to the broker's trade server."""
        info = self.mt5.terminal_info()
        if info is None:
            return False
        self._ok()
        return bool(info.connected)

    def _ok(self) -> None:
        self.last_ok = time.monotonic()

    def _need(self, value, what: str):
        if value is None:
            raise BrokerUnavailable(f"MT5 {what} returned nothing: {self.mt5.last_error()}")
        self._ok()
        return value

    # ------------------------------------------------------------------ account and symbols

    def account(self) -> AccountInfo:
        a = self._need(self.mt5.account_info(), "account_info")
        term = self.mt5.terminal_info()
        algo_ok = bool(term.trade_allowed) if term is not None else False
        return AccountInfo(int(a.login), str(a.server), str(a.currency), float(a.balance), float(a.equity),
                           bool(a.trade_allowed) and bool(a.trade_expert) and algo_ok,
                           a.trade_mode == self.mt5.ACCOUNT_TRADE_MODE_DEMO)

    def symbol_rules(self, symbol: str) -> SymbolRules:
        info = self.mt5.symbol_info(symbol)
        if info is not None and not info.visible:
            self.mt5.symbol_select(symbol, True)
        info = self._need(self.mt5.symbol_info(symbol), f"symbol_info({symbol})")
        try:
            base = contract(symbol)
            commission = self.commission.get(symbol, base.commission_per_lot)
        except KeyError:
            commission = self.commission.get(symbol, 0.0)
        loss_tv = float(getattr(info, "trade_tick_value_loss", 0.0) or 0.0)
        spec = ContractSpec(
            symbol=symbol, base=str(info.currency_base), quote=str(info.currency_profit),
            tick_size=float(info.trade_tick_size), contract_size=float(info.trade_contract_size),
            volume_min=float(info.volume_min), volume_step=float(info.volume_step), volume_max=float(info.volume_max),
            commission_per_lot=commission, tick_value_loss=loss_tv if loss_tv > 0 else None,
        )
        return SymbolRules(symbol, float(info.point), int(info.digits), int(info.trade_stops_level),
                           int(info.trade_freeze_level), int(info.filling_mode),
                           TRADE_MODES.get(int(info.trade_mode), "disabled"), spec)

    def tick(self, symbol: str) -> Tick:
        t = self._need(self.mt5.symbol_info_tick(symbol), f"symbol_info_tick({symbol})")
        ms = getattr(t, "time_msc", 0) or 0
        at = self.clock.to_utc(ms / 1000 if ms else t.time)
        return Tick(symbol, at, float(t.bid), float(t.ask))

    # ------------------------------------------------------------------ positions and history

    def positions(self) -> list[BrokerPosition]:
        rows = self._need(self.mt5.positions_get(), "positions_get")
        return [self._position(p) for p in rows]

    def position(self, ticket: int) -> BrokerPosition | None:
        rows = self.mt5.positions_get(ticket=ticket)
        if rows is None:
            raise BrokerUnavailable(f"MT5 positions_get(ticket={ticket}) returned nothing: {self.mt5.last_error()}")
        self._ok()
        return self._position(rows[0]) if rows else None

    def _position(self, p) -> BrokerPosition:
        return BrokerPosition(int(p.ticket), str(p.symbol), 1 if p.type == self.mt5.POSITION_TYPE_BUY else -1,
                              float(p.volume), float(p.price_open), float(p.sl), float(p.tp), int(p.magic),
                              str(p.comment), self.clock.to_utc(p.time), float(p.profit), float(p.swap))

    def pending_orders(self) -> list[dict]:
        rows = self._need(self.mt5.orders_get(), "orders_get")
        return [{"ticket": int(o.ticket), "symbol": str(o.symbol), "magic": int(o.magic), "comment": str(o.comment)}
                for o in rows]

    def deals(self, since: dt.datetime, until: dt.datetime) -> list[BrokerDeal]:
        """Trade deals between two UTC times. The terminal takes server wall-clock
        dates; the window is widened by a day each side and filtered after
        conversion, so a wrong guess about how it reads them can't lose a deal."""
        pad = dt.timedelta(days=1)
        rows = self.mt5.history_deals_get(self.clock.server_wall(since - pad), self.clock.server_wall(until + pad))
        if rows is None:
            raise BrokerUnavailable(f"MT5 history_deals_get returned nothing: {self.mt5.last_error()}")
        self._ok()
        out = []
        for d in rows:
            if d.type not in (self.mt5.DEAL_TYPE_BUY, self.mt5.DEAL_TYPE_SELL):
                continue  # balance, credit and fee entries
            deal = BrokerDeal(int(d.ticket), int(d.order), int(d.position_id), str(d.symbol),
                              1 if d.type == self.mt5.DEAL_TYPE_BUY else -1, DEAL_ENTRIES.get(int(d.entry), "?"),
                              DEAL_REASONS.get(int(d.reason), "other"), float(d.volume), float(d.price),
                              float(d.commission), float(d.swap), float(d.profit), self.clock.to_utc(d.time),
                              int(d.magic), str(d.comment))
            if since <= deal.time <= until:
                out.append(deal)
        return out

    def recent_deals(self, now: dt.datetime) -> list[BrokerDeal]:
        return self.deals(now - dt.timedelta(days=self.history_days), now + dt.timedelta(minutes=5))

    # ------------------------------------------------------------------ trading

    def check(self, request: dict) -> tuple[bool, int, str]:
        """order_check: (accepted, retcode, comment). The terminal answers retcode 0 when the request would pass."""
        r = self._need(self.mt5.order_check(request), "order_check")
        return int(r.retcode) == 0, int(r.retcode), str(r.comment)

    def send(self, request: dict) -> SendResult:
        self.requests_sent += 1
        r = self.mt5.order_send(request)
        if r is None:
            return SendResult(None, f"order_send returned nothing: {self.mt5.last_error()}", "unknown", unknown=True)
        self._ok()
        m = self.mt5
        code = int(r.retcode)
        status = {m.TRADE_RETCODE_DONE: "done", m.TRADE_RETCODE_DONE_PARTIAL: "partial",
                  m.TRADE_RETCODE_REQUOTE: "requote", m.TRADE_RETCODE_PRICE_CHANGED: "requote",
                  m.TRADE_RETCODE_PRICE_OFF: "requote", m.TRADE_RETCODE_TIMEOUT: "unknown",
                  m.TRADE_RETCODE_CONNECTION: "unknown", m.TRADE_RETCODE_POSITION_CLOSED: "gone"}.get(code, "rejected")
        return SendResult(code, str(r.comment), status, int(r.order), int(r.deal), float(r.volume), float(r.price),
                          unknown=status == "unknown")

    def filling(self, rules: SymbolRules) -> int:
        """Filling type the symbol allows: IOC, else FOK, else RETURN (PRD §21)."""
        if rules.filling_mode & SYMBOL_FILLING_IOC:
            return self.mt5.ORDER_FILLING_IOC
        if rules.filling_mode & SYMBOL_FILLING_FOK:
            return self.mt5.ORDER_FILLING_FOK
        return self.mt5.ORDER_FILLING_RETURN

    def market_request(self, rules: SymbolRules, direction: int, volume: float, price: float, *, deviation: int,
                       magic: int, comment: str, sl: float = 0.0, tp: float = 0.0, position: int | None = None) -> dict:
        """A market order; with ``position`` it closes that position. SL and TP go in the same request."""
        m = self.mt5
        req = {"action": m.TRADE_ACTION_DEAL, "symbol": rules.symbol, "volume": float(volume),
               "type": m.ORDER_TYPE_BUY if direction == 1 else m.ORDER_TYPE_SELL,
               "price": rules.round_price(price), "deviation": int(deviation), "magic": int(magic),
               "comment": comment[:COMMENT_MAX], "type_time": m.ORDER_TIME_GTC, "type_filling": self.filling(rules)}
        if position is not None:
            req["position"] = int(position)
        else:
            req["sl"], req["tp"] = rules.round_price(sl), rules.round_price(tp) if tp else 0.0
        return req

    def sltp_request(self, rules: SymbolRules, position: BrokerPosition, sl: float, tp: float) -> dict:
        return {"action": self.mt5.TRADE_ACTION_SLTP, "symbol": rules.symbol, "position": int(position.ticket),
                "sl": rules.round_price(sl) if sl else 0.0, "tp": rules.round_price(tp) if tp else 0.0,
                "magic": int(position.magic)}

    def m1_rates(self, symbol: str, count: int):
        """The last ``count`` M1 bars, forming bar included (numpy record array, server times)."""
        return self._need(self.mt5.copy_rates_from_pos(symbol, self.mt5.TIMEFRAME_M1, 0, count),
                          f"copy_rates_from_pos({symbol})")
