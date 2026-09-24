"""Selection arms for the keep/kill experiment (PRD §17).

An arm turns candidates into calibrated probabilities that the target is hit
first; the EV gate then decides which candidates to take. Rules-only takes
every candidate and needs no arm object.

- ``GBMArm``: the gradient-boosted baseline. It trains on the earlier part of
  the training window and fits the isotonic calibrator on the later part, so
  calibration is always out of sample for the classifier.
- ``JevArm``: asks Jev through ``JevAdapter`` and calibrates its answers the
  same way. A skip from the adapter is a skip here.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from atlas_engine.adapters.jev import JevAdapter, JevResult
from atlas_engine.calibration import Calibrator
from atlas_engine.decisions.state import CATEGORICAL, NUMERIC, payload
from atlas_engine.setups import SETUPS

EXTRA_NUMERIC = ("recent_signal_r20",)
SETUP_NAMES = tuple(sorted(SETUPS))


def feature_matrix(c: pd.DataFrame) -> pd.DataFrame:
    """Numeric state plus fixed one-hot columns, so every fold has the same columns."""
    x = c[list(NUMERIC) + list(EXTRA_NUMERIC)].astype(float).copy()
    for col, labels in {**CATEGORICAL, "setup": SETUP_NAMES}.items():
        for lab in labels:
            x[f"{col}={lab}"] = (c[col] == lab).astype(float)
    return x


class Arm:
    name = "arm"

    def fit(self, train: pd.DataFrame) -> None:
        raise NotImplementedError

    def predict(self, test: pd.DataFrame) -> np.ndarray:
        raise NotImplementedError

    def describe(self) -> dict:
        return {"name": self.name}


def _split(train: pd.DataFrame, cal_frac: float) -> tuple[pd.DataFrame, pd.DataFrame]:
    t = train.sort_values("decision_time", kind="mergesort")
    k = int(round(len(t) * (1 - cal_frac)))
    return t.iloc[:k], t.iloc[k:]


@dataclass
class GBMArm(Arm):
    """Histogram gradient-boosted trees (scikit-learn), deterministic, calibrated by isotonic regression."""

    params: dict = field(default_factory=lambda: {
        "max_depth": 3, "learning_rate": 0.05, "max_iter": 200, "min_samples_leaf": 40, "l2_regularization": 1.0})
    calibration_frac: float = 0.25
    seed: int = 0
    name: str = "rules+gbm"

    def fit(self, train: pd.DataFrame) -> None:
        from sklearn.ensemble import HistGradientBoostingClassifier

        fit_part, cal_part = _split(train, self.calibration_frac)
        self.model = HistGradientBoostingClassifier(**self.params, early_stopping=False, random_state=self.seed)
        y = fit_part["y"].to_numpy()
        if len(np.unique(y)) < 2:
            self.model = None
            self.constant = float(y.mean()) if len(y) else float("nan")
        else:
            self.model.fit(feature_matrix(fit_part), y)
        self.calibrator = Calibrator.fit(self._raw(cal_part), cal_part["y"].to_numpy(), source=self.name)

    def _raw(self, c: pd.DataFrame) -> np.ndarray:
        if len(c) == 0:
            return np.array([])
        if self.model is None:
            return np.full(len(c), self.constant)
        return self.model.predict_proba(feature_matrix(c))[:, 1]

    def predict(self, test: pd.DataFrame) -> np.ndarray:
        return self.calibrator(self._raw(test))

    def describe(self) -> dict:
        return {"name": self.name, "model": "sklearn HistGradientBoostingClassifier", "params": self.params,
                "calibration": "isotonic", "calibration_frac": self.calibration_frac}


@dataclass
class JevArm(Arm):
    adapter: JevAdapter
    calibration_frac: float = 0.25
    name: str = "rules+jev"
    skips: dict = field(default_factory=dict)

    def _ask(self, c: pd.DataFrame) -> np.ndarray:
        out = np.full(len(c), np.nan)
        for i, (_, row) in enumerate(c.iterrows()):
            state = payload(row) | {"recent_signal_r20": row.get("recent_signal_r20")}
            res = self.adapter.evaluate_setup(row["candidate_id"], state, float(row["target_r"]), float(row["cost_r_est"]))
            if isinstance(res, JevResult):
                out[i] = res.p_target_first
            else:
                self.skips[res.reason] = self.skips.get(res.reason, 0) + 1
        return out

    def fit(self, train: pd.DataFrame) -> None:
        _, cal_part = _split(train, self.calibration_frac)
        raw = self._ask(cal_part)
        ok = np.isfinite(raw)
        self.calibrator = Calibrator.fit(raw[ok], cal_part["y"].to_numpy()[ok], source=self.adapter.model_version)

    def predict(self, test: pd.DataFrame) -> np.ndarray:
        return self.calibrator(self._ask(test))

    def describe(self) -> dict:
        return {"name": self.name, "model_version": self.adapter.model_version, "calibration": "isotonic", "skips": self.skips}
