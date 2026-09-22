"""Research data access with the locked-holdout guard (PRD §13, §22).

Everything research code loads goes through here. Any request that reaches
into the holdout period raises, whoever asks. The human-run holdout step
(Phase T6) will get its own entry point outside the agent runtime; the
server-side refusal in ``atlas-backtest`` (Phase H2) is the real boundary,
and this guard keeps research code from ever needing holdout rows.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from atlas_engine.market_data import store


class HoldoutAccessError(PermissionError):
    pass


def check_not_holdout(end: pd.Timestamp, holdout_start: pd.Timestamp) -> None:
    if _utc(end) > _utc(holdout_start):
        raise HoldoutAccessError(
            f"requested data up to {end:%Y-%m-%d}, but the holdout starts {holdout_start:%Y-%m-%d}; "
            "research code never reads the holdout"
        )


def load_m1(root: Path, symbol: str, start, end, holdout_start) -> pd.DataFrame:
    check_not_holdout(pd.Timestamp(end), pd.Timestamp(holdout_start))
    return store.load_m1(root, symbol, _utc(start), _utc(end))


def _utc(ts) -> pd.Timestamp:
    ts = pd.Timestamp(ts)
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
