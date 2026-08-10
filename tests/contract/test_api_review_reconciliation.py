"""The correction loop, end to end through the route that closes it.

`POST /api/review/items/{field_id}/correct` keeps every key it had and gains one:

    "reconciliation": {
        "exceptions_removed": int,
        "exceptions_added": int,
        "dollars_at_risk_change_cents": int
    } | null

`dollars_at_risk_change_cents` is the change in the sum of absolute impact of the
document's open exceptions, so a correction that clears risk is negative. Null
when the document cannot be reconciled: no ledger under the dataset directory, or
no blocking identity to match against.

The database is seeded through `crossfoot.db.schema` rather than the phase 1
seeder, because the blocking identity a re-reconciliation reads back lives on the
documents row. The baseline exceptions are laid down by the same
`db.reconciliation` the build calls, which is the point: what the reviewer is
correcting is what the build produced.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from crossfoot.api.app import create_app
from crossfoot.api.ledger import LEDGER_FILENAME
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
    ReviewStatus,
    ScheduleType,
)
from crossfoot.db import connect, reconciliation
from crossfoot.db.schema import ensure_schema
from crossfoot.models.extraction import FieldSignals
from crossfoot.models.ledger import Dealer, LedgerBook, LedgerEntry

DB_NAME = "crossfoot.db"
DOC_ID = "doc-parts-1"
EMPTY_DOC_ID = "doc-header-only"
DEALER_ID = "dlr-1"
RUN_ID = "ingest-contract"
REVIEWER = "rc"
PERIOD_START = date(2026, 7, 1)
PERIOD_END = date(2026, 7, 31)

# Line 1 is read 10.00 low against a 100.00 entry; line 2 agrees with its entry.
MISREAD_AMOUNT_FIELD = "fld-1-line_amount"
MATCHING_AMOUNT_FIELD = "fld-2-line_amount"
DESCRIPTION_FIELD = "fld-1-description"
HEADER_TOTAL_FIELD = "fld-header-total"

MISMATCH_IMPACT_CENTS = -1_000
CREATED_IMPACT_CENTS = 5_000

RECONCILIATION_KEYS = frozenset(
    {"exceptions_removed", "exceptions_added", "dollars_at_risk_change_cents"}
)
ITEM_KEYS = frozenset(
    {
        "field_id",
        "doc_id",
        "line_no",
        "name",
        "family",
        "raw_text",
        "value",
        "confidence",
        "status",
        "crop_url",
    }
)

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

SIGNALS = FieldSignals(route=ExtractionRoute.DIGITAL_PDF).model_dump_json()


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


BOOK = LedgerBook(
    dealers=(Dealer(dealer_id=DEALER_ID, name="Dealer One", oem=Oem.MERIDIAN),),
    entries=(
        _entry("led-1", "M0000001", date(2026, 7, 5), 100_00),
        _entry("led-2", "M0000002", date(2026, 7, 12), 250_00),
    ),
)


def _document(conn: sqlite3.Connection, doc_id: str, *, identified: bool = True) -> None:
    conn.execute(
        _INSERT_DOCUMENT,
        (
            doc_id,
            f"files/{doc_id}.pdf",
            DocType.PARTS_STATEMENT.value,
            QualityTier.CLEAN_DIGITAL.value,
            ExtractionRoute.DIGITAL_PDF.value,
            None,
            None,
            DEALER_ID if identified else None,
            Oem.MERIDIAN.value if identified else None,
            PERIOD_START.isoformat() if identified else None,
            PERIOD_END.isoformat() if identified else None,
        ),
    )


def _field(
    conn: sqlite3.Connection,
    *,
    field_id: str,
    doc_id: str,
    line_no: int | None,
    name: FieldName,
    family: FieldFamily,
    value: str,
    value_cents: int | None = None,
    value_date: date | None = None,
) -> None:
    conn.execute(
        _INSERT_FIELD,
        (
            field_id,
            doc_id,
            line_no,
            name.value,
            family.value,
            value,
            value,
            value_cents,
            None if value_date is None else value_date.isoformat(),
            FieldSource.DETERMINISTIC.value,
            "row_band",
            None,
            None,
            None,
            None,
            None,
            0.4,
            ReviewStatus.NEEDS_REVIEW.value,
            SIGNALS,
        ),
    )


def _line(conn: sqlite3.Connection, line_no: int, invoice: str, day: date, cents: int) -> None:
    _field(
        conn,
        field_id=f"fld-{line_no}-invoice_number",
        doc_id=DOC_ID,
        line_no=line_no,
        name=FieldName.INVOICE_NUMBER,
        family=FieldFamily.REFERENCE,
        value=invoice,
    )
    _field(
        conn,
        field_id=f"fld-{line_no}-line_date",
        doc_id=DOC_ID,
        line_no=line_no,
        name=FieldName.LINE_DATE,
        family=FieldFamily.DATE,
        value=day.isoformat(),
        value_date=day,
    )
    _field(
        conn,
        field_id=f"fld-{line_no}-line_amount",
        doc_id=DOC_ID,
        line_no=line_no,
        name=FieldName.LINE_AMOUNT,
        family=FieldFamily.AMOUNT,
        value=f"{cents / 100:.2f}",
        value_cents=cents,
    )
    _field(
        conn,
        field_id=f"fld-{line_no}-description",
        doc_id=DOC_ID,
        line_no=line_no,
        name=FieldName.DESCRIPTION,
        family=FieldFamily.TEXT,
        value="brake kit",
    )


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """Two documents and the exceptions a build would have left behind."""
    path = tmp_path / DB_NAME
    connection = connect(path)
    try:
        with connection:
            ensure_schema(connection)
            _document(connection, DOC_ID)
            _line(connection, 1, "M0000001", date(2026, 7, 5), 90_00)
            _line(connection, 2, "M0000002", date(2026, 7, 12), 250_00)
            # A document the extractor read a header off and nothing else.
            _document(connection, EMPTY_DOC_ID)
            _field(
                connection,
                field_id=HEADER_TOTAL_FIELD,
                doc_id=EMPTY_DOC_ID,
                line_no=None,
                name=FieldName.TOTAL,
                family=FieldFamily.AMOUNT,
                value="340.00",
                value_cents=34_000,
            )
            reconciliation.reconcile_document(
                connection, doc_id=DOC_ID, book=BOOK, run_id=RUN_ID, now=datetime.now(UTC)
            )
    finally:
        connection.close()
    return path


@pytest.fixture
def dataset_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "dataset"
    directory.mkdir()
    (directory / LEDGER_FILENAME).write_text(BOOK.model_dump_json(), encoding="utf-8")
    return directory


def _app(tmp_path: Path, db_path: Path, dataset_dir: Path) -> Any:
    crops_root = tmp_path / "crops"
    crops_root.mkdir(exist_ok=True)
    scorecards_dir = tmp_path / "scorecards"
    scorecards_dir.mkdir(exist_ok=True)
    return create_app(
        db_path=db_path,
        crops_root=crops_root,
        scorecards_dir=scorecards_dir,
        dataset_dir=dataset_dir,
    )


@pytest.fixture
def client(tmp_path: Path, db_path: Path, dataset_dir: Path) -> Iterator[TestClient]:
    with TestClient(_app(tmp_path, db_path, dataset_dir)) as test_client:
        yield test_client


@pytest.fixture
def ledgerless_client(tmp_path: Path, db_path: Path) -> Iterator[TestClient]:
    """The same database served from a dataset directory holding no ledger."""
    empty = tmp_path / "no-dataset"
    empty.mkdir()
    with TestClient(_app(tmp_path, db_path, empty)) as test_client:
        yield test_client


def correct(client: TestClient, field_id: str, value: str) -> dict[str, Any]:
    response = client.post(
        f"/api/review/items/{field_id}/correct", json={"value": value, "reviewer": REVIEWER}
    )
    assert response.status_code == 200
    payload: dict[str, Any] = response.json()
    return payload


def exception_rows(db_path: Path, doc_id: str) -> list[tuple[str, str, int]]:
    """(type, ledger entry, impact) for one document, straight out of the table."""
    connection = connect(db_path)
    try:
        rows = connection.execute(
            "SELECT exception_type, ledger_entry_id, dollar_impact_cents"
            " FROM exceptions WHERE doc_id = ? ORDER BY exception_id",
            (doc_id,),
        ).fetchall()
    finally:
        connection.close()
    return [
        (str(row["exception_type"]), str(row["ledger_entry_id"]), int(row["dollar_impact_cents"]))
        for row in rows
    ]


def open_exceptions(client: TestClient) -> list[dict[str, Any]]:
    response = client.get("/api/exceptions", params={"status": ExceptionStatus.OPEN.value})
    assert response.status_code == 200
    items: list[dict[str, Any]] = response.json()["items"]
    return items


def test_the_build_left_one_open_exception_for_the_misread_line(client: TestClient) -> None:
    """Everything below is measured against this row, so it is asserted first."""
    (only,) = open_exceptions(client)
    assert only["exception_type"] == ExceptionType.AMOUNT_MISMATCH.value
    assert only["statement_line_no"] == 1
    assert only["ledger_entry_id"] == "led-1"
    assert only["dollar_impact_cents"] == MISMATCH_IMPACT_CENTS


def test_a_correction_that_clears_an_exception_reports_it_removed(client: TestClient) -> None:
    payload = correct(client, MISREAD_AMOUNT_FIELD, "100.00")
    assert payload["reconciliation"] == {
        "exceptions_removed": 1,
        "exceptions_added": 0,
        # Clearing risk is negative, and the risk cleared is the absolute impact.
        "dollars_at_risk_change_cents": -abs(MISMATCH_IMPACT_CENTS),
    }
    assert open_exceptions(client) == []


def test_a_correction_that_creates_an_exception_reports_it_added(client: TestClient) -> None:
    # Line 2 agreed with the books until a reviewer overruled it.
    payload = correct(client, MATCHING_AMOUNT_FIELD, "300.00")
    assert payload["reconciliation"] == {
        "exceptions_removed": 0,
        "exceptions_added": 1,
        "dollars_at_risk_change_cents": CREATED_IMPACT_CENTS,
    }
    lines = {item["statement_line_no"] for item in open_exceptions(client)}
    assert lines == {1, 2}


def test_the_corrected_value_is_the_one_the_reconciler_used(client: TestClient) -> None:
    """The fields row still holds the model's reading, so the correction had to win."""
    correct(client, MISREAD_AMOUNT_FIELD, "100.00")
    detail = client.get(f"/api/review/items/{MISREAD_AMOUNT_FIELD}")
    assert detail.json()["value"] == "100.00"
    assert open_exceptions(client) == []


