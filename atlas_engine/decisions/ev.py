"""EV gate in R after costs (PRD §17).

    EV_R = p × R_target − (1 − p) × 1 − C_R

The gate takes a trade only when EV_R ≥ EV_min. A missing, non-finite or
out-of-range probability means skip, never a guess. The gate only says
yes or no to a setup the rules already produced; it has no say over risk,
size, prop rules or execution.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np


def ev_r(p, target_r, cost_r):
    """Expected R after costs of a trade that wins ``target_r`` with probability ``p`` or loses 1 R."""
    p = np.asarray(p, float)
    return p * np.asarray(target_r, float) - (1 - p) - np.asarray(cost_r, float)


def breakeven_p(target_r: float, cost_r: float, ev_min: float = 0.0) -> float:
    """Smallest probability the gate accepts."""
    return (1 + cost_r + ev_min) / (target_r + 1)


@dataclass(frozen=True)
class Decision:
    take: bool
    ev_r: float | None
    reason: str


@dataclass(frozen=True)
class EVGate:
    ev_min: float = 0.15

    def decide(self, p: float | None, target_r: float, cost_r: float) -> Decision:
        if p is None or isinstance(p, bool) or not isinstance(p, (int, float)) or not math.isfinite(p) or not 0.0 <= p <= 1.0:
            return Decision(False, None, "no_valid_probability")
        ev = float(ev_r(p, target_r, cost_r))
        if ev >= self.ev_min:
            return Decision(True, ev, "ev_above_min")
        return Decision(False, ev, "ev_below_min")

    def mask(self, p, target_r, cost_r) -> np.ndarray:
        """Vectorised ``decide(...).take`` for research; NaN or out-of-range p is skipped."""
        p = np.asarray(p, float)
        valid = np.isfinite(p) & (p >= 0) & (p <= 1)
        ev = ev_r(np.where(valid, p, 0.0), target_r, cost_r)
        return valid & (ev >= self.ev_min)
