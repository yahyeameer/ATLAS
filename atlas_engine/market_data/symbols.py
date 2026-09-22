"""Static symbol metadata shared by the data pipeline, backtester and engine."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SymbolSpec:
    name: str
    price_scale: int  # Dukascopy stores prices as integers; divide by this
    pip: float  # one pip in price units

    def pips(self, price_distance: float) -> float:
        return price_distance / self.pip


SYMBOLS: dict[str, SymbolSpec] = {
    "EURUSD": SymbolSpec("EURUSD", 100_000, 0.0001),
    "GBPUSD": SymbolSpec("GBPUSD", 100_000, 0.0001),
    "USDJPY": SymbolSpec("USDJPY", 1_000, 0.01),
    "XAUUSD": SymbolSpec("XAUUSD", 1_000, 0.1),
}


def spec(symbol: str) -> SymbolSpec:
    try:
        return SYMBOLS[symbol.upper()]
    except KeyError:
        raise KeyError(f"unknown symbol {symbol!r}; add it to atlas_engine.market_data.symbols") from None