def test_the_response_keeps_every_key_it_had_and_gains_exactly_one(client: TestClient) -> None:
    payload = correct(client, MISREAD_AMOUNT_FIELD, "100.00")
    assert set(payload) == ITEM_KEYS | {"reconciliation"}
    assert set(payload["reconciliation"]) == RECONCILIATION_KEYS
    assert payload["status"] == ReviewStatus.HUMAN_CORRECTED.value


def test_a_document_with_no_ledger_reports_null_rather_than_erroring(
    ledgerless_client: TestClient,
) -> None:
    payload = correct(ledgerless_client, MISREAD_AMOUNT_FIELD, "100.00")
    assert payload["reconciliation"] is None
    assert payload["status"] == ReviewStatus.HUMAN_CORRECTED.value
    # The correction still landed, and nothing pretended to re-derive anything.
    assert [item["dollar_impact_cents"] for item in open_exceptions(ledgerless_client)] == [
        MISMATCH_IMPACT_CENTS
    ]


def test_an_identity_nothing_can_read_answers_null_rather_than_500(
    client: TestClient, db_path: Path
) -> None:
    """The correction has already landed by the time these columns are read.

    A marque that is not a marque and a period start that is not a date leave a
    document there is nothing to reconcile against, which is an answer. Raising
    here answered a committed write with a 500 and no delta at all.
    """
    connection = connect(db_path)
    try:
        with connection:
            connection.execute(
                "UPDATE documents SET oem = ?, period_start = ? WHERE doc_id = ?",
                ("ford", "April 2026", DOC_ID),
            )
    finally:
        connection.close()
    payload = correct(client, MISREAD_AMOUNT_FIELD, "100.00")
    assert payload["reconciliation"] is None
    assert payload["status"] == ReviewStatus.HUMAN_CORRECTED.value


