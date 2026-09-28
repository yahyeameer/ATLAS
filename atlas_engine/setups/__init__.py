"""Deterministic setup library (PRD §16)."""

from .base import EdgeFilters, Setup
from .channel_breakout import SETUP as CHANNEL_BREAKOUT
from .liquidity_sweep import SETUP as LIQUIDITY_SWEEP
from .session_breakout import SETUP as SESSION_BREAKOUT
from .trend_pullback import SETUP as TREND_PULLBACK
from .usd_seasonality import SETUP as USD_SEASONALITY

SETUPS: dict[str, Setup] = {s.name: s for s in (TREND_PULLBACK, SESSION_BREAKOUT, LIQUIDITY_SWEEP, CHANNEL_BREAKOUT, USD_SEASONALITY)}

__all__ = ["EdgeFilters", "Setup", "SETUPS"]
