"""What the engine sees of a broker: plain, UTC-timed records (PRD §21).

The MT5 adapter converts the terminal's records into these, so the execution,
reconciliation and engine code never touches the ``MetaTrader5`` package or
broker server time.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass

from atlas_engine.sizing.lots import ContractSpec


class BrokerUnavailable(ConnectionError):
    """The terminal or the broker did not answer (the call returned None)."""


@dataclass(frozen=True)
class Tick:
    symbol: str
    time: dt.datetime  # UTC
    bid: float
    ask: float

    @property
    def spread(self) -> float:
        return self.ask - self.bid

    def price(self, direction: int, closing: bool = False) -> float:
        """Price a market order fills at: buys at the ask, sells at the bid."""
        buying = (direction == 1) != closing
        return self.ask if buying else self.bid


@dataclass(frozen=True)
class AccountInfo:
    login: int
    server: str
    currency: str
    balance: float
    equity: float
    trade_allowed: bool  # the account may trade and the terminal allows algo trading
    demo: bool


@dataclass(frozen=True)
class SymbolRules:
    symbol: str
    point: float
    digits: int
    stops_level: int  # points; SL/TP must be at least this far from the price
    freeze_level: int  # points; no modification while the price is this close to SL/TP
    filling_mode: int  # SYMBOL_FILLING_* bit flags
    trade_mode: str  # full | long_only | short_only | close_only | disabled
    spec: ContractSpec

    def round_price(self, price: float) -> float:
        return round(price, self.digits)


@dataclass(frozen=True)
class BrokerPosition:
    ticket: int
    symbol: str
    direction: int
    volume: float
    price_open: float
    sl: float  # 0.0 when none
    tp: float
    magic: int
    comment: str
    time: dt.datetime
    profit: float
    swap: float

    def to_dict(self) -> dict:
        d = asdict(self)
        d["time"] = self.time.isoformat()
        return d


@dataclass(frozen=True)
class BrokerDeal:
    ticket: int
    order: int
    position_id: int
    symbol: str
    direction: int  # +1 buy, -1 sell
    entry: str  # in | out | inout | out_by
    reason: str  # client | expert | sl | tp | so | ...
    volume: float
    price: float
    commission: float
    swap: float
    profit: float
    time: dt.datetime
    magic: int
    comment: str

    @property
    def net(self) -> float:
        return self.profit + self.commission + self.swap


@dataclass(frozen=True)
class SendResult:
    """Outcome of order_send. ``unknown`` means the call returned nothing: the
    order may or may not have reached the broker, so the caller must look
    before any retry (PRD §21)."""

    retcode: int | None
    comment: str
    status: str = "rejected"  # done | partial | requote | rejected | unknown | gone (position already closed)
    order: int = 0
    deal: int = 0
    volume: float = 0.0
    price: float = 0.0
    unknown: bool = False

    def to_dict(self) -> dict:
        return asdict(self)
