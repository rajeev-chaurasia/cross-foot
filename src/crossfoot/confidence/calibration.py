"""Split discipline, thresholds, and reliability.

Every published confidence number rests on one rule: fit on TRAIN, choose
thresholds on CALIBRATION, report on TEST. The rule is enforced here in code
rather than by convention, so leakage raises instead of quietly inflating a
scorecard. Both the requested split and the split tag on every row are checked.
"""

import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

import numpy as np

from crossfoot.confidence.scorer import LogisticModel, fit, fit_logistic, probability
from crossfoot.constants import FieldFamily, SplitName
from crossfoot.models.extraction import FieldSignals
from crossfoot.models.scorecard import CalibrationBin, ThresholdPoint

# Lowest auto-accept precision each family must hold before a threshold is usable.
PRECISION_TARGETS: dict[FieldFamily, float] = {
    FieldFamily.AMOUNT: 0.995,
    FieldFamily.REFERENCE: 0.995,
    FieldFamily.DATE: 0.99,
    FieldFamily.TEXT: 0.97,
}

# Reliability diagram resolution: equal-count bins, not equal-width.
BIN_COUNT = 10

# Test ECE above this calls for Platt scaling fit on the calibration split.
MAX_EXPECTED_CALIBRATION_ERROR = 0.05

FIT_SPLIT = SplitName.TRAIN
THRESHOLD_SPLIT = SplitName.CALIBRATION

_FAMILY_ORDER = {family: index for index, family in enumerate(FieldFamily)}


class SplitDisciplineError(RuntimeError):
    """Raised when a split is used for anything but its one sanctioned purpose."""


class _SplitTagged(Protocol):
    @property
    def split(self) -> SplitName: ...


@dataclass(frozen=True, slots=True)
class TrainingSample:
    field_family: FieldFamily
    signals: FieldSignals
    correct: bool
    split: SplitName


@dataclass(frozen=True, slots=True)
class ConfidenceSample:
    field_family: FieldFamily
    confidence: float
    correct: bool
    split: SplitName


@dataclass(frozen=True, slots=True)
class PlattScaler:
    """Single-feature logistic rescaling of an already-scored confidence."""

    slope: float
    intercept: float

    def apply(self, confidence: float) -> float:
        return probability(self.slope * confidence + self.intercept)


def fit_scorers(
    samples: Sequence[TrainingSample], *, split: SplitName
) -> Mapping[FieldFamily, LogisticModel]:
    """One fitted model per family present in the training rows."""
    _require_split(samples, FIT_SPLIT, split, "fitting scorers")
    grouped: defaultdict[FieldFamily, list[tuple[FieldSignals, bool]]] = defaultdict(list)
    for sample in samples:
        grouped[sample.field_family].append((sample.signals, sample.correct))
    return {family: fit(family, rows) for family, rows in grouped.items()}


def choose_thresholds(
    samples: Sequence[ConfidenceSample], *, split: SplitName
) -> tuple[ThresholdPoint, ...]:
    """Per family, the lowest review rate whose auto-accept precision meets target."""
    _require_split(samples, THRESHOLD_SPLIT, split, "choosing thresholds")
    by_family: defaultdict[FieldFamily, list[ConfidenceSample]] = defaultdict(list)
    for sample in samples:
        by_family[sample.field_family].append(sample)
    families = sorted(by_family, key=lambda family: _FAMILY_ORDER[family])
    return tuple(_best_threshold(family, by_family[family]) for family in families)


def fit_platt_scaling(samples: Sequence[ConfidenceSample], *, split: SplitName) -> PlattScaler:
    """Rescaling for a family whose test ECE exceeds the ceiling; calibration split only."""
    _require_split(samples, THRESHOLD_SPLIT, split, "fitting platt scaling")
    features = np.array([[1.0, sample.confidence] for sample in samples])
    labels = np.array([float(sample.correct) for sample in samples])
    weights = fit_logistic(features, labels)
    return PlattScaler(slope=float(weights[1]), intercept=float(weights[0]))


def reliability_bins(
    samples: Sequence[ConfidenceSample], field_family: FieldFamily
) -> tuple[CalibrationBin, ...]:
    """Equal-count bins over one family's confidences, least confident first."""
    ordered = sorted(
        (sample for sample in samples if sample.field_family is field_family),
        key=lambda sample: sample.confidence,
    )
    total = len(ordered)
    bins = []
    for index in range(BIN_COUNT):
        chunk = ordered[index * total // BIN_COUNT : (index + 1) * total // BIN_COUNT]
        if not chunk:
            continue  # fewer samples than bins: report the bins that exist
        bins.append(
            CalibrationBin(
                field_family=field_family,
                mean_confidence=sum(sample.confidence for sample in chunk) / len(chunk),
                empirical_accuracy=sum(1 for sample in chunk if sample.correct) / len(chunk),
                count=len(chunk),
            )
        )
    return tuple(bins)


def expected_calibration_error(bins: Sequence[CalibrationBin]) -> float:
    """Count-weighted mean absolute gap between stated confidence and accuracy."""
    total = sum(one_bin.count for one_bin in bins)
    if not total:
        return 0.0
    gap = sum(
        one_bin.count * abs(one_bin.mean_confidence - one_bin.empirical_accuracy)
        for one_bin in bins
    )
    return gap / total


def _require_split(
    samples: Sequence[_SplitTagged], allowed: SplitName, requested: SplitName, purpose: str
) -> None:
    """Guard both the caller's intent and the rows themselves, so neither alone can leak."""
    if requested is not allowed:
        raise SplitDisciplineError(f"{purpose} uses the {allowed} split, not {requested}")
    foreign = sorted({sample.split for sample in samples if sample.split is not allowed})
    if foreign:
        raise SplitDisciplineError(f"{purpose} on {allowed} was handed {', '.join(foreign)} rows")


def _best_threshold(family: FieldFamily, samples: Sequence[ConfidenceSample]) -> ThresholdPoint:
    target = PRECISION_TARGETS[family]
    highest = max(sample.confidence for sample in samples)
    # The sentinel accepts nothing, so a family that cannot meet its target
    # reviews everything rather than publishing a threshold it does not earn.
    candidates = {sample.confidence for sample in samples} | {math.nextafter(highest, math.inf)}
    points = [_sweep(family, samples, threshold) for threshold in sorted(candidates)]
    qualifying = [point for point in points if point.auto_accept_precision >= target]
    return min(qualifying, key=lambda point: point.review_rate)


def _sweep(
    family: FieldFamily, samples: Sequence[ConfidenceSample], threshold: float
) -> ThresholdPoint:
    accepted = [sample for sample in samples if sample.confidence >= threshold]
    correct = sum(1 for sample in accepted if sample.correct)
    return ThresholdPoint(
        field_family=family,
        threshold=threshold,
        # Accepting nothing is vacuously precise, which is what makes the
        # sentinel a safe fallback rather than a way to fake the target.
        auto_accept_precision=correct / len(accepted) if accepted else 1.0,
        review_rate=1.0 - len(accepted) / len(samples),
    )
