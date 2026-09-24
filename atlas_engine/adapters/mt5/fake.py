"""A fake MT5 terminal for tests and drills: the ``MetaTrader5`` calls the adapter
uses, with the package's own constants, and switches to inject failures.

The real package only runs on Windows next to a logged-in terminal, so T4's
failure-injection suite runs against this instead. Constant values are copied
from ``MetaTrader5`` 5.0.6180 (``MetaTrader5/__init__.py``); record fields use
the package's names. It is a model, not an emulator: one hedging account in a
USD-denominated currency, market orders only, broker-side SL/TP that trigger
when ``set_price`` crosses them (filling at the new price, so a gap fills
through the stop), and commission charged per side on every deal.

Faults (see ``FAULTS``): the terminal dies (every call returns None), the
broker link drops (orders come back CONNECTION), ``order_send`` times out
after filling or without filling, requotes and other reject codes, partial
fills, slippage, the broker dropping the SL, algo trading switched off,
positions opened or closed behind the engine's back, frozen quotes and a
skewed tick clock.

Nothing here talks to a network or a broker.
"""

from __future__ import annotations

import datetime as dt
import itertools
from collections import namedtuple
from dataclasses import dataclass, field
from typing import Callable

import numpy as np

from .clock import ServerClock

TerminalInfo = namedtuple("TerminalInfo", "connected trade_allowed ping_last")
AccountInfo = namedtuple("AccountInfo", "login trade_mode leverage balance profit equity margin margin_free currency "
                                        "server trade_allowed trade_expert")
SymbolInfo = namedtuple("SymbolInfo", "name visible digits spread point trade_tick_value trade_tick_value_profit "
                                      "trade_tick_value_loss trade_tick_size trade_contract_size volume_min volume_max "
                                      "volume_step trade_stops_level trade_freeze_level filling_mode trade_mode "
                                      "currency_base currency_profit bid ask time")
Tick = namedtuple("Tick", "time bid ask last volume time_msc flags")
TradePosition = namedtuple("TradePosition", "ticket time time_msc type magic identifier reason volume price_open sl tp "
                                            "price_current swap profit symbol comment")
TradeDeal = namedtuple("TradeDeal", "ticket order time time_msc type entry magic position_id reason volume price "
                                    "commission swap profit fee symbol comment")
OrderCheckResult = namedtuple("OrderCheckResult", "retcode balance equity profit margin margin_free comment request")
OrderSendResult = namedtuple("OrderSendResult", "retcode deal order volume price bid ask comment request_id "
                                                "retcode_external request")

RATE_DTYPE = np.dtype([("time", "<i8"), ("open", "<f8"), ("high", "<f8"), ("low", "<f8"), ("close", "<f8"),
                       ("tick_volume", "<u8"), ("spread", "<i4"), ("real_volume", "<u8")])

# name -> (base, quote, digits, contract size)
SYMBOLS = {"EURUSD": ("EUR", "USD", 5, 100_000), "GBPUSD": ("GBP", "USD", 5, 100_000),
           "USDJPY": ("USD", "JPY", 3, 100_000), "XAUUSD": ("XAU", "USD", 2, 100)}
START_PRICES = {"EURUSD": (1.17000, 1.17002), "GBPUSD": (1.34000, 1.34005), "USDJPY": (148.000, 148.003),
                "XAUUSD": (2650.00, 2650.25)}

FAULTS = ("timeout_filled", "timeout_lost", "retcode", "partial", "slippage", "drop_sl")


@dataclass
class _Symbol:
    name: str
    base: str
    quote: str
    digits: int
    contract_size: float
    bid: float
    ask: float
    tick_time: dt.datetime
    stops_level: int = 10
    freeze_level: int = 0
    filling_mode: int = 2  # IOC
    trade_mode: int = 4  # full
    volume_min: float = 0.01
    volume_step: float = 0.01
    volume_max: float = 100.0
    frozen: bool = False

    @property
    def point(self) -> float:
        return 10.0 ** -self.digits


@dataclass
class _Position:
    ticket: int
    symbol: str
    direction: int
    volume: float
    price_open: float
    sl: float
    tp: float
    magic: int
    comment: str
    time: dt.datetime
    swap: float = 0.0


