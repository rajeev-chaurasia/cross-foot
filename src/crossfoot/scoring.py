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
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from crossfoot.confidence.calibration import (
    FIT_SPLIT,
    THRESHOLD_SPLIT,
    ConfidenceSample,
    TrainingSample,
    choose_thresholds,
    fit_scorers,
)
from crossfoot.confidence.scorer import LogisticModel
from crossfoot.confidence.signals import attach_signals
from crossfoot.constants import FieldFamily, ReviewStatus, SplitName
from crossfoot.evals.metrics import field_is_correct
from crossfoot.evals.runner import signal_context
from crossfoot.models.extraction import ExtractedDocument, ExtractedField, FieldSignals
from crossfoot.models.manifest import ManifestRecord
from crossfoot.models.scorecard import ThresholdPoint

# What an unscored field publishes. Never an auto accept: a missing score is a
# reason for a human to look, not a reason to skip one.
UNSCORED_CONFIDENCE = 0.0
UNSCORED_STATUS = ReviewStatus.NEEDS_REVIEW

FamilyModels = Mapping[FieldFamily, LogisticModel]
FamilyThresholds = Mapping[FieldFamily, float]


@dataclass(frozen=True, slots=True)
class ConfidencePass:
    """Every document rescored, and the operating point that rescored it."""

    documents: tuple[ExtractedDocument, ...]
    thresholds: tuple[ThresholdPoint, ...]

    def threshold_for(self, family: FieldFamily) -> float | None:
        """The confidence a field of this family had to reach, or None if it had none."""
        return next(
            (point.threshold for point in self.thresholds if point.field_family is family), None
        )


@dataclass(frozen=True, slots=True)
class _Labelled:
    """One extracted field paired with truth, carrying the split it was read from."""

    field_family: FieldFamily
    signals: FieldSignals
    correct: bool
    split: SplitName


def apply_confidence(
    documents: Sequence[ExtractedDocument],
    records: Mapping[str, ManifestRecord],
    *,
    fit_split: SplitName = FIT_SPLIT,
    threshold_split: SplitName = THRESHOLD_SPLIT,
) -> ConfidencePass:
    """Score every field of every document and set its status from its family's threshold.

    The two split arguments are here to be guarded, not to be chosen. Calibration
    sanctions TRAIN for fitting and CALIBRATION for thresholds and nothing else,
    so any other value raises SplitDisciplineError rather than fitting on it.
    """
    prepared = {doc.doc_id: _prepared(doc, records.get(doc.doc_id)) for doc in documents}
    models = _fit(prepared, records, fit_split)
    thresholds = _thresholds(prepared, records, models, threshold_split)
    by_family = {point.field_family: point.threshold for point in thresholds}

    scored: list[ExtractedDocument] = []
    for doc in documents:
        ready = prepared[doc.doc_id]
        if ready is None:
            # No context means no signals worth scoring, so the whole document
            # queues rather than passing through a model fed stale features.
            scored.append(_scored(doc, {}, {}))
        else:
            scored.append(_scored(ready, models, by_family))
    return ConfidencePass(documents=tuple(scored), thresholds=thresholds)


def _prepared(doc: ExtractedDocument, record: ManifestRecord | None) -> ExtractedDocument | None:
    """The document with its signals recomputed, or None when their context is missing.

    Recomputed rather than trusted: the scorers were fit on signals assembled this
    way, so scoring a field on anything else would feed the model a different
    feature row than it learned from.
    """
    if record is None:
        return None
    context = signal_context(record)
    if context is None:
        return None
    return attach_signals(doc, context)


def _fit(
    prepared: Mapping[str, ExtractedDocument | None],
    records: Mapping[str, ManifestRecord],
    split: SplitName,
) -> FamilyModels:
    rows = _labelled(prepared, records, split)
    samples = [
        TrainingSample(
            field_family=row.field_family,
            signals=row.signals,
            correct=row.correct,
            split=row.split,
        )
        for row in rows
    ]
    return fit_scorers(samples, split=split)


def _thresholds(
    prepared: Mapping[str, ExtractedDocument | None],
    records: Mapping[str, ManifestRecord],
    models: FamilyModels,
    split: SplitName,
) -> tuple[ThresholdPoint, ...]:
    samples = [
        ConfidenceSample(
            field_family=row.field_family,
            confidence=models[row.field_family].predict(row.signals),
            correct=row.correct,
            split=row.split,
        )
        for row in _labelled(prepared, records, split)
        if row.field_family in models
    ]
    return choose_thresholds(samples, split=split)


def _labelled(
    prepared: Mapping[str, ExtractedDocument | None],
    records: Mapping[str, ManifestRecord],
    split: SplitName,
) -> list[_Labelled]:
    """Fields of one split paired with truth, tagged so the split guard can check them."""
    rows: list[_Labelled] = []
    for doc_id, doc in prepared.items():
        record = records.get(doc_id)
        if doc is None or record is None or record.split is not split or record.truth is None:
            continue
        for field in (*doc.header_fields, *doc.line_fields):
            correct = field_is_correct(field, record.truth)
            if correct is None:
                continue  # truth holds no value there, so there is nothing to learn
            rows.append(_Labelled(field.family, field.signals, correct, record.split))
    return rows


def _scored(
    doc: ExtractedDocument, models: FamilyModels, thresholds: FamilyThresholds
) -> ExtractedDocument:
    return doc.model_copy(
        update={
            "header_fields": tuple(
                _scored_field(field, models, thresholds) for field in doc.header_fields
            ),
            "line_fields": tuple(
                _scored_field(field, models, thresholds) for field in doc.line_fields
            ),
        }
    )


def _scored_field(
    field: ExtractedField, models: FamilyModels, thresholds: FamilyThresholds
) -> ExtractedField:
    model = models.get(field.family)
    threshold = thresholds.get(field.family)
    if model is None or threshold is None:
        return field.model_copy(
            update={"confidence": UNSCORED_CONFIDENCE, "status": UNSCORED_STATUS}
        )
    confidence = model.predict(field.signals)
    status = ReviewStatus.AUTO_ACCEPTED if confidence >= threshold else ReviewStatus.NEEDS_REVIEW
    return field.model_copy(update={"confidence": confidence, "status": status})
