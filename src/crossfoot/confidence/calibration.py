"""Split discipline, thresholds, and reliability.

Every published confidence number rests on one rule: fit on TRAIN, choose
thresholds on CALIBRATION, report on TEST. The rule is enforced here in code
rather than by convention, so leakage raises instead of quietly inflating a
scorecard. Both the requested split and the split tag on every row are checked.
"""

import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Protocol

import numpy as np

from crossfoot.confidence.scorer import LogisticModel, fit, fit_logistic, logit, probability
from crossfoot.constants import FieldFamily, SplitName
from crossfoot.models.extraction import FieldSignals
from crossfoot.models.scorecard import CalibrationBin, PlattCell, ThresholdPoint

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

# A Platt fit reads a logit spanning several units where a signal row spans
# [0, 1], so the shared fixed schedule needs more steps to reach the same
# optimum. Measured on this corpus: the fit stops moving well before here.
PLATT_ITERATIONS = 2000

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
    """Two-parameter logistic rescaling of an already-scored confidence.

    The linear term is the scorer's own logit rather than its confidence, which
    is what makes slope 1 and intercept 0 the identity: a family that needs no
    correction can be fit and left where it was. On a confidence the identity is
    not expressible, so fitting one would move a calibrated family off its mark.

    Increasing in confidence for any positive slope, so rescaling reorders
    nothing and cannot change which fields a threshold accepts. A fit returns a
    negative slope only for a family whose confidence points the wrong way,
    which is a broken scorer rather than a rescaling to publish.
    """

    slope: float
    intercept: float

    def apply(self, confidence: float) -> float:
        return probability(self.slope * logit(confidence) + self.intercept)


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
    """Rescaling for one family's confidences; calibration split only.

    The calibration split and not TRAIN: a scorer's scores on the rows it was
    fit on are optimistic, so a correction fit there would learn nothing.
    """
    _require_split(samples, THRESHOLD_SPLIT, split, "fitting platt scaling")
    return _platt(samples)


def fit_platt_scalers(
    samples: Sequence[ConfidenceSample], *, split: SplitName
) -> Mapping[FieldFamily, PlattScaler]:
    """One scaler per family present in the calibration rows.

    Every family is fit, not only the ones over the ceiling. Which families to
    correct cannot be read off the test ECE without letting the held out split
    choose the model, and the identity lies inside this family of curves, so a
    family already under the ceiling is not thrown off its mark by being fit.
    """
    _require_split(samples, THRESHOLD_SPLIT, split, "fitting platt scaling")
    grouped: defaultdict[FieldFamily, list[ConfidenceSample]] = defaultdict(list)
    for sample in samples:
        grouped[sample.field_family].append(sample)
    return {family: _platt(rows) for family, rows in grouped.items()}


def rescale(
    samples: Sequence[ConfidenceSample], scalers: Mapping[FieldFamily, PlattScaler]
) -> tuple[ConfidenceSample, ...]:
    """Each confidence through its family's scaler; a family with none passes through.

    The split tag rides along untouched, so a rescaled row is still guarded by
    whatever it is handed to next. An empty mapping is the uncalibrated pipeline
    exactly as it was, which is what makes the correction optional.
    """
    return tuple(
        sample
        if (scaler := scalers.get(sample.field_family)) is None
        else replace(sample, confidence=scaler.apply(sample.confidence))
        for sample in samples
    )


def platt_cells(scalers: Mapping[FieldFamily, PlattScaler]) -> tuple[PlattCell, ...]:
    """The applied scalers as scorecard cells, in the order the sweep publishes families.

    A scorecard has to state the correction that produced its numbers, so the two
    parameters travel with the figures rather than in prose beside them. An empty
    mapping gives an empty collection: an uncalibrated run says so by carrying
    nothing, and there is no second field to disagree with.
    """
    return tuple(
        PlattCell(
            field_family=family,
            slope=scalers[family].slope,
            intercept=scalers[family].intercept,
        )
        for family in sorted(scalers, key=lambda family: _FAMILY_ORDER[family])
    )


def sweep_point(
    field_family: FieldFamily, samples: Sequence[ConfidenceSample], threshold: float
) -> ThresholdPoint:
    """What one family reaches at a threshold already chosen, on whatever split it is handed.

    Reporting, not choosing. `choose_thresholds` is split guarded because picking
    an operating point on TEST would leak; measuring a point picked elsewhere is
    exactly what TEST exists for, so this is not guarded and must never be used
    to select one.
    """
    return _sweep(field_family, samples, threshold)


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


def _platt(samples: Sequence[ConfidenceSample]) -> PlattScaler:
    """One sigmoid over one linear term.

    Two parameters and no more: the calibration split holds a few hundred rows
    per family, which isotonic regression would fit the noise of.
    """
    features = np.array([[1.0, logit(sample.confidence)] for sample in samples])
    labels = np.array([float(sample.correct) for sample in samples])
    weights = fit_logistic(features, labels, iterations=PLATT_ITERATIONS)
    return PlattScaler(slope=float(weights[1]), intercept=float(weights[0]))


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
