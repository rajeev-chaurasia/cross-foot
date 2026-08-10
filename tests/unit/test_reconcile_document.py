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
import threading
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

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
from crossfoot.db import connect, exceptions, reconciliation, review
from crossfoot.db.schema import ensure_schema
from crossfoot.evals.runner import statement_from_extraction
from crossfoot.models.extraction import ExtractedDocument, ExtractedField, FieldSignals
from crossfoot.models.ledger import Dealer, LedgerBook, LedgerEntry
from crossfoot.models.manifest import ManifestRecord
from crossfoot.models.reconciliation import ExceptionRecord, ReconciliationDelta
from crossfoot.models.statement import StatementDoc, StatementLine
from crossfoot.reconcile.engine import reconcile
from crossfoot.reconcile.statement import FieldValue, StatementIdentity, statement_from_fields

DOC_ID = "doc-parts-1"
DEALER_ID = "dlr-1"
RUN_ID = "ingest-test"
REVIEWER = "rc"
RESOLUTION = "credit issued, ticket 4471"
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

# The statement's own reading of line 1, and the ledger's.
LINE_1_STATEMENT_CENTS = 90_00
LINE_1_LEDGER_CENTS = 100_00

# Long enough that a thread blocked on the write lock is still blocked when the
# holder gives up waiting for it, short enough to pay once per test.
HANDOVER_SECONDS = 0.4

# Where a rerun stops reading and starts writing. Everything a concurrent
# correction or resolve can invalidate has been read by the time this runs.
_FIRST_WRITE = "DELETE FROM exceptions"

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


def _exception_id_of(db_path: Path, exception_type: ExceptionType) -> str:
    connection = connect(db_path)
    try:
        (found,) = connection.execute(
            "SELECT exception_id FROM exceptions WHERE exception_type = ?",
            (exception_type.value,),
        ).fetchone()
    finally:
        connection.close()
    return str(found)


def _open_risk(db_path: Path) -> int:
    """The tile: absolute impact of everything still open on this document."""
    connection = connect(db_path)
    try:
        (total,) = connection.execute(
            "SELECT COALESCE(SUM(ABS(dollar_impact_cents)), 0) FROM exceptions"
            " WHERE doc_id = ? AND status = ?",
            (DOC_ID, ExceptionStatus.OPEN.value),
        ).fetchone()
    finally:
        connection.close()
    return int(total)


class _InterleavedConnection(sqlite3.Connection):
    """A connection that hands over to another thread just before a rerun writes.

    A concurrency finding is an interleaving, not a timing accident, so the tests
    below put the other thread's work exactly where the failure needs it rather
    than starting threads and hoping. Holding the write lock across the read is
    what makes the other thread arrive too late, which is the thing being pinned.
    """

    interleave: Callable[[], None]

    def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
        if sql.startswith(_FIRST_WRITE):
            self.interleave()
        return super().execute(sql, parameters)


def _interleaved(db_path: Path, interleave: Callable[[], None]) -> _InterleavedConnection:
    connection = sqlite3.connect(db_path, factory=_InterleavedConnection)
    connection.row_factory = sqlite3.Row
    connection.interleave = interleave
    return connection


def _correct_and_reconcile(
    db_path: Path,
    book: LedgerBook,
    *,
    field_id: str,
    value: str,
    interleave: Callable[[], None],
) -> ReconciliationDelta:
    """One reviewer's correction and the re-derivation it triggers, as the API runs it."""
    connection = _interleaved(db_path, interleave)
    try:
        review.correct(connection, field_id=field_id, new_value=value, reviewer=REVIEWER)
        with connection:
            delta = reconciliation.reconcile_document(
                connection, doc_id=DOC_ID, book=book, run_id=RUN_ID, now=NOW
            )
    finally:
        connection.close()
    assert delta is not None
    return delta


def _resolve_elsewhere(db_path: Path, exception_id: str) -> None:
    connection = connect(db_path)
    try:
        exceptions.resolve(connection, exception_id=exception_id, resolution=RESOLUTION)
    finally:
        connection.close()


def _wait_for(barrier: threading.Barrier) -> None:
    """Wait for the other reruns, or give up when the lock has already stopped them."""
    with suppress(threading.BrokenBarrierError):
        barrier.wait()


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
        exceptions.resolve(connection, exception_id=resolved_id, resolution=RESOLUTION)
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
    assert str(row["resolution"]) == RESOLUTION
    assert delta is not None
    # The resolved finding carries no open dollars on either side of the rerun.
    assert delta == delta.model_copy(update={"dollars_at_risk_change_cents": 0})


