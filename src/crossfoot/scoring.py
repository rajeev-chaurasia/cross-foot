"""Fit the confidence scorers, score every extracted field, apply the thresholds.

Phase 2 built the scorers and the threshold sweep. This is the step that carries
them to a reviewer. Without it the review surface publishes the extractor's own
binary confidence, which sends every field no deterministic validator vouched for
to a human and disproves the claim the surface exists to demonstrate.

Split discipline is not restated here, it is delegated.
`crossfoot.confidence.calibration` guards both the requested split and the tag on
every row, so the fit call below is handed TRAIN rows and the threshold call
CALIBRATION rows, and anything else raises SplitDisciplineError before a model
exists. TEST is scored with the resulting operating point and never contributes
to choosing one.

A field this pass cannot score keeps confidence 0.0 and NEEDS_REVIEW. Silence is
never an auto accept; docs/contracts-phase3.md states that as a rule rather than
leaving it to a default.

FEATURES COME FROM THE ARTIFACT, LABELS ARE HANDED IN. This module deliberately
cannot see the dataset manifest: it computes every feature by calling
`attach_signals` on the extraction, and it receives the correct/incorrect
judgement as a sequence of `FieldLabel`, which the caller derives from truth.
An earlier version imported `ManifestRecord` and built the signals from it, which
put the generator's quality tier, the true statement period, the true marque and
the true line types into the feature vector of the review database the API and
the UI read. The split is structural now, and `tests/unit/test_truth_boundary.py`
fails the build if this module ever imports the manifest again.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from crossfoot.confidence.calibration import (
    FIT_SPLIT,
    THRESHOLD_SPLIT,
    ConfidenceSample,
    PlattScaler,
    TrainingSample,
    choose_thresholds,
    fit_platt_scalers,
    fit_scorers,
    platt_cells,
    rescale,
)
from crossfoot.confidence.scorer import LogisticModel
from crossfoot.confidence.signals import attach_signals
from crossfoot.constants import FieldFamily, ReviewStatus, SplitName
from crossfoot.models.extraction import ExtractedDocument, ExtractedField, FieldSignals
from crossfoot.models.scorecard import PlattCell, ThresholdPoint

# What an unscored field publishes. Never an auto accept: a missing score is a
# reason for a human to look, not a reason to skip one.
UNSCORED_CONFIDENCE = 0.0
UNSCORED_STATUS = ReviewStatus.NEEDS_REVIEW

FamilyModels = Mapping[FieldFamily, LogisticModel]
FamilyThresholds = Mapping[FieldFamily, float]
FamilyScalers = Mapping[FieldFamily, PlattScaler]


@dataclass(frozen=True, slots=True)
class ConfidencePass:
    """Every document rescored, and the operating point that rescored it."""

    documents: tuple[ExtractedDocument, ...]
    thresholds: tuple[ThresholdPoint, ...]
    # The rescaling behind the confidences above, in the same shape a scorecard
    # publishes. Empty is an uncalibrated pass, so the two cannot disagree.
    platt_scaling: tuple[PlattCell, ...] = ()

    def threshold_for(self, family: FieldFamily) -> float | None:
        """The confidence a field of this family had to reach, or None if it had none."""
        return next(
            (point.threshold for point in self.thresholds if point.field_family is family), None
        )


@dataclass(frozen=True, slots=True)
class FieldLabel:
    """One field judged right or wrong, and the split its document belongs to.

    THE ONLY DOOR TRUTH COMES THROUGH. A label says whether a reading matched the
    answer key; it carries no description of the document, so nothing about it can
    become a feature. The caller builds these (`crossfoot.ingest_db` from the
    dataset manifest, an eval harness from wherever it holds truth) and this
    module never asks where they came from.
    """

    field_id: str
    correct: bool
    split: SplitName


@dataclass(frozen=True, slots=True)
class _Labelled:
    """One scored feature row joined to its label, tagged with its split."""

    field_family: FieldFamily
    signals: FieldSignals
    correct: bool
    split: SplitName


def apply_confidence(
    documents: Sequence[ExtractedDocument],
    labels: Sequence[FieldLabel],
    *,
    fit_split: SplitName = FIT_SPLIT,
    threshold_split: SplitName = THRESHOLD_SPLIT,
    calibrate: bool = False,
) -> ConfidencePass:
    """Score every field of every document and set its status from its family's threshold.

    The two split arguments are here to be guarded, not to be chosen. Calibration
    sanctions TRAIN for fitting and CALIBRATION for thresholds and nothing else,
    so any other value raises SplitDisciplineError rather than fitting on it.

    Signals are recomputed rather than trusted: the scorers are fit on signals
    assembled by `attach_signals`, so scoring a field on anything else would feed
    the model a different feature row than it learned from.

    `calibrate` puts every score through a Platt scaler fit on the threshold
    split, the same correction `crossfoot eval --calibrate` reports, so a
    reviewer reads the probability the published ECE describes instead of the raw
    score. Off by default, because the review database is a deployed surface and
    a caller has to ask for the change.
    """
    prepared = [attach_signals(doc) for doc in documents]
    by_field = {
        field.field_id: field
        for doc in prepared
        for field in (*doc.header_fields, *doc.line_fields)
    }
    rows = _labelled(by_field, labels)
    models = _fit(rows, fit_split)
    samples = _samples(rows, models, threshold_split)
    scalers: FamilyScalers = fit_platt_scalers(samples, split=threshold_split) if calibrate else {}
    # Rescaling happens before the threshold is chosen, never after: a threshold
    # picked on raw scores names a different operating point once the scores
    # underneath it move.
    thresholds = choose_thresholds(rescale(samples, scalers), split=threshold_split)
    by_family = {point.field_family: point.threshold for point in thresholds}
    return ConfidencePass(
        documents=tuple(_scored(doc, models, by_family, scalers) for doc in prepared),
        thresholds=thresholds,
        platt_scaling=platt_cells(scalers),
    )


def _fit(rows: Sequence[_Labelled], split: SplitName) -> FamilyModels:
    samples = [
        TrainingSample(
            field_family=row.field_family,
            signals=row.signals,
            correct=row.correct,
            split=row.split,
        )
        for row in rows
        if row.split is split
    ]
    return fit_scorers(samples, split=split)


def _samples(
    rows: Sequence[_Labelled], models: FamilyModels, split: SplitName
) -> list[ConfidenceSample]:
    """One split's rows scored by their family model, tagged so the guards can check them."""
    return [
        ConfidenceSample(
            field_family=row.field_family,
            confidence=models[row.field_family].predict(row.signals),
            correct=row.correct,
            split=row.split,
        )
        for row in rows
        if row.split is split and row.field_family in models
    ]


