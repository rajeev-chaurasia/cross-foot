"""Hand-rolled logistic regression over field signals, fit once per field family."""

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
import numpy.typing as npt

from crossfoot.constants import FieldFamily, QualityTier
from crossfoot.models.extraction import FieldSignals

# Deterministic by construction: zero init, fixed schedule, no RNG anywhere.
LEARNING_RATE = 0.5
ITERATIONS = 500
L2_PENALTY = 1e-3
# Logits are clipped so a confident model cannot overflow the exponential.
LOGIT_CLIP = 40.0

Vector = npt.NDArray[np.float64]
Matrix = npt.NDArray[np.float64]

_TIERS: tuple[QualityTier, ...] = tuple(QualityTier)


def encode(field_signals: FieldSignals) -> tuple[float, ...]:
    """Feature row. Optional signals become an (indicator, value) pair, so a
    missing signal is learnable instead of indistinguishable from a present zero."""
    optional = (
        field_signals.self_consistency,
        field_signals.det_llm_agreement,
        field_signals.validator_pass,
        field_signals.grammar_match,
        field_signals.crossfoot_ok,
    )
    pairs = [part for value in optional for part in (float(value is not None), value or 0.0)]
    tiers = [float(field_signals.quality_tier is tier) for tier in _TIERS]
    return (
        *pairs,
        float(field_signals.crossfoot_residual_suspect),
        field_signals.char_ambiguity,
        *tiers,
    )


@dataclass(frozen=True, slots=True)
class LogisticModel:
    """Fitted weights for one field family; element zero is the intercept."""

    field_family: FieldFamily
    weights: Vector

    def predict(self, field_signals: FieldSignals) -> float:
        return probability(float(_design_row(encode(field_signals)) @ self.weights))


def fit(family: FieldFamily, samples: Sequence[tuple[FieldSignals, bool]]) -> LogisticModel:
    """Batch gradient descent on the L2-penalized logistic loss."""
    if not samples:
        raise ValueError(f"no training samples for {family}")
    features = np.array([_design_row(encode(signals)) for signals, _ in samples])
    labels = np.array([float(correct) for _, correct in samples])
    return LogisticModel(field_family=family, weights=fit_logistic(features, labels))


def fit_logistic(features: Matrix, labels: Vector) -> Vector:
    """Weights for a design matrix whose first column is the intercept."""
    weights = np.zeros(features.shape[1], dtype=np.float64)
    for _ in range(ITERATIONS):
        residual = _sigmoid(features @ weights) - labels
        gradient = features.T @ residual / len(labels) + L2_PENALTY * weights
        weights = weights - LEARNING_RATE * gradient
    return weights


def probability(logit: float) -> float:
    """Logistic link shared by the family scorers and the calibration rescaler."""
    return float(_sigmoid(np.asarray(logit, dtype=np.float64)))


def _design_row(features: tuple[float, ...]) -> Vector:
    return np.array((1.0, *features), dtype=np.float64)


def _sigmoid(logits: Vector) -> Vector:
    return 1.0 / (1.0 + np.exp(-np.clip(logits, -LOGIT_CLIP, LOGIT_CLIP)))
