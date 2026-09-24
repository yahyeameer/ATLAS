"""MT5 adapter (PRD §21). ``MT5Adapter`` wraps the ``MetaTrader5`` package on the
Windows engine host; ``fake.FakeMT5`` stands in for it in tests and drills."""

from .adapter import MT5Adapter
from .clock import ServerClock

__all__ = ["MT5Adapter", "ServerClock"]
