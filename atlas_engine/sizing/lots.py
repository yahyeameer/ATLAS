"""Lot sizing from a risk budget (PRD §20).

    lots = (equity x risk% x m) / ((SL_distance / tick_size) x tick_value)

Rounded down to ``volume_step``, clamped to the symbol's and the firm's
limits, and rejected when ``volume_min`` would overshoot the budget by more
than ``max_overshoot``. In T4 the MT5 adapter fills ``ContractSpec`` from
``symbol_info()``; the static table below is for research and tests.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass

_EPS = 1e-9


@dataclass(frozen=True)
class ContractSpec:
    symbol: str
    base: str  # first currency, or the metal (XAU)
    quote: str
    tick_size: float
    contract_size: float
    volume_min: float = 0.01
    volume_step: float = 0.01
    volume_max: float = 100.0
    commission_per_lot: float = 0.0  # round turn, account currency; counted in the risk
    tick_value_loss: float | None = None  # MT5 trade_tick_value_loss when the broker reports it

    def tick_value(self, account_ccy: str, rates: dict[str, float] | None = None) -> float:
        """Value of one tick for one lot, in account currency."""
        if self.tick_value_loss is not None:
            return self.tick_value_loss
        return self.tick_size * self.contract_size * conversion_rate(self.quote, account_ccy, rates or {})


# Defaults for an FTMO-style MT5 account. Commission is the $7/lot round-turn
# placeholder T0 uses; T4 replaces all of this with the broker's own numbers.
CONTRACTS: dict[str, ContractSpec] = {
    "EURUSD": ContractSpec("EURUSD", "EUR", "USD", 0.00001, 100_000, commission_per_lot=7.0),
    "GBPUSD": ContractSpec("GBPUSD", "GBP", "USD", 0.00001, 100_000, commission_per_lot=7.0),
    "USDJPY": ContractSpec("USDJPY", "USD", "JPY", 0.001, 100_000, commission_per_lot=7.0),
    "XAUUSD": ContractSpec("XAUUSD", "XAU", "USD", 0.01, 100, volume_max=50.0, commission_per_lot=7.0),
}


def contract(symbol: str) -> ContractSpec:
    try:
        return CONTRACTS[symbol.upper()]
    except KeyError:
        raise KeyError(f"no contract spec for {symbol!r}") from None


def conversion_rate(ccy: str, account_ccy: str, rates: dict[str, float]) -> float:
    """Units of ``account_ccy`` per unit of ``ccy`` from a dict of pair mid prices."""
    if ccy == account_ccy:
        return 1.0
    if f"{ccy}{account_ccy}" in rates:
        return rates[f"{ccy}{account_ccy}"]
    if f"{account_ccy}{ccy}" in rates:
        return 1.0 / rates[f"{account_ccy}{ccy}"]
    raise KeyError(f"need a {ccy}{account_ccy} or {account_ccy}{ccy} rate to convert {ccy} to {account_ccy}")


@dataclass(frozen=True)
class SizeResult:
    ok: bool
    volume: float
    risk_amount: float  # money lost at the stop for ``volume``, commission included
    budget: float
    reason: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def risk_per_lot(spec: ContractSpec, sl_distance: float, account_ccy: str, rates: dict[str, float] | None = None) -> float:
    ticks = sl_distance / spec.tick_size
    return ticks * spec.tick_value(account_ccy, rates) + spec.commission_per_lot


def size_position(
    equity: float,
    risk_pct: float,
    multiplier: float,
    sl_distance: float,
    spec: ContractSpec,
    account_ccy: str = "USD",
    rates: dict[str, float] | None = None,
    max_lot: float | None = None,
    max_overshoot: float = 1.10,
) -> SizeResult:
    """Lots for a trade risking ``risk_pct`` x ``multiplier`` of ``equity`` to its stop."""
    budget = equity * risk_pct / 100 * multiplier
    if not (equity > 0 and budget > 0):
        return SizeResult(False, 0.0, 0.0, max(budget, 0.0), "no_risk_budget")
    if not sl_distance > 0 or not math.isfinite(sl_distance):
        return SizeResult(False, 0.0, 0.0, budget, "invalid_stop_distance")
    per_lot = risk_per_lot(spec, sl_distance, account_ccy, rates)
    if not per_lot > 0:
        return SizeResult(False, 0.0, 0.0, budget, "invalid_tick_value")
    steps = math.floor(budget / per_lot / spec.volume_step + _EPS)
    volume = steps * spec.volume_step
    cap = spec.volume_max if max_lot is None else min(spec.volume_max, max_lot)
    volume = min(volume, math.floor(cap / spec.volume_step + _EPS) * spec.volume_step)
    if volume + _EPS < spec.volume_min:
        if spec.volume_min * per_lot > budget * max_overshoot + _EPS:
            return SizeResult(False, 0.0, 0.0, budget, "min_volume_exceeds_budget")
        volume = spec.volume_min
    if volume > cap + _EPS:
        return SizeResult(False, 0.0, 0.0, budget, "min_volume_exceeds_max_lot")
    volume = round(volume, 8)
    return SizeResult(True, volume, volume * per_lot, budget)
