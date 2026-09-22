"""Deterministic setup library (PRD §16)."""

from .base import EdgeFilters, Setup
from .liquidity_sweep import SETUP as LIQUIDITY_SWEEP
from .session_breakout import SETUP as SESSION_BREAKOUT
from .trend_pullback import SETUP as TREND_PULLBACK

SETUPS: dict[str, Setup] = {s.name: s for s in (TREND_PULLBACK, SESSION_BREAKOUT, LIQUIDITY_SWEEP)}

__all__ = ["EdgeFilters", "Setup", "SETUPS"]
