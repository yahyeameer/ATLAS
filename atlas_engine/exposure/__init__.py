"""Currency netting and correlation clusters (PRD §20)."""

from atlas_engine.exposure.netting import (
    CorrelationMatrix, Exposure, correlated_with, correlation_matrix, currency_risk, legs,
)

__all__ = ["CorrelationMatrix", "Exposure", "correlated_with", "correlation_matrix", "currency_risk", "legs"]