def test_clearing_one_finding_leaves_every_other_id_where_it_was(
    db_path: Path, book: LedgerBook
) -> None:
    """A reviewer resolves by id, so a rerun that renumbers closes the wrong finding.

    Line 1's amount mismatch is cleared by a correction. Under emission order that
    shifts every later finding onto the id in front of it, and the reviewer who
    was reading one of them resolves another.
    """
    _run(db_path, book)
    connection = connect(db_path)
    try:
        before = _ids_by_finding(connection)
        review.correct(connection, field_id=AMOUNT_FIELD, new_value="100.00", reviewer=REVIEWER)
        with connection:
            reconciliation.reconcile_document(
                connection, doc_id=DOC_ID, book=book, run_id=RUN_ID, now=NOW
            )
        after = _ids_by_finding(connection)
    finally:
        connection.close()
    cleared = (ExceptionType.AMOUNT_MISMATCH.value, 1, "led-1")
    assert set(before) - set(after) == {cleared}
    assert after
    assert after == {finding: before[finding] for finding in after}


def _ids_by_finding(connection: sqlite3.Connection) -> dict[tuple[object, ...], str]:
    """What each finding is about, mapped to the id it is being worked under."""
    return {
        (str(row["exception_type"]), row["statement_line_no"], row["ledger_entry_id"]): str(
            row["exception_id"]
        )
        for row in _rows(connection)
    }


def test_a_resolution_does_not_outlive_the_money_it_was_written_about(
    db_path: Path, book: LedgerBook
) -> None:
    """A decision covers the facts in front of the reviewer, not the row's name.

    Nine dollars was closed with a note. Corrected, the same finding is most of a
    million, which nobody has looked at, so it is open again and the money is back
    on the tile.
    """
    _run(db_path, book)
    resolved_id = _exception_id_of(db_path, ExceptionType.AMOUNT_MISMATCH)
    connection = connect(db_path)
    try:
        exceptions.resolve(connection, exception_id=resolved_id, resolution=RESOLUTION)
        review.correct(connection, field_id=AMOUNT_FIELD, new_value="998360.21", reviewer=REVIEWER)
        with connection:
            delta = reconciliation.reconcile_document(
                connection, doc_id=DOC_ID, book=book, run_id=RUN_ID, now=NOW
            )
        row = connection.execute(
            "SELECT * FROM exceptions WHERE exception_id = ?", (resolved_id,)
        ).fetchone()
    finally:
        connection.close()
    # Same finding, by identity: the line and the entry it is about did not move.
    assert str(row["exception_id"]) == resolved_id
    assert str(row["status"]) == ExceptionStatus.OPEN.value
    assert row["resolution"] is None
    assert row["resolved_at"] is None
    assert delta is not None
    assert delta.exceptions_added == 1
    assert delta.dollars_at_risk_change_cents == 99_836_021 - LINE_1_LEDGER_CENTS


def test_a_resolution_comes_back_with_the_finding_it_was_written_for(
    db_path: Path, book: LedgerBook
) -> None:
    """Resolve, correct until it clears, correct back: the decision is still there.

    The note is keyed by what the finding is, so it outlives the row. Coming back
    closed, it brings no risk with it.
    """
    _run(db_path, book)
    resolved_id = _exception_id_of(db_path, ExceptionType.AMOUNT_MISMATCH)
    connection = connect(db_path)
    try:
        exceptions.resolve(connection, exception_id=resolved_id, resolution=RESOLUTION)
        for value in (f"{LINE_1_LEDGER_CENTS / 100:.2f}", f"{LINE_1_STATEMENT_CENTS / 100:.2f}"):
            review.correct(connection, field_id=AMOUNT_FIELD, new_value=value, reviewer=REVIEWER)
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
    assert str(row["resolution"]) == RESOLUTION
    assert delta == ReconciliationDelta(
        exceptions_removed=0, exceptions_added=0, dollars_at_risk_change_cents=0
    )


def test_a_rerun_that_changes_nothing_reports_three_zeroes(db_path: Path, book: LedgerBook) -> None:
    """The baseline every other delta is read against."""
    _run(db_path, book)
    connection = connect(db_path)
    try:
        with connection:
            delta = reconciliation.reconcile_document(
                connection, doc_id=DOC_ID, book=book, run_id=RUN_ID, now=NOW
            )
    finally:
        connection.close()
    assert delta == ReconciliationDelta(
        exceptions_removed=0, exceptions_added=0, dollars_at_risk_change_cents=0
    )


