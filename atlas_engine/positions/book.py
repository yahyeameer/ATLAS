"""The engine's record of open positions: what it believes the broker holds.

Reconciliation compares this with the broker (atlas_engine.reconciliation);
the risk engine sees it as ``OpenPosition``s. It survives restarts through the
journal's state table.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass, replace

from atlas_engine.risk import OpenPosition
from atlas_engine.sizing.lots import ContractSpec


@dataclass(frozen=True)
class ManagedPosition:
    ticket: int
    client_id: str
    decision_id: str
    symbol: str
    setup: str
    direction: int
    volume: float
    entry: float
    stop: float
    target: float
    magic: int
    opened_at: str  # ISO UTC
    last_stop_bar: str | None = None  # bar in which the stop last moved (one change per bar, §18)

    def risk_amount(self, spec: ContractSpec, account_ccy: str = "USD", rates: dict | None = None) -> float:
        """Money lost if the stop filled now, commission included; 0 once the stop is at or past entry."""
        dist = self.direction * (self.entry - self.stop)
        if dist <= 0:
            return 0.0
        return dist / spec.tick_size * spec.tick_value(account_ccy, rates or {}) * self.volume + \
            spec.commission_per_lot * self.volume

    def to_risk(self, spec: ContractSpec, account_ccy: str = "USD", rates: dict | None = None) -> OpenPosition:
        return OpenPosition(str(self.ticket), self.symbol, self.setup, self.direction, self.volume, self.entry,
                            self.stop, self.risk_amount(spec, account_ccy, rates))

    def to_dict(self) -> dict:
        return asdict(self)


class PositionBook:
    def __init__(self, positions: dict[int, ManagedPosition] | None = None):
        self.positions: dict[int, ManagedPosition] = dict(positions or {})

    def add(self, p: ManagedPosition) -> None:
        self.positions[p.ticket] = p

    def remove(self, ticket: int) -> ManagedPosition | None:
        return self.positions.pop(ticket, None)

    def update(self, ticket: int, **changes) -> ManagedPosition:
        self.positions[ticket] = replace(self.positions[ticket], **changes)
        return self.positions[ticket]

    def get(self, ticket: int) -> ManagedPosition | None:
        return self.positions.get(ticket)

    def by_client_id(self, client_id: str) -> ManagedPosition | None:
        return next((p for p in self.positions.values() if p.client_id == client_id), None)

    def __iter__(self):
        return iter(list(self.positions.values()))

    def __len__(self) -> int:
        return len(self.positions)

    def to_dict(self) -> dict:
        return {"positions": [p.to_dict() for p in self.positions.values()]}

    @classmethod
    def from_dict(cls, d: dict | None) -> "PositionBook":
        return cls({int(p["ticket"]): ManagedPosition(**p) for p in (d or {}).get("positions", [])})


def iso(t: dt.datetime) -> str:
    return t.astimezone(dt.timezone.utc).isoformat()
