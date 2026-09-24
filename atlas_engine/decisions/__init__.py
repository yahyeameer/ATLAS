"""Trade selection after the rules fire: decision state, regimes and the EV gate (PRD §16, §17)."""

from .ev import Decision, EVGate, breakeven_p, ev_r
from .state import CATEGORICAL, NUMERIC, PAYLOAD_KEYS, REGIMES, payload, regime_label, state_frame, with_returns

__all__ = [
    "CATEGORICAL", "NUMERIC", "PAYLOAD_KEYS", "REGIMES", "Decision", "EVGate", "breakeven_p", "ev_r", "payload",
    "regime_label", "state_frame", "with_returns",
]
