"""The pass that turns signals into a review decision.

The hand built set is two populations of amount fields: clean documents that
foot, print unambiguous glyphs, and were read correctly, and scans that do not
foot and carry one badly misread line each. That is enough for the family scorer
to separate them, which is what makes the confidences graded rather than the
binary flag extraction hands over.

The fixtures carry truth twice over on purpose. A `ManifestRecord` is what an
eval holds, and `field_is_correct` turns it into the `FieldLabel` rows
`apply_confidence` accepts. Nothing else about the record crosses: the features
come off the extraction, which is the boundary the whole confidence claim rests
on and the one an adversarial audit found broken.
"""

from datetime import date

import pytest

from crossfoot.confidence.calibration import SplitDisciplineError
from crossfoot.constants import (
    DocType,
    ExtractionRoute,
    FieldFamily,
    FieldName,
    FieldSource,
    LineType,
    Oem,
    QualityTier,
    ReviewStatus,
    SplitName,
)
from crossfoot.evals.metrics import field_is_correct
from crossfoot.models.extraction import ExtractedDocument, ExtractedField, FieldSignals
from crossfoot.models.manifest import ManifestRecord
from crossfoot.models.statement import StatementDoc, StatementLine
from crossfoot.scoring import (
    UNSCORED_CONFIDENCE,
    ConfidencePass,
    FieldLabel,
    apply_confidence,
)

PERIOD_START = date(2026, 7, 1)
PERIOD_END = date(2026, 7, 31)
STATEMENT_DATE = date(2026, 7, 31)

# Amounts printed with glyphs no marque font confuses, so a clean reading scores
# zero character ambiguity and the signal means what it says.
CLEAN_CENTS: tuple[int, ...] = (43_697, 73_446, 94_397, 66_934)
# The misread line of a heavy scan: read high, and printed in the confusable
# classes the ambiguity signal counts.
MISREAD_CENTS = 108_805
DIRTY_CENTS: tuple[int, ...] = (34_697, 47_346, 96_437, 100_580)

CLEAN_DOCS_PER_SPLIT = 8
DIRTY_DOCS_PER_SPLIT = 3


def _truth(doc_id: str, amounts: tuple[int, ...]) -> StatementDoc:
    lines = tuple(
        StatementLine(
            line_no=line_no,
            line_type=LineType.CHARGE,
            line_date=date(2026, 7, 14),
            description="brake kit",
            amount_cents=cents,
        )
        for line_no, cents in enumerate(amounts, start=1)
    )
    total = sum(amounts)
    return StatementDoc(
        doc_id=doc_id,
        dealer_id="dlr-northstar",
        doc_type=DocType.PARTS_STATEMENT,
        oem=Oem.NORTHSTAR,
        statement_number="STMT-202607-01",
        statement_date=STATEMENT_DATE,
        period_start=PERIOD_START,
        period_end=PERIOD_END,
        subtotal_cents=total,
        total_cents=total,
        lines=lines,
    )


def _amount_field(
    doc_id: str, name: FieldName, cents: int, *, line_no: int | None
) -> ExtractedField:
    suffix = "header" if line_no is None else f"{line_no:02d}"
    text = f"{cents / 100:.2f}"
    return ExtractedField(
        field_id=f"{doc_id}-{suffix}-{name.value}",
        doc_id=doc_id,
        line_no=line_no,
        name=name,
        family=FieldFamily.AMOUNT,
        raw_text=text,
        value=text,
        value_cents=cents,
        source=FieldSource.LLM_VISION,
        signals=FieldSignals(),
    )


def _document(
    doc_id: str, read: tuple[int, ...], printed_total: int, route: ExtractionRoute
) -> ExtractedDocument:
    """One statement as it was read: the printed total plus every line amount."""
    return ExtractedDocument(
        doc_id=doc_id,
        file_path=f"files/{doc_id}.pdf",
        route=route,
        doc_type=DocType.PARTS_STATEMENT,
        header_fields=(_amount_field(doc_id, FieldName.TOTAL, printed_total, line_no=None),),
        line_fields=tuple(
            _amount_field(doc_id, FieldName.LINE_AMOUNT, cents, line_no=line_no)
            for line_no, cents in enumerate(read, start=1)
        ),
    )


def _record(
    doc_id: str, amounts: tuple[int, ...], tier: QualityTier, split: SplitName | None
) -> ManifestRecord:
    return ManifestRecord(
        doc_id=doc_id,
        file_path=f"files/{doc_id}.pdf",
        quality_tier=tier,
        template_id="northstar-parts_statement-scan-v1",
        render_seed=1,
        truth=_truth(doc_id, amounts),
        split=split,
    )


