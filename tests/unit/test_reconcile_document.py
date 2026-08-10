"""Reconciling one document out of the review database.

The build reconciles a saved extraction; the review API reconciles the same
document again after a reviewer corrects a value. This pins the two together:
the rows the database path writes are compared against `reconcile` driven by
`crossfoot.evals.runner.statement_from_extraction`, which is the shape the eval
side has always fed the engine. A refactor that lets the serving path see a
different statement than the build did fails here.

The fixture is one parts statement over a four entry ledger, arranged so a single
reconciliation emits four of the six exception types.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from crossfoot.constants import (
    DocType,
    ExceptionStatus,
    ExceptionType,
    ExtractionRoute,
    FieldFamily,
    FieldName,
    FieldSource,
    Oem,
    QualityTier,
    ReconMode,
    ReviewStatus,
    ScheduleType,
)
from crossfoot.db import connect, reconciliation, review
from crossfoot.db.schema import ensure_schema
from crossfoot.evals.runner import statement_from_extraction
from crossfoot.models.extraction import ExtractedDocument, ExtractedField, FieldSignals
from crossfoot.models.ledger import Dealer, LedgerBook, LedgerEntry
from crossfoot.models.manifest import ManifestRecord
from crossfoot.models.reconciliation import ExceptionRecord
from crossfoot.models.statement import StatementDoc, StatementLine
from crossfoot.reconcile.engine import reconcile
from crossfoot.reconcile.statement import FieldValue, StatementIdentity, statement_from_fields

DOC_ID = "doc-parts-1"
DEALER_ID = "dlr-1"
RUN_ID = "ingest-test"
REVIEWER = "rc"
NOW = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
PERIOD_START = date(2026, 7, 1)
PERIOD_END = date(2026, 7, 31)

IDENTITY = StatementIdentity(
    dealer_id=DEALER_ID,
    doc_type=DocType.PARTS_STATEMENT,
    oem=Oem.MERIDIAN,
    period_start=PERIOD_START,
    period_end=PERIOD_END,
)

# line_no, invoice, date, cents. Line 1 is read low, line 3 is on no schedule,
# line 4 settles an entry the books posted in the previous period. Line 3's
# invoice is several edits from every entry, since one edit is a fuzzy match.
LINES: tuple[tuple[int, str, date, int], ...] = (
    (1, "M0000001", date(2026, 7, 5), 90_00),
    (2, "M0000002", date(2026, 7, 12), 250_00),
    (3, "M0007777", date(2026, 7, 15), 33_00),
    (4, "M0000004", date(2026, 7, 25), 700_00),
)

AMOUNT_FIELD = f"fld-{DOC_ID}-01-line_amount"
DESCRIPTION_FIELD = f"fld-{DOC_ID}-01-description"

_INSERT_DOCUMENT = """
INSERT INTO documents (
    doc_id, file_path, doc_type, quality_tier, route, split, error_kind,
    dealer_id, oem, period_start, period_end
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_INSERT_FIELD = """
INSERT INTO fields (
    field_id, doc_id, line_no, name, family, raw_text, value,
    value_cents, value_date, source, crop_kind, page,
    x0, y0, x1, y1, confidence, status, signals
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def _entry(entry_id: str, invoice: str, post_date: date, cents: int) -> LedgerEntry:
    return LedgerEntry(
        entry_id=entry_id,
        dealer_id=DEALER_ID,
        schedule=ScheduleType.PARTS_PAYABLE,
        gl_account="1400",
        invoice_number=invoice,
        post_date=post_date,
        amount_cents=cents,
        description="brake kit",
        counterparty="Meridian Parts",
    )


@pytest.fixture
def book() -> LedgerBook:
    return LedgerBook(
        dealers=(Dealer(dealer_id=DEALER_ID, name="Dealer One", oem=Oem.MERIDIAN),),
        entries=(
            _entry("led-1", "M0000001", date(2026, 7, 5), 100_00),
            _entry("led-2", "M0000002", date(2026, 7, 12), 250_00),
            _entry("led-3", "M0000003", date(2026, 7, 20), 500_00),
            # Posted the period before, so blocking reaches it and expectation does not.
            _entry("led-4", "M0000004", date(2026, 6, 20), 700_00),
        ),
    )


def _field(
    name: FieldName,
    family: FieldFamily,
    *,
    line_no: int | None,
    value: str,
    value_cents: int | None = None,
    value_date: date | None = None,
) -> ExtractedField:
    suffix = "header" if line_no is None else f"{line_no:02d}"
    return ExtractedField(
        field_id=f"fld-{DOC_ID}-{suffix}-{name.value}",
        doc_id=DOC_ID,
        line_no=line_no,
        name=name,
        family=family,
        raw_text=value,
        value=value,
        value_cents=value_cents,
        value_date=value_date,
        source=FieldSource.DETERMINISTIC,
        signals=FieldSignals(route=ExtractionRoute.DIGITAL_PDF),
        confidence=0.5,
        status=ReviewStatus.NEEDS_REVIEW,
    )


@pytest.fixture
def document() -> ExtractedDocument:
    """The statement as the extractor read it, header fields and four lines."""
    lines: list[ExtractedField] = []
    for line_no, invoice, line_date, cents in LINES:
        lines.append(
            _field(FieldName.INVOICE_NUMBER, FieldFamily.REFERENCE, line_no=line_no, value=invoice)
        )
        lines.append(
            _field(
                FieldName.LINE_DATE,
                FieldFamily.DATE,
                line_no=line_no,
                value=line_date.isoformat(),
                value_date=line_date,
            )
        )
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
            _field(FieldName.DESCRIPTION, FieldFamily.TEXT, line_no=line_no, value="brake kit")
        )
    total = sum(cents for _, _, _, cents in LINES)
    return ExtractedDocument(
        doc_id=DOC_ID,
        file_path=f"files/{DOC_ID}.pdf",
        route=ExtractionRoute.DIGITAL_PDF,
        doc_type=DocType.PARTS_STATEMENT,
        header_fields=(
            _field(
                FieldName.STATEMENT_NUMBER,
                FieldFamily.REFERENCE,
                line_no=None,
                value="STMT-202607-01",
            ),
            _field(
                FieldName.STATEMENT_DATE,
                FieldFamily.DATE,
                line_no=None,
                value=PERIOD_END.isoformat(),
                value_date=PERIOD_END,
            ),
            _field(
                FieldName.TOTAL,
                FieldFamily.AMOUNT,
                line_no=None,
                value=f"{total / 100:.2f}",
                value_cents=total,
            ),
        ),
        line_fields=tuple(lines),
    )


@pytest.fixture
def record() -> ManifestRecord:
    """What an eval holds about the same document, so the reference path can run."""
    truth = StatementDoc(
        doc_id=DOC_ID,
        dealer_id=DEALER_ID,
        doc_type=DocType.PARTS_STATEMENT,
        oem=Oem.MERIDIAN,
        statement_number="STMT-202607-01",
        statement_date=PERIOD_END,
        period_start=PERIOD_START,
        period_end=PERIOD_END,
        subtotal_cents=0,
        total_cents=0,
        lines=(),
    )
    return ManifestRecord(
        doc_id=DOC_ID,
        file_path=f"files/{DOC_ID}.pdf",
        quality_tier=QualityTier.CLEAN_DIGITAL,
        template_id="parts-1",
        render_seed=1,
        truth=truth,
    )


@pytest.fixture
def db_path(tmp_path: Path, document: ExtractedDocument) -> Path:
    """The review database as a build would leave it, exceptions aside."""
    path = tmp_path / "crossfoot.db"
    connection = connect(path)
    try:
        with connection:
            ensure_schema(connection)
            connection.execute(
                _INSERT_DOCUMENT,
                (
                    DOC_ID,
                    f"files/{DOC_ID}.pdf",
                    DocType.PARTS_STATEMENT.value,
                    QualityTier.CLEAN_DIGITAL.value,
                    ExtractionRoute.DIGITAL_PDF.value,
                    None,
                    None,
                    DEALER_ID,
                    Oem.MERIDIAN.value,
                    PERIOD_START.isoformat(),
                    PERIOD_END.isoformat(),
                ),
            )
            for field in (*document.header_fields, *document.line_fields):
                connection.execute(
                    _INSERT_FIELD,
                    (
                        field.field_id,
                        field.doc_id,
                        field.line_no,
                        field.name.value,
                        field.family.value,
                        field.raw_text,
                        field.value,
                        field.value_cents,
                        None if field.value_date is None else field.value_date.isoformat(),
                        field.source.value,
                        field.crop_kind.value,
                        None,
                        None,
                        None,
                        None,
                        None,
                        field.confidence,
                        field.status.value,
                        field.signals.model_dump_json(),
                    ),
                )
    finally:
        connection.close()
    return path


def _rows(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(connection.execute("SELECT * FROM exceptions ORDER BY exception_id"))


def _comparable(row: sqlite3.Row) -> tuple[object, ...]:
    return (
        str(row["exception_id"]),
        str(row["exception_type"]),
        row["statement_line_no"],
        row["ledger_entry_id"],
        row["match_key"],
        row["statement_amount_cents"],
        row["ledger_amount_cents"],
        int(row["dollar_impact_cents"]),
        int(row["memo_amount_cents"]),
        str(row["explanation"]),
        str(row["status"]),
        str(row["detected_at"]),
    )


def _expected(record: ExceptionRecord) -> tuple[object, ...]:
    return (
        record.exception_id,
        record.exception_type.value,
        record.statement_line_no,
        record.ledger_entry_id,
        record.match_key,
        record.statement_amount_cents,
        record.ledger_amount_cents,
        record.dollar_impact_cents,
        record.memo_amount_cents,
        record.explanation,
        record.status.value,
        record.detected_at.isoformat(),
    )


def _reference(
    document: ExtractedDocument, record: ManifestRecord, book: LedgerBook
) -> tuple[ExceptionRecord, ...]:
    """What the build has always produced, driven straight off the extraction."""
    statement = statement_from_extraction(document, record)
    assert statement is not None
    return reconcile(statement, book, mode=ReconMode.END_TO_END, run_id=RUN_ID, now=NOW).exceptions


def _run(db_path: Path, book: LedgerBook) -> list[sqlite3.Row]:
    connection = connect(db_path)
    try:
        with connection:
            reconciliation.reconcile_document(
                connection, doc_id=DOC_ID, book=book, run_id=RUN_ID, now=NOW
            )
        return _rows(connection)
    finally:
        connection.close()


def test_the_fixture_exercises_four_of_the_six_exception_types(
    document: ExtractedDocument, record: ManifestRecord, book: LedgerBook
) -> None:
    """A comparison over one exception type would not be much of a comparison."""
    kinds = {exception.exception_type for exception in _reference(document, record, book)}
    assert kinds == {
        ExceptionType.AMOUNT_MISMATCH,
        ExceptionType.MISSING_FROM_LEDGER,
        ExceptionType.MISSING_FROM_STATEMENT,
        ExceptionType.TIMING_DIFFERENCE,
    }


def test_the_database_path_reproduces_the_build_path_exactly(
    db_path: Path, document: ExtractedDocument, record: ManifestRecord, book: LedgerBook
) -> None:
    """One reconciliation, called twice: the rows must be indistinguishable."""
    rows = _run(db_path, book)
    expected = sorted(_expected(record_) for record_ in _reference(document, record, book))
    assert [_comparable(row) for row in rows] == expected


def test_a_document_with_no_stored_identity_is_not_reconciled(
    db_path: Path, book: LedgerBook
) -> None:
    """A file nothing could be extracted from has no dealer and no period to block on."""
    connection = connect(db_path)
    try:
        with connection:
            connection.execute("UPDATE documents SET dealer_id = NULL WHERE doc_id = ?", (DOC_ID,))
            delta = reconciliation.reconcile_document(
                connection, doc_id=DOC_ID, book=book, run_id=RUN_ID, now=NOW
            )
        assert delta is None
        assert _rows(connection) == []
    finally:
        connection.close()


def test_a_correction_is_what_the_reconciler_sees(db_path: Path, book: LedgerBook) -> None:
    """`fields.value` still says 90.00; the newest correction says otherwise."""
    _run(db_path, book)
    connection = connect(db_path)
    try:
        review.correct(connection, field_id=AMOUNT_FIELD, new_value="100.00", reviewer=REVIEWER)
        with connection:
            delta = reconciliation.reconcile_document(
                connection, doc_id=DOC_ID, book=book, run_id=RUN_ID, now=NOW
            )
        types = {str(row["exception_type"]) for row in _rows(connection)}
        stored = connection.execute(
            "SELECT value FROM fields WHERE field_id = ?", (AMOUNT_FIELD,)
        ).fetchone()
    finally:
        connection.close()
    assert delta is not None
    assert delta.exceptions_removed == 1
    assert delta.exceptions_added == 0
    assert delta.dollars_at_risk_change_cents == -1_000
    assert ExceptionType.AMOUNT_MISMATCH.value not in types
    assert str(stored["value"]) == "90.00"


def test_a_resolved_exception_is_not_reopened_by_a_rerun(db_path: Path, book: LedgerBook) -> None:
    """A human closed it; re-deriving the same finding may not undo that."""
    _run(db_path, book)
    connection = connect(db_path)
    try:
        (resolved_id,) = connection.execute(
            "SELECT exception_id FROM exceptions WHERE exception_type = ?",
            (ExceptionType.AMOUNT_MISMATCH.value,),
        ).fetchone()
        with connection:
            connection.execute(
                "UPDATE exceptions SET status = ?, resolution = ?, resolved_at = ?"
                " WHERE exception_id = ?",
                (ExceptionStatus.RESOLVED.value, "credit issued", NOW.isoformat(), resolved_id),
            )
        # A correction somewhere else on the document, so the resolved finding
        # comes back out of the engine unchanged.
        review.correct(
            connection, field_id=DESCRIPTION_FIELD, new_value="brake kit, front", reviewer=REVIEWER
        )
        with connection:
            delta = reconciliation.reconcile_document(
                connection, doc_id=DOC_ID, book=book, run_id=RUN_ID, now=NOW
            )
        row = connection.execute(
            "SELECT * FROM exceptions WHERE exception_id = ?", (resolved_id,)
        ).fetchone()
    finally:
        connection.close()
    assert str(row["status"]) == ExceptionStatus.RESOLVED.value
    assert str(row["resolution"]) == "credit issued"
    assert str(row["resolved_at"]) == NOW.isoformat()
    assert delta is not None
    # The resolved finding carries no open dollars on either side of the rerun.
    assert delta == delta.model_copy(update={"dollars_at_risk_change_cents": 0})


def test_a_statement_falls_back_rather_than_refusing_when_its_header_went_unread() -> None:
    """Neither the number nor the date reaches the matcher, so neither may block one."""
    values = (
        FieldValue(
            name=FieldName.LINE_AMOUNT, line_no=1, value="10.00", value_cents=1_000, value_date=None
        ),
        FieldValue(
            name=FieldName.LINE_DATE,
            line_no=1,
            value="2026-07-05",
            value_cents=None,
            value_date=date(2026, 7, 5),
        ),
    )
    statement = statement_from_fields(DOC_ID, values, IDENTITY)
    assert statement.statement_number == DOC_ID
    assert statement.statement_date == PERIOD_END
    assert statement.total_cents == 1_000
    assert statement.lines == (
        StatementLine(
            line_no=1,
            line_type=statement.lines[0].line_type,
            line_date=date(2026, 7, 5),
            description="",
            amount_cents=1_000,
        ),
    )


def test_a_line_missing_its_amount_or_date_is_not_a_line() -> None:
    """The engine matches on amount and date, so a line without both is no line."""
    values = (
        FieldValue(
            name=FieldName.LINE_AMOUNT, line_no=1, value="10.00", value_cents=1_000, value_date=None
        ),
    )
    assert statement_from_fields(DOC_ID, values, IDENTITY).lines == ()
