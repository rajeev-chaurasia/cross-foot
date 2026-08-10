"""The seam that turns an extraction into the statement the reconciler matches.

End to end reconciliation is only an extraction measurement if this function
carries the extraction's own readings through. What it may take from the record
is the identity the reconciler blocks on and no extractor produces: the dealer,
the document type, the marque, and the period. Everything a line says has to
come from the extraction or the measurement is circular.
"""

from datetime import date

import pytest

from crossfoot.constants import (
    DocType,
    ExtractionRoute,
    FieldFamily,
    FieldName,
    FieldSource,
    LineType,
    Oem,
    QualityTier,
    SplitName,
)
from crossfoot.evals.runner import statement_from_extraction
from crossfoot.models.extraction import ExtractedDocument, ExtractedField, FieldSignals
from crossfoot.models.manifest import ManifestRecord
from crossfoot.models.statement import StatementDoc, StatementLine

DOC_ID = "doc-parts_statement-dlr-atlas-202604-01"
TRUTH_TOTAL_CENTS = 250_000
LINE_ONE_CENTS = 100_000
LINE_TWO_CENTS = 150_000
READ_TOTAL_CENTS = 175_000


def _truth() -> StatementDoc:
    return StatementDoc(
        doc_id=DOC_ID,
        dealer_id="dlr-atlas",
        doc_type=DocType.PARTS_STATEMENT,
        oem=Oem.ATLAS,
        statement_number="ATL-0001",
        statement_date=date(2026, 4, 30),
        period_start=date(2026, 4, 1),
        period_end=date(2026, 4, 30),
        subtotal_cents=TRUTH_TOTAL_CENTS,
        total_cents=TRUTH_TOTAL_CENTS,
        lines=(
            StatementLine(
                line_no=1,
                line_type=LineType.CHARGE,
                invoice_number="ATL-INV-0001",
                line_date=date(2026, 4, 3),
                description="Brake pad set",
                amount_cents=LINE_ONE_CENTS,
            ),
            StatementLine(
                line_no=2,
                line_type=LineType.CHARGE,
                invoice_number="ATL-INV-0002",
                line_date=date(2026, 4, 9),
                description="Oil filter",
                amount_cents=LINE_TWO_CENTS,
            ),
        ),
    )


def _record() -> ManifestRecord:
    return ManifestRecord(
        doc_id=DOC_ID,
        file_path=f"files/{DOC_ID}.pdf",
        quality_tier=QualityTier.SCAN_LIGHT,
        template_id="atlas-parts",
        render_seed=1,
        truth=_truth(),
        split=SplitName.TEST,
    )


def _field(name: FieldName, family: FieldFamily, **kwargs: object) -> ExtractedField:
    line_no = kwargs.pop("line_no", None)
    return ExtractedField(
        field_id=f"fld-{DOC_ID}-{line_no or 0:04d}-{name.value}",
        doc_id=DOC_ID,
        line_no=line_no,
        name=name,
        family=family,
        source=FieldSource.LLM_VISION,
        signals=FieldSignals(),
        **kwargs,
    )


def _extraction(total_cents: int | None) -> ExtractedDocument:
    header = [
        _field(
            FieldName.STATEMENT_NUMBER,
            FieldFamily.REFERENCE,
            value="ATL-0001",
            raw_text="ATL-0001",
        )
    ]
    if total_cents is not None:
        header.append(
            _field(
                FieldName.TOTAL,
                FieldFamily.AMOUNT,
                value=f"{total_cents / 100:.2f}",
                value_cents=total_cents,
            )
        )
    lines = []
    for line_no, cents, invoice in ((1, LINE_ONE_CENTS, "ATL-INV-0001"), (2, LINE_TWO_CENTS, "X")):
        lines.append(
            _field(
                FieldName.LINE_AMOUNT,
                FieldFamily.AMOUNT,
                line_no=line_no,
                value=f"{cents / 100:.2f}",
                value_cents=cents,
            )
        )
        lines.append(
            _field(
                FieldName.INVOICE_NUMBER,
                FieldFamily.REFERENCE,
                line_no=line_no,
                value=invoice,
                raw_text=invoice,
            )
        )
        lines.append(
            _field(
                FieldName.LINE_DATE,
                FieldFamily.DATE,
                line_no=line_no,
                value="2026-04-03",
                value_date=date(2026, 4, 3),
            )
        )
    return ExtractedDocument(
        doc_id=DOC_ID,
        file_path=f"files/{DOC_ID}.pdf",
        route=ExtractionRoute.SCANNED_PDF,
        header_fields=tuple(header),
        line_fields=tuple(lines),
    )


def test_a_corrupted_record_with_no_truth_yields_no_statement() -> None:
    record = _record().model_copy(update={"truth": None})

    assert statement_from_extraction(_extraction(TRUTH_TOTAL_CENTS), record) is None


def test_the_lines_carry_what_the_extractor_read_not_what_the_record_knows() -> None:
    """Line two's invoice number was misread. The statement must keep the misreading."""
    statement = statement_from_extraction(_extraction(TRUTH_TOTAL_CENTS), _record())

    assert statement is not None
    assert [line.invoice_number for line in statement.lines] == ["ATL-INV-0001", "X"]


def test_the_blocking_identity_comes_from_the_record() -> None:
    """No extractor produces these, and the reconciler blocks on every one of them."""
    statement = statement_from_extraction(_extraction(TRUTH_TOTAL_CENTS), _record())
    truth = _truth()

    assert statement is not None
    assert statement.dealer_id == truth.dealer_id
    assert statement.doc_type is truth.doc_type
    assert statement.oem is truth.oem
    assert statement.period_start == truth.period_start
    assert statement.period_end == truth.period_end


def test_a_total_the_extractor_read_is_reported_as_read() -> None:
    statement = statement_from_extraction(_extraction(READ_TOTAL_CENTS), _record())

    assert statement is not None
    assert statement.total_cents == READ_TOTAL_CENTS
    assert statement.crossfoot_delta_cents() != 0


def test_a_total_read_as_zero_is_a_reading_and_not_a_missing_reading() -> None:
    """The case an `or` would silently repair, and the one the crossfoot check exists for.

    A statement whose printed total contradicts its own lines is the signature of
    a tampered or misread document. Substituting the sum of the lines here would
    hide it and report a document that adds up perfectly.
    """
    statement = statement_from_extraction(_extraction(0), _record())

    assert statement is not None
    assert statement.total_cents == 0
    assert statement.crossfoot_delta_cents() == -(LINE_ONE_CENTS + LINE_TWO_CENTS)


def test_a_total_the_extractor_never_returned_falls_back_to_the_lines() -> None:
    statement = statement_from_extraction(_extraction(None), _record())

    assert statement is not None
    assert statement.total_cents == LINE_ONE_CENTS + LINE_TWO_CENTS
    assert statement.crossfoot_delta_cents() == 0


@pytest.mark.parametrize("total_cents", [0, READ_TOTAL_CENTS, None])
def test_the_subtotal_is_always_the_lines_that_were_read(total_cents: int | None) -> None:
    statement = statement_from_extraction(_extraction(total_cents), _record())

    assert statement is not None
    assert statement.subtotal_cents == LINE_ONE_CENTS + LINE_TWO_CENTS