def _clean(doc_id: str, split: SplitName) -> tuple[ExtractedDocument, ManifestRecord]:
    """Read exactly right, so the document foots and every field is correct."""
    total = sum(CLEAN_CENTS)
    document = _document(doc_id, CLEAN_CENTS, total, ExtractionRoute.DIGITAL_PDF)
    return document, _record(doc_id, CLEAN_CENTS, QualityTier.CLEAN_DIGITAL, split)


def _dirty(doc_id: str, split: SplitName) -> tuple[ExtractedDocument, ManifestRecord]:
    """One line read high on a heavy scan, so the document no longer foots."""
    read = (*DIRTY_CENTS[:-1], MISREAD_CENTS)
    document = _document(doc_id, read, sum(DIRTY_CENTS), ExtractionRoute.SCANNED_PDF)
    return document, _record(doc_id, DIRTY_CENTS, QualityTier.SCAN_HEAVY, split)


def _population(
    split: SplitName,
) -> tuple[list[ExtractedDocument], list[ManifestRecord]]:
    documents: list[ExtractedDocument] = []
    records: list[ManifestRecord] = []
    for index in range(CLEAN_DOCS_PER_SPLIT):
        document, record = _clean(f"doc-{split.value}-clean-{index:02d}", split)
        documents.append(document)
        records.append(record)
    for index in range(DIRTY_DOCS_PER_SPLIT):
        document, record = _dirty(f"doc-{split.value}-dirty-{index:02d}", split)
        documents.append(document)
        records.append(record)
    return documents, records


def _corpus(
    splits: tuple[SplitName, ...] = (SplitName.TRAIN, SplitName.CALIBRATION, SplitName.TEST),
) -> tuple[list[ExtractedDocument], dict[str, ManifestRecord]]:
    documents: list[ExtractedDocument] = []
    records: dict[str, ManifestRecord] = {}
    for split in splits:
        split_documents, split_records = _population(split)
        documents.extend(split_documents)
        records.update({record.doc_id: record for record in split_records})
    return documents, records


def _labels(
    documents: list[ExtractedDocument], records: dict[str, ManifestRecord]
) -> list[FieldLabel]:
    """The eval side of the boundary: truth in, right-or-wrong bits out.

    This is what `crossfoot.ingest_db._labels` does against a real dataset, and
    it is deliberately the only thing the tests here hand to `apply_confidence`.
    """
    labels: list[FieldLabel] = []
    for document in documents:
        record = records.get(document.doc_id)
        if record is None or record.truth is None or record.split is None:
            continue
        for field in (*document.header_fields, *document.line_fields):
            correct = field_is_correct(field, record.truth)
            if correct is not None:
                labels.append(FieldLabel(field.field_id, correct, record.split))
    return labels


def _fields(result: ConfidencePass, split: SplitName) -> list[ExtractedField]:
    return [
        field
        for document in result.documents
        if split.value in document.doc_id
        for field in (*document.header_fields, *document.line_fields)
    ]


@pytest.fixture(scope="module")
def scored() -> ConfidencePass:
    documents, records = _corpus()
    return apply_confidence(documents, _labels(documents, records))


def test_fields_get_graded_confidences_rather_than_a_binary_flag(scored: ConfidencePass) -> None:
    confidences = {round(field.confidence, 6) for field in _fields(scored, SplitName.TEST)}
    assert confidences - {0.0, 1.0}, "every confidence is still the extractor's binary flag"
    assert len(confidences) >= 3
    # A graded score lives strictly inside the interval, not at either end of it.
    assert any(0.01 < value < 0.99 for value in confidences)


def test_the_auto_accepted_share_matches_the_calibrated_review_rate(
    scored: ConfidencePass,
) -> None:
    point = next(p for p in scored.thresholds if p.field_family is FieldFamily.AMOUNT)
    # A real operating point, not the sentinel that reviews everything nor a
    # threshold so low it accepts the whole split.
    assert 0.0 < point.review_rate < 1.0
    fields = _fields(scored, SplitName.CALIBRATION)
    accepted = [f for f in fields if f.status is ReviewStatus.AUTO_ACCEPTED]
    assert len(accepted) / len(fields) == pytest.approx(1.0 - point.review_rate, abs=0.02)


def test_every_auto_accepted_field_cleared_its_family_threshold(scored: ConfidencePass) -> None:
    threshold = scored.threshold_for(FieldFamily.AMOUNT)
    assert threshold is not None
    for field in _fields(scored, SplitName.TEST):
        cleared = field.confidence >= threshold
        assert (field.status is ReviewStatus.AUTO_ACCEPTED) is cleared


