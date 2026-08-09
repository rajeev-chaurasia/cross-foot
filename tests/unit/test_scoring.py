"""The pass that turns signals into a review decision.

The hand built set is two populations of amount fields: clean documents that
foot, print unambiguous glyphs, and were read correctly, and heavy scans that do
not foot and carry one badly misread line each. That is enough for the family
scorer to separate them, which is what makes the confidences graded rather than
the binary flag extraction hands over.
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
from crossfoot.models.extraction import ExtractedDocument, ExtractedField, FieldSignals
from crossfoot.models.manifest import ManifestRecord
from crossfoot.models.statement import StatementDoc, StatementLine
from crossfoot.scoring import UNSCORED_CONFIDENCE, ConfidencePass, apply_confidence

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
    doc_id: str, name: FieldName, cents: int, *, line_no: int | None, tier: QualityTier
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
        signals=FieldSignals(quality_tier=tier),
    )


def _document(
    doc_id: str, read: tuple[int, ...], printed_total: int, tier: QualityTier
) -> ExtractedDocument:
    """One statement as it was read: the printed total plus every line amount."""
    return ExtractedDocument(
        doc_id=doc_id,
        file_path=f"files/{doc_id}.pdf",
        route=ExtractionRoute.SCANNED_PDF,
        doc_type=DocType.PARTS_STATEMENT,
        header_fields=(
            _amount_field(doc_id, FieldName.TOTAL, printed_total, line_no=None, tier=tier),
        ),
        line_fields=tuple(
            _amount_field(doc_id, FieldName.LINE_AMOUNT, cents, line_no=line_no, tier=tier)
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
    document = _document(doc_id, CLEAN_CENTS, total, QualityTier.CLEAN_DIGITAL)
    return document, _record(doc_id, CLEAN_CENTS, QualityTier.CLEAN_DIGITAL, split)


def _dirty(doc_id: str, split: SplitName) -> tuple[ExtractedDocument, ManifestRecord]:
    """One line read high on a heavy scan, so the document no longer foots."""
    read = (*DIRTY_CENTS[:-1], MISREAD_CENTS)
    document = _document(doc_id, read, sum(DIRTY_CENTS), QualityTier.SCAN_HEAVY)
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
    return apply_confidence(documents, records)


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


def test_a_document_whose_context_is_missing_stays_in_the_queue_at_zero() -> None:
    documents, records = _corpus()
    # A corrupted file carries no truth, so no signal context can be built for it
    # and nothing about its fields is known well enough to auto accept them.
    blind = _document("doc-blind-00", CLEAN_CENTS, sum(CLEAN_CENTS), QualityTier.CORRUPTED)
    documents.append(blind)
    records["doc-blind-00"] = _record("doc-blind-00", CLEAN_CENTS, QualityTier.CORRUPTED, None)
    records["doc-blind-00"] = records["doc-blind-00"].model_copy(update={"truth": None})

    result = apply_confidence(documents, records)
    scored_blind = next(doc for doc in result.documents if doc.doc_id == "doc-blind-00")
    fields = (*scored_blind.header_fields, *scored_blind.line_fields)
    assert fields
    assert all(field.confidence == UNSCORED_CONFIDENCE for field in fields)
    assert all(field.status is ReviewStatus.NEEDS_REVIEW for field in fields)


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
        signals=FieldSignals(quality_tier=QualityTier.CLEAN_DIGITAL),
    )
    documents[documents.index(document)] = document.model_copy(
        update={"line_fields": (*document.line_fields, reference)}
    )

    result = apply_confidence(documents, records)
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
        apply_confidence(documents, records, fit_split=split)


@pytest.mark.parametrize("split", [SplitName.TRAIN, SplitName.TEST])
def test_choosing_thresholds_on_anything_but_calibration_raises(split: SplitName) -> None:
    documents, records = _corpus()
    with pytest.raises(SplitDisciplineError):
        apply_confidence(documents, records, threshold_split=split)