def test_clearing_a_finding_a_reviewer_had_already_closed_moves_nothing(
    db_path: Path, book: LedgerBook
) -> None:
    """Both halves of the delta describe one population, the open findings.

    A resolved finding is not risk, so it leaving is not a clearance. Counting it
    put "cleared 1" beside a dollar figure of zero.
    """
    _run(db_path, book)
    resolved_id = _exception_id_of(db_path, ExceptionType.AMOUNT_MISMATCH)
    connection = connect(db_path)
    try:
        exceptions.resolve(connection, exception_id=resolved_id, resolution=RESOLUTION)
        review.correct(connection, field_id=AMOUNT_FIELD, new_value="100.00", reviewer=REVIEWER)
        with connection:
            delta = reconciliation.reconcile_document(
                connection, doc_id=DOC_ID, book=book, run_id=RUN_ID, now=NOW
            )
        gone = connection.execute(
            "SELECT COUNT(*) FROM exceptions WHERE exception_id = ?", (resolved_id,)
        ).fetchone()
    finally:
        connection.close()
    assert int(gone[0]) == 0
    assert delta == ReconciliationDelta(
        exceptions_removed=0, exceptions_added=0, dollars_at_risk_change_cents=0
    )


def test_corrections_landing_together_report_deltas_that_sum_to_the_move(
    db_path: Path, book: LedgerBook
) -> None:
    """Four reviewers, one document, one move: the panels have to add up to it.

    Every rerun is held at the point it would write until the others have caught
    up, so each one either read the state the others are about to change or was
    made to wait for them.
    """
    _run(db_path, book)
    corrections = (("01", "100.00"), ("02", "300.00"), ("03", "44.00"), ("04", "705.00"))
    barrier = threading.Barrier(len(corrections), timeout=HANDOVER_SECONDS)
    opening = _open_risk(db_path)
    deltas = _in_parallel(db_path, book, corrections, barrier)
    closing = _open_risk(db_path)
    assert sum(delta.dollars_at_risk_change_cents for delta in deltas) == closing - opening
    # Line 1 now agrees; lines 2 and 4 disagree by 50.00 and 5.00; line 3 is still
    # on no schedule at 44.00; the ledger's own 500.00 entry is still unclaimed.
    # Every correction is in the final state, so no rerun overwrote another's.
    assert closing == 5_000 + 500 + 44_00 + 500_00


def _in_parallel(
    db_path: Path,
    book: LedgerBook,
    corrections: Sequence[tuple[str, str]],
    barrier: threading.Barrier,
) -> list[ReconciliationDelta]:
    with ThreadPoolExecutor(max_workers=len(corrections)) as pool:
        futures = [
            pool.submit(
                _correct_and_reconcile,
                db_path,
                book,
                field_id=f"fld-{DOC_ID}-{suffix}-line_amount",
                value=value,
                interleave=lambda: _wait_for(barrier),
            )
            for suffix, value in corrections
        ]
        return [future.result() for future in futures]


def test_a_resolve_landing_mid_rerun_is_not_written_back_over(
    db_path: Path, book: LedgerBook
) -> None:
    """The same lock, the other direction: a decision taken while a rerun is in flight.

    The resolve is released at the moment the rerun stops reading. It has to end
    up applied, whether it gets in first or waits for the write to finish.
    """
    _run(db_path, book)
    resolved_id = _exception_id_of(db_path, ExceptionType.MISSING_FROM_LEDGER)
    resolver = threading.Thread(target=_resolve_elsewhere, args=(db_path, resolved_id))

    def interleave() -> None:
        resolver.start()
        resolver.join(timeout=HANDOVER_SECONDS)

    _correct_and_reconcile(
        db_path,
        book,
        field_id=DESCRIPTION_FIELD,
        value="brake kit, front",
        interleave=interleave,
    )
    resolver.join()
    connection = connect(db_path)
    try:
        row = connection.execute(
            "SELECT * FROM exceptions WHERE exception_id = ?", (resolved_id,)
        ).fetchone()
    finally:
        connection.close()
    assert str(row["status"]) == ExceptionStatus.RESOLVED.value
    assert str(row["resolution"]) == RESOLUTION


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