def test_identical_extractions_score_identically_whatever_truth_says_about_them() -> None:
    """The audit's finding, pinned as behaviour rather than as an import rule.

    Two documents whose extracted values are the same in every respect, one read
    correctly and one whose truth says every line is wrong. They must receive the
    same confidence, because confidence is a claim about the evidence on the page
    and the page is identical. A feature drawn from the manifest, such as the
    degradation tier the generator applied, would pull them apart.
    """
    documents, records = _corpus()
    twin_id = "doc-test-twin-00"
    twin = _document(twin_id, CLEAN_CENTS, sum(CLEAN_CENTS), ExtractionRoute.DIGITAL_PDF)
    # Same reading, and a truth that disagrees with all of it.
    records[twin_id] = _record(
        twin_id, tuple(c + 1 for c in CLEAN_CENTS), QualityTier.SCAN_HEAVY, SplitName.TEST
    )
    documents.append(twin)

    result = apply_confidence(documents, _labels(documents, records))
    scored_twin = next(doc for doc in result.documents if doc.doc_id == twin_id)
    scored_clean = next(doc for doc in result.documents if doc.doc_id == "doc-test-clean-00")
    twin_confidences = [f.confidence for f in scored_twin.line_fields]
    clean_confidences = [f.confidence for f in scored_clean.line_fields]
    assert twin_confidences == pytest.approx(clean_confidences)


def test_a_document_no_label_names_is_still_scored_from_its_own_signals() -> None:
    """Production has no answer key, so absence from one cannot change a score.

    The earlier version of this pass built its features from the manifest record
    and left any document without one unscored at zero. That made membership of a
    dataset an input to a confidence, which is exactly what a deployed pipeline
    cannot have.
    """
    documents, records = _corpus()
    stranger = _document(
        "doc-stranger-00", CLEAN_CENTS, sum(CLEAN_CENTS), ExtractionRoute.DIGITAL_PDF
    )
    documents.append(stranger)  # named by no record, so no label mentions it

    result = apply_confidence(documents, _labels(documents, records))
    scored_stranger = next(doc for doc in result.documents if doc.doc_id == "doc-stranger-00")
    fields = (*scored_stranger.header_fields, *scored_stranger.line_fields)
    assert fields
    assert all(field.confidence > UNSCORED_CONFIDENCE for field in fields)
    # And it scores what an identical document inside the dataset scores.
    twin = next(doc for doc in result.documents if doc.doc_id == "doc-test-clean-00")
    assert [f.confidence for f in scored_stranger.line_fields] == pytest.approx(
        [f.confidence for f in twin.line_fields]
    )


def test_a_family_no_scorer_learned_stays_in_the_queue_at_zero() -> None:
    """A reference field nothing was trained on cannot be scored, so it queues."""
    documents, records = _corpus()
    doc_id = "doc-test-clean-00"
    document = next(doc for doc in documents if doc.doc_id == doc_id)
    reference = ExtractedField(
        field_id=f"{doc_id}-01-claim_number",
        doc_id=doc_id,
        line_no=1,
        name=FieldName.CLAIM_NUMBER,
        family=FieldFamily.REFERENCE,
        raw_text="NS12345678",
        value="NS12345678",
        source=FieldSource.LLM_VISION,
        signals=FieldSignals(),
    )
    documents[documents.index(document)] = document.model_copy(
        update={"line_fields": (*document.line_fields, reference)}
    )

    result = apply_confidence(documents, _labels(documents, records))
    assert result.threshold_for(FieldFamily.REFERENCE) is None
    scored_field = next(
        field
        for doc in result.documents
        if doc.doc_id == doc_id
        for field in doc.line_fields
        if field.family is FieldFamily.REFERENCE
    )
    assert scored_field.confidence == UNSCORED_CONFIDENCE
    assert scored_field.status is ReviewStatus.NEEDS_REVIEW


@pytest.mark.parametrize("split", [SplitName.CALIBRATION, SplitName.TEST])
def test_fitting_on_anything_but_train_raises_through_the_new_path(split: SplitName) -> None:
    documents, records = _corpus()
    with pytest.raises(SplitDisciplineError):
        apply_confidence(documents, _labels(documents, records), fit_split=split)


@pytest.mark.parametrize("split", [SplitName.TRAIN, SplitName.TEST])
def test_choosing_thresholds_on_anything_but_calibration_raises(split: SplitName) -> None:
    documents, records = _corpus()
    with pytest.raises(SplitDisciplineError):
        apply_confidence(documents, _labels(documents, records), threshold_split=split)