def _labelled(
    by_field: Mapping[str, ExtractedField], labels: Sequence[FieldLabel]
) -> list[_Labelled]:
    """Join each label to the scored field it names, dropping labels with no field."""
    rows: list[_Labelled] = []
    for label in labels:
        field = by_field.get(label.field_id)
        if field is None:
            continue
        rows.append(_Labelled(field.family, field.signals, label.correct, label.split))
    return rows


def _scored(
    doc: ExtractedDocument,
    models: FamilyModels,
    thresholds: FamilyThresholds,
    scalers: FamilyScalers,
) -> ExtractedDocument:
    return doc.model_copy(
        update={
            "header_fields": tuple(
                _scored_field(field, models, thresholds, scalers) for field in doc.header_fields
            ),
            "line_fields": tuple(
                _scored_field(field, models, thresholds, scalers) for field in doc.line_fields
            ),
        }
    )


def _scored_field(
    field: ExtractedField,
    models: FamilyModels,
    thresholds: FamilyThresholds,
    scalers: FamilyScalers,
) -> ExtractedField:
    model = models.get(field.family)
    threshold = thresholds.get(field.family)
    if model is None or threshold is None:
        return field.model_copy(
            update={"confidence": UNSCORED_CONFIDENCE, "status": UNSCORED_STATUS}
        )
    scaler = scalers.get(field.family)
    confidence = model.predict(field.signals)
    # A Platt scaler is increasing, and the threshold below was chosen from
    # scores it had already been applied to, so the correction moves the number
    # displayed beside a field and not which fields the review queue holds: the
    # threshold moves with the scores. A reader seeing the confidence change
    # while the queue stands still is looking at that, not at a bug.
    if scaler is not None:
        confidence = scaler.apply(confidence)
    status = ReviewStatus.AUTO_ACCEPTED if confidence >= threshold else ReviewStatus.NEEDS_REVIEW
    return field.model_copy(update={"confidence": confidence, "status": status})