@dataclass
class _Deal:
    ticket: int
    order: int
    time: dt.datetime
    direction: int
    entry: int
    magic: int
    position_id: int
    reason: int
    volume: float
    price: float
    commission: float
    profit: float
    symbol: str
    comment: str


@dataclass
class _Fault:
    kind: str
    params: dict = field(default_factory=dict)


class FakeMT5:
    # --- constants, values from MetaTrader5 5.0.6180
    TIMEFRAME_M1 = 1
    POSITION_TYPE_BUY, POSITION_TYPE_SELL = 0, 1
    ORDER_TYPE_BUY, ORDER_TYPE_SELL = 0, 1
    ORDER_FILLING_FOK, ORDER_FILLING_IOC, ORDER_FILLING_RETURN = 0, 1, 2
    ORDER_TIME_GTC = 0
    DEAL_TYPE_BUY, DEAL_TYPE_SELL, DEAL_TYPE_BALANCE = 0, 1, 2
    DEAL_ENTRY_IN, DEAL_ENTRY_OUT = 0, 1
    DEAL_REASON_CLIENT, DEAL_REASON_EXPERT, DEAL_REASON_SL, DEAL_REASON_TP, DEAL_REASON_SO = 0, 3, 4, 5, 6
    TRADE_ACTION_DEAL, TRADE_ACTION_PENDING, TRADE_ACTION_SLTP, TRADE_ACTION_REMOVE = 1, 5, 6, 8
    ACCOUNT_TRADE_MODE_DEMO, ACCOUNT_TRADE_MODE_REAL = 0, 2
    TRADE_RETCODE_REQUOTE = 10004
    TRADE_RETCODE_REJECT = 10006
    TRADE_RETCODE_DONE = 10009
    TRADE_RETCODE_DONE_PARTIAL = 10010
    TRADE_RETCODE_TIMEOUT = 10012
    TRADE_RETCODE_INVALID = 10013
    TRADE_RETCODE_INVALID_VOLUME = 10014
    TRADE_RETCODE_INVALID_PRICE = 10015
    TRADE_RETCODE_INVALID_STOPS = 10016
    TRADE_RETCODE_TRADE_DISABLED = 10017
    TRADE_RETCODE_MARKET_CLOSED = 10018
    TRADE_RETCODE_NO_MONEY = 10019
    TRADE_RETCODE_PRICE_CHANGED = 10020
    TRADE_RETCODE_PRICE_OFF = 10021
    TRADE_RETCODE_TOO_MANY_REQUESTS = 10024
    TRADE_RETCODE_NO_CHANGES = 10025
    TRADE_RETCODE_CLIENT_DISABLES_AT = 10027
    TRADE_RETCODE_FROZEN = 10029
    TRADE_RETCODE_INVALID_FILL = 10030
    TRADE_RETCODE_CONNECTION = 10031
    TRADE_RETCODE_POSITION_CLOSED = 10036
    RES_S_OK, RES_E_FAIL, RES_E_INTERNAL_FAIL_CONNECT = 1, -1, -10004

    def __init__(self, now: Callable[[], dt.datetime], clock: ServerClock | None = None, balance: float = 10_000.0,
                 symbols: tuple[str, ...] = ("EURUSD", "GBPUSD"), commission_per_side: float = 3.5,
                 demo: bool = True, live_quotes: bool = False):
        self.now = now
        self.live_quotes = live_quotes  # drills: every quote read is a fresh tick at the same price
        self.clock = clock or ServerClock()
        self.balance = balance
        self.commission_per_side = commission_per_side
        self.demo = demo
        self.initialized = False
        self.terminal_alive = True
        self.broker_connected = True
        self.algo_trading = True
        self.account_trade_allowed = True
        self.tick_skew_s = 0.0  # added to every tick time (server clock ahead of ours)
        self.symbols = {s: _Symbol(s, *SYMBOLS[s][:2], SYMBOLS[s][2], SYMBOLS[s][3], *START_PRICES[s], now())
                        for s in symbols}
        self.positions: dict[int, _Position] = {}
        self.deals: list[_Deal] = []
        self.faults: list[_Fault] = []
        self.requests: list[dict] = []  # every order_send request, for assertions
        self.rates: dict[str, np.ndarray] = {}
        self._ids = itertools.count(500_001)
        self._error = (self.RES_S_OK, "Success")

    # ================================================================ test controls

    def inject(self, kind: str, **params) -> None:
        """Queue a fault for the next order_send (``FAULTS``)."""
        if kind not in FAULTS:
            raise ValueError(f"unknown fault {kind!r}; known: {', '.join(FAULTS)}")
        self.faults.append(_Fault(kind, params))

    def set_price(self, symbol: str, bid: float, ask: float | None = None, advance_tick: bool = True) -> None:
        s = self.symbols[symbol]
        s.bid, s.ask = bid, ask if ask is not None else bid + (s.ask - s.bid)
        if advance_tick and not s.frozen:
            s.tick_time = self.now()
        self._trigger_stops(symbol)

    def touch(self) -> None:
        """Refresh every unfrozen symbol's tick time to now (a live market)."""
        for s in self.symbols.values():
            if not s.frozen:
                s.tick_time = self.now()

    def open_external(self, symbol: str, direction: int, volume: float, sl: float = 0.0, tp: float = 0.0,
                      magic: int = 0, comment: str = "") -> int:
        """A position the engine did not open (manual, another EA, or a lost fill)."""
        s = self.symbols[symbol]
        price = s.ask if direction == 1 else s.bid
        return self._fill_open(symbol, direction, volume, price, sl, tp, magic, comment, self.DEAL_REASON_CLIENT, 0)

    def close_external(self, ticket: int, reason: int | None = None, price: float | None = None) -> None:
        """Close a position as the broker would on SL/TP, or as a manual close would."""
        p = self.positions[ticket]
        s = self.symbols[p.symbol]
        px = price if price is not None else (s.bid if p.direction == 1 else s.ask)
        self._fill_close(p, p.volume, px, self.DEAL_REASON_SL if reason is None else reason, 0, "")

    def remove_sl(self, ticket: int) -> None:
        self.positions[ticket].sl = 0.0

    def load_rates(self, symbol: str, m1_bid_ask) -> None:
        """Bars ``copy_rates_from_pos`` serves: a bid/ask M1 frame (atlas_engine.market_data.bars)."""
        df = m1_bid_ask
        s = self.symbols[symbol]
        arr = np.zeros(len(df), dtype=RATE_DTYPE)
        arr["time"] = [int(self.clock.to_server(t.to_pydatetime())) for t in df.index]
        arr["open"], arr["high"], arr["low"], arr["close"] = (df[f"bid_{c}"].to_numpy() for c in "ohlc")
        arr["spread"] = np.round((df["ask_c"] - df["bid_c"]).to_numpy() / s.point).astype(int)
        arr["tick_volume"] = df["volume"].to_numpy().astype(np.uint64)
        self.rates[symbol] = arr

    # ================================================================ MetaTrader5 API

    def initialize(self, path: str | None = None, **kw) -> bool:
        if not self.terminal_alive:
            self._error = (self.RES_E_INTERNAL_FAIL_CONNECT, "IPC initialize failed, MetaTrader 5 x64 not found")
            return False
        self.initialized = True
        return True

    def shutdown(self) -> None:
        self.initialized = False

    def last_error(self) -> tuple[int, str]:
        return self._error

    def _alive(self) -> bool:
        if not (self.initialized and self.terminal_alive):
            self._error = (self.RES_E_INTERNAL_FAIL_CONNECT, "No IPC connection")
            return False
        return True

    def terminal_info(self):
        if not self._alive():
            return None
        return TerminalInfo(self.broker_connected, self.algo_trading, 12_000)

    def account_info(self):
        if not self._alive():
            return None
        floating = sum(self._profit(p) + p.swap for p in self.positions.values())
        return AccountInfo(5_550_001, self.ACCOUNT_TRADE_MODE_DEMO if self.demo else self.ACCOUNT_TRADE_MODE_REAL,
                           100, round(self.balance, 2), round(floating, 2), round(self.balance + floating, 2), 0.0,
                           self.balance + floating, "USD", "FakeBroker-Demo", self.account_trade_allowed, True)

    def symbol_select(self, symbol: str, enable: bool = True) -> bool:
        return self._alive() and symbol in self.symbols

    def symbol_info(self, symbol: str):
        if not self._alive() or symbol not in self.symbols:
            return None
        s = self.symbols[symbol]
        tick_size = s.point
        tv = tick_size * s.contract_size * (1.0 if s.quote == "USD" else 1.0 / s.bid)
        return SymbolInfo(s.name, True, s.digits, round((s.ask - s.bid) / s.point), s.point, tv, tv, tv, tick_size,
                          s.contract_size, s.volume_min, s.volume_max, s.volume_step, s.stops_level, s.freeze_level,
                          s.filling_mode, s.trade_mode, s.base, s.quote, s.bid, s.ask,
                          int(self.clock.to_server(s.tick_time)))

    def symbol_info_tick(self, symbol: str):
        if not self._alive() or symbol not in self.symbols:
            return None
        s = self.symbols[symbol]
        if self.live_quotes and not s.frozen:
            s.tick_time = self.now()
        server = self.clock.to_server(s.tick_time) + self.tick_skew_s
        return Tick(int(server), s.bid, s.ask, 0.0, 0, int(server * 1000), 6)

    def positions_get(self, symbol: str | None = None, group: str | None = None, ticket: int | None = None):
        if not self._alive():
            return None
        rows = [p for p in self.positions.values()
                if (symbol is None or p.symbol == symbol) and (ticket is None or p.ticket == ticket)]
        return tuple(self._position_record(p) for p in rows)

    def orders_get(self, symbol: str | None = None, group: str | None = None, ticket: int | None = None):
        return () if self._alive() else None

    def history_deals_get(self, date_from=None, date_to=None, group: str | None = None, ticket: int | None = None,
                          position: int | None = None):
        if not self._alive():
            return None
        rows = self.deals
        if position is not None:
            rows = [d for d in rows if d.position_id == position]
        elif ticket is not None:
            rows = [d for d in rows if d.order == ticket]
        else:
            lo, hi = (self.clock.to_utc((x - dt.datetime(1970, 1, 1)).total_seconds()) for x in (date_from, date_to))
            rows = [d for d in rows if lo <= d.time <= hi]
        return tuple(self._deal_record(d) for d in rows)

    def copy_rates_from_pos(self, symbol: str, timeframe: int, start_pos: int, count: int):
        if not self._alive() or symbol not in self.rates:
            return None
        arr = self.rates[symbol]
        arr = arr[arr["time"] <= self.clock.to_server(self.now())]  # forming bar included, like MT5
        end = len(arr) - start_pos
        return arr[max(0, end - count):end]

    def order_check(self, request: dict):
        if not self._alive():
            return None
        code, comment = self._validate(request, checking=True)
        eq = self.account_info().equity
        return OrderCheckResult(0 if code == self.TRADE_RETCODE_DONE else code, eq, eq, 0.0, 0.0, eq, comment, request)

    def order_send(self, request: dict):
        if not self._alive():
            return None
        self.requests.append(dict(request))
        fault = self.faults.pop(0) if self.faults else None
        if fault and fault.kind == "timeout_lost":
            self._error = (self.RES_E_FAIL, "Terminal: request timed out")
            return None
        if fault and fault.kind == "retcode":
            return self._result(int(fault.params["code"]), request, comment=fault.params.get("comment", "rejected"))
        code, comment = self._validate(request, checking=False)
        if code != self.TRADE_RETCODE_DONE:
            return self._result(code, request, comment=comment)
        action = request["action"]
        if action == self.TRADE_ACTION_SLTP:
            p = self.positions[int(request["position"])]
            p.sl, p.tp = float(request.get("sl", 0.0)), float(request.get("tp", 0.0))
            return self._result(self.TRADE_RETCODE_DONE, request, comment="Request executed")
        s = self.symbols[request["symbol"]]
        direction = 1 if request["type"] == self.ORDER_TYPE_BUY else -1
        volume = float(request["volume"])
        slip = float((fault.params.get("points", 5) if fault and fault.kind == "slippage" else 0)) * s.point
        code = self.TRADE_RETCODE_DONE
        if fault and fault.kind == "partial":
            volume = round(volume * float(fault.params.get("fraction", 0.5)) / s.volume_step) * s.volume_step
            code = self.TRADE_RETCODE_DONE_PARTIAL
        price = (s.ask if direction == 1 else s.bid) + direction * slip
        order = next(self._ids)
        if request.get("position"):
            p = self.positions[int(request["position"])]
            deal = self._fill_close(p, min(volume, p.volume), price, self.DEAL_REASON_EXPERT, order,
                                    request.get("comment", ""))
            ticket = p.ticket
        else:
            sl = 0.0 if fault and fault.kind == "drop_sl" else float(request.get("sl", 0.0))
            ticket = self._fill_open(s.name, direction, volume, price, sl, float(request.get("tp", 0.0)),
                                     int(request.get("magic", 0)), str(request.get("comment", ""))[:31],
                                     self.DEAL_REASON_EXPERT, order)
            deal = self.deals[-1].ticket
        if fault and fault.kind == "timeout_filled":
            self._error = (self.RES_E_FAIL, "Terminal: request timed out")
            return None
        return self._result(code, request, order=ticket if not request.get("position") else order, deal=deal,
                            volume=volume, price=price, comment="Request executed")

    # ================================================================ internals

    def _result(self, code, request, order=0, deal=0, volume=0.0, price=0.0, comment=""):
        s = self.symbols.get(request.get("symbol", ""))
        return OrderSendResult(code, deal, order, volume, price, s.bid if s else 0.0, s.ask if s else 0.0, comment,
                               0, 0, request)

    def _validate(self, r: dict, checking: bool) -> tuple[int, str]:
        if not self.broker_connected:
            return self.TRADE_RETCODE_CONNECTION, "No connection with the trade server"
        if not self.algo_trading:
            return self.TRADE_RETCODE_CLIENT_DISABLES_AT, "AutoTrading disabled by client"
        if not self.account_trade_allowed:
            return self.TRADE_RETCODE_TRADE_DISABLED, "Trade is disabled"
        if r.get("action") == self.TRADE_ACTION_SLTP:
            p = self.positions.get(int(r.get("position", 0)))
            if p is None:
                return self.TRADE_RETCODE_POSITION_CLOSED, "Position doesn't exist"
            s = self.symbols[p.symbol]
            return self._check_stops(s, p.direction, s.bid if p.direction == 1 else s.ask, r)
        if r.get("action") != self.TRADE_ACTION_DEAL:
            return self.TRADE_RETCODE_INVALID, "Invalid request"
        s = self.symbols.get(r.get("symbol"))
        if s is None:
            return self.TRADE_RETCODE_INVALID, "Unknown symbol"
        if s.trade_mode == 0:
            return self.TRADE_RETCODE_TRADE_DISABLED, "Trade is disabled for the symbol"
        vol = float(r.get("volume", 0))
        steps = vol / s.volume_step
        if not (s.volume_min - 1e-9 <= vol <= s.volume_max + 1e-9) or abs(steps - round(steps)) > 1e-6:
            return self.TRADE_RETCODE_INVALID_VOLUME, "Invalid volume"
        allowed = {self.ORDER_FILLING_FOK} if s.filling_mode & 1 else set()
        allowed |= {self.ORDER_FILLING_IOC} if s.filling_mode & 2 else set()
        if r.get("type_filling", self.ORDER_FILLING_FOK) not in allowed:
            return self.TRADE_RETCODE_INVALID_FILL, "Unsupported filling mode"
        direction = 1 if r.get("type") == self.ORDER_TYPE_BUY else -1
        market = s.ask if direction == 1 else s.bid
        if r.get("position"):
            p = self.positions.get(int(r["position"]))
            if p is None:
                return self.TRADE_RETCODE_POSITION_CLOSED, "Position doesn't exist"
            if vol > p.volume + 1e-9:
                return self.TRADE_RETCODE_INVALID_VOLUME, "Close volume exceeds position"
        else:
            code, comment = self._check_stops(s, direction, market, r)
            if code != self.TRADE_RETCODE_DONE:
                return code, comment
        if not checking and abs(float(r.get("price", market)) - market) > int(r.get("deviation", 0)) * s.point + 1e-12:
            return self.TRADE_RETCODE_REQUOTE, "Requote"
        return self.TRADE_RETCODE_DONE, "Done"

    def _check_stops(self, s: _Symbol, direction: int, price: float, r: dict) -> tuple[int, str]:
        min_dist = s.stops_level * s.point
        sl, tp = float(r.get("sl", 0.0) or 0.0), float(r.get("tp", 0.0) or 0.0)
        if sl and (direction * (price - sl) < min_dist - 1e-12):
            return self.TRADE_RETCODE_INVALID_STOPS, "Invalid stops"
        if tp and (direction * (tp - price) < min_dist - 1e-12):
            return self.TRADE_RETCODE_INVALID_STOPS, "Invalid stops"
        return self.TRADE_RETCODE_DONE, "Done"

    def _fill_open(self, symbol, direction, volume, price, sl, tp, magic, comment, reason, order) -> int:
        ticket = order or next(self._ids)
        now = self.now()
        self.positions[ticket] = _Position(ticket, symbol, direction, volume, price, sl, tp, magic, comment, now)
        self._deal(ticket, ticket, now, direction, self.DEAL_ENTRY_IN, magic, reason, volume, price, 0.0, symbol, comment)
        return ticket

    def _fill_close(self, p: _Position, volume: float, price: float, reason: int, order: int, comment: str) -> int:
        profit = self._profit(p, price, volume)
        self._deal(order or next(self._ids), p.ticket, self.now(), -p.direction, self.DEAL_ENTRY_OUT, p.magic, reason,
                   volume, price, profit, p.symbol, comment or p.comment)
        p.volume = round(p.volume - volume, 8)
        if p.volume <= 1e-9:
            del self.positions[p.ticket]
        return self.deals[-1].ticket

    def _deal(self, order, position_id, time, direction, entry, magic, reason, volume, price, profit, symbol, comment):
        commission = -self.commission_per_side * volume
        self.balance += profit + commission
        self.deals.append(_Deal(next(self._ids), order, time, direction, entry, magic, position_id, reason, volume, price,
                                commission, round(profit, 2), symbol, comment))

    def _profit(self, p: _Position, price: float | None = None, volume: float | None = None) -> float:
        s = self.symbols[p.symbol]
        px = price if price is not None else (s.bid if p.direction == 1 else s.ask)
        raw = p.direction * (px - p.price_open) * (p.volume if volume is None else volume) * s.contract_size
        return raw if s.quote == "USD" else raw / px

    def _trigger_stops(self, symbol: str) -> None:
        s = self.symbols[symbol]
        for p in [p for p in self.positions.values() if p.symbol == symbol]:
            px = s.bid if p.direction == 1 else s.ask
            if p.sl and p.direction * (px - p.sl) <= 0:
                self._fill_close(p, p.volume, px, self.DEAL_REASON_SL, 0, "[sl]")
            elif p.tp and p.direction * (px - p.tp) >= 0:
                self._fill_close(p, p.volume, px, self.DEAL_REASON_TP, 0, "[tp]")

    def _position_record(self, p: _Position):
        server = int(self.clock.to_server(p.time))
        s = self.symbols[p.symbol]
        return TradePosition(p.ticket, server, server * 1000,
                             self.POSITION_TYPE_BUY if p.direction == 1 else self.POSITION_TYPE_SELL, p.magic,
                             p.ticket, self.DEAL_REASON_EXPERT, p.volume, p.price_open, p.sl, p.tp,
                             s.bid if p.direction == 1 else s.ask, p.swap, round(self._profit(p), 2), p.symbol,
                             p.comment)

    def _deal_record(self, d: _Deal):
        server = int(self.clock.to_server(d.time))
        return TradeDeal(d.ticket, d.order, server, server * 1000,
                         self.DEAL_TYPE_BUY if d.direction == 1 else self.DEAL_TYPE_SELL, d.entry, d.magic,
                         d.position_id, d.reason, d.volume, d.price, d.commission, 0.0, d.profit, 0.0, d.symbol,
                         d.comment)
