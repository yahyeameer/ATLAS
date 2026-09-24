"""Broker server time <-> UTC (PRD §21: UTC everywhere, explicit conversion).

MT5 reports times as seconds since 1970 on the broker server's wall clock, not
UTC. Most FX brokers, FTMO's MT5 servers included, run on New York time plus
7 hours, so the server day starts at the 17:00 New York close and the offset
is UTC+2 in winter and UTC+3 in summer, switching on US DST dates. Both the
zone and the offset are settings; a wrong one shows up at once as clock drift
of an hour or more, which puts the engine in HALT.
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

EPOCH = dt.datetime(1970, 1, 1)


class ServerClock:
    def __init__(self, tz: str = "America/New_York", offset_hours: float = 7.0):
        self.tz = ZoneInfo(tz)
        self.name = tz
        self.offset = dt.timedelta(hours=offset_hours)

    def to_utc(self, server_seconds: float) -> dt.datetime:
        wall = EPOCH + dt.timedelta(seconds=float(server_seconds))
        return (wall - self.offset).replace(tzinfo=self.tz).astimezone(dt.timezone.utc)

    def to_server(self, utc: dt.datetime) -> float:
        if utc.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware")
        wall = utc.astimezone(self.tz).replace(tzinfo=None) + self.offset
        return (wall - EPOCH).total_seconds()

    def server_wall(self, utc: dt.datetime) -> dt.datetime:
        """Naive server wall-clock time, the form ``history_deals_get`` date arguments take."""
        return EPOCH + dt.timedelta(seconds=self.to_server(utc))
