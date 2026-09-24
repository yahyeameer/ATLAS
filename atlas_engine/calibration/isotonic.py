"""Isotonic calibration and scoring (PRD §17).

The engine refits one calibrator per symbol group weekly on the last 500
closed trades. The fit is pool-adjacent-violators on (raw score, outcome),
stored as breakpoints so it serialises to JSON for the ``calibration_models``
table and replays exactly. Scores between breakpoints interpolate linearly;
scores outside the fitted range take the end values.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

import numpy as np
import pandas as pd


def fit_isotonic(x, y, weight=None) -> tuple[np.ndarray, np.ndarray]:
    """Non-decreasing least-squares fit of ``y`` on ``x``. Returns (breakpoints, values)."""
    x = np.asarray(x, float)
    y = np.asarray(y, float)
    w = np.ones_like(y) if weight is None else np.asarray(weight, float)
    ok = np.isfinite(x) & np.isfinite(y)
    x, y, w = x[ok], y[ok], w[ok]
    if len(x) == 0:
        return np.array([]), np.array([])
    order = np.argsort(x, kind="mergesort")
    x, y, w = x[order], y[order], w[order]
    # Ties in x share one value, so collapse them first.
    ux, start = np.unique(x, return_index=True)
    ws = np.add.reduceat(w, start)
    ys = np.add.reduceat(w * y, start) / ws
    # Pool adjacent violators over blocks of (mean, weight, first index, last index).
    means, weights, lo, hi = [], [], [], []
    for i in range(len(ux)):
        means.append(ys[i]); weights.append(ws[i]); lo.append(i); hi.append(i)
        while len(means) > 1 and means[-2] > means[-1]:
            m2, w2, h2 = means.pop(), weights.pop(), hi.pop()
            lo.pop()
            wsum = weights[-1] + w2
            means[-1] = (means[-1] * weights[-1] + m2 * w2) / wsum
            weights[-1] = wsum
            hi[-1] = h2
    # Each block contributes its two end breakpoints at the block mean.
    bx, by = [], []
    for m, a, b in zip(means, lo, hi):
        bx.append(ux[a]); by.append(m)
        if b != a:
            bx.append(ux[b]); by.append(m)
    return np.array(bx), np.array(by)


@dataclass
class Calibrator:
    """A fitted isotonic map from a model's raw score to P(target first)."""

    x: list[float] = field(default_factory=list)
    y: list[float] = field(default_factory=list)
    n: int = 0
    base_rate: float = float("nan")
    source: str = ""  # model version the raw scores came from
    group: str = ""  # symbol group

    @classmethod
    def fit(cls, raw, outcome, window: int | None = None, source: str = "", group: str = "") -> "Calibrator":
        raw = np.asarray(raw, float)
        outcome = np.asarray(outcome, float)
        if window is not None:
            raw, outcome = raw[-window:], outcome[-window:]
        bx, by = fit_isotonic(raw, outcome)
        rate = float(np.nanmean(outcome)) if len(outcome) else float("nan")
        return cls(bx.tolist(), by.tolist(), int(len(outcome)), rate, source, group)

    def __call__(self, raw) -> np.ndarray:
        raw = np.asarray(raw, float)
        if not self.x:
            return np.full(raw.shape, self.base_rate)
        out = np.interp(raw, self.x, self.y)
        return np.where(np.isfinite(raw), out, np.nan)

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    @classmethod
    def from_json(cls, s: str) -> "Calibrator":
        return cls(**json.loads(s))


def brier(p, y) -> float:
    p, y = np.asarray(p, float), np.asarray(y, float)
    ok = np.isfinite(p) & np.isfinite(y)
    return float(np.mean((p[ok] - y[ok]) ** 2)) if ok.any() else float("nan")


def brier_skill(p, y, base_rate: float) -> float:
    """1 − Brier / Brier(constant base rate). Positive means better than the base rate."""
    ref = brier(np.full(len(np.asarray(y)), base_rate), y)
    b = brier(p, y)
    return float(1 - b / ref) if ref > 0 else float("nan")


def reliability(p, y, bins: int = 10) -> pd.DataFrame:
    """Mean predicted vs observed rate per probability bin (the dashboard's reliability curve)."""
    p, y = np.asarray(p, float), np.asarray(y, float)
    ok = np.isfinite(p) & np.isfinite(y)
    p, y = p[ok], y[ok]
    b = np.clip((p * bins).astype(int), 0, bins - 1)
    df = pd.DataFrame({"bin": b, "p": p, "y": y}).groupby("bin").agg(n=("y", "size"), predicted=("p", "mean"), observed=("y", "mean"))
    df.index = [f"{i / bins:.1f}-{(i + 1) / bins:.1f}" for i in df.index]
    return df