def test_a_header_only_document_reconciles_the_way_the_build_reconciled_it(
    client: TestClient, db_path: Path
) -> None:
    """A document with no lines still has a period, and a period still expects entries.

    The build reconciles this document and the dashboard shows what it found, so
    a route that answered "cannot be reconciled" was contradicting rows already
    on screen.
    """
    payload = correct(client, HEADER_TOTAL_FIELD, "999.00")
    assert payload["reconciliation"] == {
        "exceptions_removed": 0,
        "exceptions_added": 2,
        # Both entries posted inside the period and no line claimed either.
        "dollars_at_risk_change_cents": 350_00,
    }
    assert exception_rows(db_path, EMPTY_DOC_ID) == [
        (ExceptionType.MISSING_FROM_STATEMENT.value, "led-1", -100_00),
        (ExceptionType.MISSING_FROM_STATEMENT.value, "led-2", -250_00),
    ]


def test_a_resolved_exception_is_not_reopened_by_a_later_correction(client: TestClient) -> None:
    (only,) = open_exceptions(client)
    resolved = client.post(
        f"/api/exceptions/{only['exception_id']}/resolve", json={"resolution": "credit issued"}
    )
    assert resolved.status_code == 200

    # A correction elsewhere on the same document re-derives the same finding.
    payload = correct(client, DESCRIPTION_FIELD, "brake kit, front")
    assert payload["reconciliation"] == {
        "exceptions_removed": 0,
        "exceptions_added": 0,
        # A resolved exception carries no open dollars on either side of the rerun.
        "dollars_at_risk_change_cents": 0,
    }
    assert open_exceptions(client) == []
    listing = client.get("/api/exceptions").json()["items"]
    assert [item["status"] for item in listing] == [ExceptionStatus.RESOLVED.value]
    assert [item["resolution"] for item in listing] == ["credit issued"]


def test_accepting_a_field_moves_no_exception(client: TestClient) -> None:
    """Accept keeps the extracted value, so there is nothing for it to change."""
    before = open_exceptions(client)
    response = client.post(f"/api/review/items/{MISREAD_AMOUNT_FIELD}/accept")
    assert response.status_code == 200
    assert open_exceptions(client) == before


def test_a_second_correction_reconciles_against_the_newest_value(client: TestClient) -> None:
    """The corrections table is append only, so the latest row is the current value."""
    correct(client, MISREAD_AMOUNT_FIELD, "100.00")
    payload = correct(client, MISREAD_AMOUNT_FIELD, "80.00")
    assert payload["reconciliation"] == {
        "exceptions_removed": 0,
        "exceptions_added": 1,
        "dollars_at_risk_change_cents": 2_000,
    }
