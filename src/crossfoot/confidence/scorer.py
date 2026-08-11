"""Hand-rolled logistic regression over field signals, fit once per field family."""

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
import numpy.typing as npt

from crossfoot.constants import ExtractionRoute, FieldFamily
from crossfoot.models.extraction import FieldSignals

# Deterministic by construction: zero init, fixed schedule, no RNG anywhere.
LEARNING_RATE = 0.5
ITERATIONS = 500
L2_PENALTY = 1e-3
# Logits are clipped so a confident model cannot overflow the exponential.
LOGIT_CLIP = 40.0

Vector = npt.NDArray[np.float64]
Matrix = npt.NDArray[np.float64]

# The categorical the model learns base rates per. It is the route the router
# chose from the file's bytes, never the tier the generator degraded a page to:
# a scan's severity is not something any document announces, so the model does
# not get to see it. An unrouted row one-hots to all zeros, which is the same
# "no information" the absent-signal pairs above express.
_ROUTES: tuple[ExtractionRoute, ...] = tuple(ExtractionRoute)


def encode(field_signals: FieldSignals) -> tuple[float, ...]:
    """Feature row. Optional signals become an (indicator, value) pair, so a
    missing signal is learnable instead of indistinguishable from a present zero.

    This is the feature side of the fit and it reads `FieldSignals` and nothing
    else. Truth reaches a fit only as the label argument of `fit`, never here.
    """
    optional = (
        field_signals.self_consistency,
        field_signals.det_llm_agreement,
        field_signals.validator_pass,
        field_signals.grammar_match,
        field_signals.crossfoot_ok,
    )
    pairs = [part for value in optional for part in (float(value is not None), value or 0.0)]
    routes = [float(field_signals.route is route) for route in _ROUTES]
    return (
        *pairs,
        float(field_signals.crossfoot_residual_suspect),
        field_signals.char_ambiguity,
        *routes,
    )


@dataclass(frozen=True, slots=True)
class LogisticModel:
    """Fitted weights for one field family; element zero is the intercept."""

    field_family: FieldFamily
    weights: Vector

    def predict(self, field_signals: FieldSignals) -> float:
        return probability(float(_design_row(encode(field_signals)) @ self.weights))


def fit(family: FieldFamily, samples: Sequence[tuple[FieldSignals, bool]]) -> LogisticModel:
    """Batch gradient descent on the L2-penalized logistic loss.

    The pair in each sample is the whole features-versus-labels split: the
    `FieldSignals` is the feature row and comes from the extraction, the bool is
    the label and is the only thing truth is allowed to decide.
    """
    if not samples:
        raise ValueError(f"no training samples for {family}")
    features = np.array([_design_row(encode(signals)) for signals, _ in samples])
    labels = np.array([float(correct) for _, correct in samples])
    return LogisticModel(field_family=family, weights=fit_logistic(features, labels))


def fit_logistic(features: Matrix, labels: Vector, *, iterations: int = ITERATIONS) -> Vector:
    """Weights for a design matrix whose first column is the intercept.

    The schedule is fixed, so the step count is what a caller whose features span
    a wider range than a signal row has to raise to reach the same optimum.
    """
    weights = np.zeros(features.shape[1], dtype=np.float64)
    for _ in range(iterations):
        residual = _sigmoid(features @ weights) - labels
        gradient = features.T @ residual / len(labels) + L2_PENALTY * weights
        weights = weights - LEARNING_RATE * gradient
    return weights


def probability(logit_value: float) -> float:
    """Logistic link shared by the family scorers and the calibration rescaler."""
    return float(_sigmoid(np.asarray(logit_value, dtype=np.float64)))


def logit(confidence: float) -> float:
    """Inverse of `probability`, saturating at the same bound the link does.

    A confidence of exactly 0 or 1 is reachable in float64 once the link clips,
    so the inverse has to name a finite logit for both rather than diverge.
    """
    if confidence <= 0.0:
        return -LOGIT_CLIP
    if confidence >= 1.0:
        return LOGIT_CLIP
    return float(np.clip(np.log(confidence / (1.0 - confidence)), -LOGIT_CLIP, LOGIT_CLIP))


def _design_row(features: tuple[float, ...]) -> Vector:
    return np.array((1.0, *features), dtype=np.float64)


def _sigmoid(logits: Vector) -> Vector:
    return 1.0 / (1.0 + np.exp(-np.clip(logits, -LOGIT_CLIP, LOGIT_CLIP)))
