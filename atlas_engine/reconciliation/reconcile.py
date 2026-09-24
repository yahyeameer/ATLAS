"""Compare the engine's position book with the broker (PRD §21). Pure: it finds, the engine acts.

Run at startup and every 60 s. Each finding says what the engine should do:

    kind               what it means                                  engine action
    orphan_position    ATLAS position at the broker, not in the book   adopt if the journal knows its order,
                                                                       else the orphan policy (close / attach SL)
    foreign_position   a position ATLAS did not open                   none: stays a mismatch (HALT) until
                                                                       the operator deals with it
    missing_position   in the book, gone at the broker                 mark closed from deal history;
                                                                       a mismatch if no closing deal is found
    stop_missing       broker has no SL                                re-attach the book's SL
    stop_loosened      broker SL further away than the book's          restore the book's SL
    stop_tightened     broker SL tighter than the book's               adopt it (tighter is allowed)
    target_mismatch    broker TP differs from the book                 restore the book's TP
    volume_mismatch    broker volume differs (partial close)           adopt the broker's volume

A finding the engine can't resolve stays a mismatch, and any mismatch is a
HALT (atlas_engine.ops.health).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from atlas_engine.adapters.broker import BrokerPosition
from atlas_engine.positions.book import PositionBook

CLIENT_PREFIX = "ATL-"


@dataclass(frozen=True)
class Finding:
    kind: str
    ticket: int
    symbol: str
    detail: str
    broker: BrokerPosition | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["broker"] = self.broker.to_dict() if self.broker else None
        return d


def is_atlas(p: BrokerPosition, magic_base: int) -> bool:
    return p.comment.startswith(CLIENT_PREFIX) or magic_base <= p.magic < magic_base + 1000


def diff(book: PositionBook, broker: list[BrokerPosition], magic_base: int, point: dict[str, float]) -> list[Finding]:
    out: list[Finding] = []
    at_broker = {p.ticket: p for p in broker}
    for b in broker:
        mine = book.get(b.ticket)
        if mine is None:
            kind = "orphan_position" if is_atlas(b, magic_base) else "foreign_position"
            out.append(Finding(kind, b.ticket, b.symbol, f"{'long' if b.direction == 1 else 'short'} {b.volume} lots, "
                               f"magic {b.magic}, comment {b.comment!r}, sl {b.sl or 'none'}", b))
            continue
        tol = point.get(b.symbol, 1e-5) / 2
        if not b.sl:
            out.append(Finding("stop_missing", b.ticket, b.symbol, f"book stop {mine.stop}", b))
        elif abs(b.sl - mine.stop) > tol:
            tighter = mine.direction * (b.sl - mine.stop) > 0
            out.append(Finding("stop_tightened" if tighter else "stop_loosened", b.ticket, b.symbol,
                               f"broker sl {b.sl}, book {mine.stop}", b))
        if abs((b.tp or 0.0) - (mine.target or 0.0)) > tol:
            out.append(Finding("target_mismatch", b.ticket, b.symbol, f"broker tp {b.tp or 'none'}, book {mine.target}", b))
        if abs(b.volume - mine.volume) > 1e-9:
            out.append(Finding("volume_mismatch", b.ticket, b.symbol, f"broker {b.volume} lots, book {mine.volume}", b))
    for mine in book:
        if mine.ticket not in at_broker:
            out.append(Finding("missing_position", mine.ticket, mine.symbol,
                               f"{mine.setup} {mine.client_id} not at the broker"))
    return out
