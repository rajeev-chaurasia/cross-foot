"""Reconcile one stored document and replace the exception rows it owns.

The build calls this for every document and the review API calls it for the one
document a correction touched, so there is a single reconciliation called twice
rather than two that can disagree. Everything it reads is a row: the values come
from `fields` with the newest correction applied, and the blocking identity comes
from the `documents` row that ingest wrote it to.

A human decision outranks a re-derivation. An exception that survives the rerun
keeps the status and resolution note a reviewer gave it, which is what stops a
correction elsewhere on the document from quietly reopening closed work.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Sequence
from datetime import date, datetime

from crossfoot.constants import DocType, ExceptionStatus, FieldFamily, FieldName, Oem, ReconMode
from crossfoot.extraction.normalize import parse_amount_to_cents, parse_date
from crossfoot.models.ledger import LedgerBook
from crossfoot.models.reconciliation import ExceptionRecord, ReconciliationDelta
from crossfoot.reconcile.engine import reconcile
from crossfoot.reconcile.statement import FieldValue, StatementIdentity, statement_from_fields

# Stamped on exceptions re-derived after a correction when the document carried
# no exceptions to take a run id from.
REVIEW_RUN_ID = "review"

_IDENTITY_COLUMNS = ("dealer_id", "doc_type", "oem", "period_start", "period_end")

_SELECT_IDENTITY = f"SELECT {', '.join(_IDENTITY_COLUMNS)} FROM documents WHERE doc_id = :doc_id"

_HAS_LINES = "SELECT 1 FROM fields WHERE doc_id = :doc_id AND line_no IS NOT NULL LIMIT 1"

# The newest correction is the field's current value; `fields.value` is never
# rewritten, so the effective reading is a join rather than a column.
_SELECT_FIELDS = """
SELECT f.name AS name, f.family AS family, f.line_no AS line_no,
       f.value AS value, f.value_cents AS value_cents, f.value_date AS value_date,
       (
           SELECT c.new_value FROM corrections c
           WHERE c.field_id = f.field_id
           ORDER BY c.rowid DESC
           LIMIT 1
       ) AS corrected
FROM fields f
WHERE f.doc_id = :doc_id
ORDER BY f.field_id
"""

_SELECT_EXCEPTIONS = "SELECT * FROM exceptions WHERE doc_id = :doc_id"

_DELETE_EXCEPTIONS = "DELETE FROM exceptions WHERE doc_id = :doc_id"

_INSERT_EXCEPTION = """
INSERT INTO exceptions (
    exception_id, run_id, exception_type, doc_id, statement_line_no,
    ledger_entry_id, match_key, statement_amount_cents, ledger_amount_cents,
    dollar_impact_cents, memo_amount_cents, explanation, status, detected_at,
    resolution, resolved_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_SELECT_RUN_ID = "SELECT run_id FROM exceptions WHERE doc_id = :doc_id LIMIT 1"

# What makes two exceptions the same finding across two reconciliations. The
# exception_id cannot: it numbers a document's findings in emission order, so
# clearing the first one renames every one after it.
_Key = tuple[str, int | None, str | None, str | None]


def has_lines(connection: sqlite3.Connection, doc_id: str) -> bool:
    """Whether the extraction found any statement line to match at all."""
    return connection.execute(_HAS_LINES, {"doc_id": doc_id}).fetchone() is not None


def run_id_for(connection: sqlite3.Connection, doc_id: str) -> str:
    """The run this document's exceptions already belong to, so a rerun stays in it."""
    row = connection.execute(_SELECT_RUN_ID, {"doc_id": doc_id}).fetchone()
    return REVIEW_RUN_ID if row is None else str(row["run_id"])


def reconcile_document(
    connection: sqlite3.Connection,
    *,
    doc_id: str,
    book: LedgerBook,
    run_id: str,
    now: datetime,
) -> ReconciliationDelta | None:
    """Rebuild one document's exceptions from its rows, and say how its risk moved.

    None when the document carries no blocking identity, which is the case for a
    file nothing could be extracted from: there is no dealer and no period to
    match against, so there is nothing to reconcile.
    """
    identity = _identity(connection, doc_id)
    if identity is None:
        return None
    statement = statement_from_fields(doc_id, _field_values(connection, doc_id), identity)
    result = reconcile(statement, book, mode=ReconMode.END_TO_END, run_id=run_id, now=now)
    return _replace_exceptions(connection, doc_id, result.exceptions)


def _identity(connection: sqlite3.Connection, doc_id: str) -> StatementIdentity | None:
    row = connection.execute(_SELECT_IDENTITY, {"doc_id": doc_id}).fetchone()
    if row is None or any(row[column] is None for column in _IDENTITY_COLUMNS):
        return None
    return StatementIdentity(
        dealer_id=str(row["dealer_id"]),
        doc_type=DocType(str(row["doc_type"])),
        oem=Oem(str(row["oem"])),
        period_start=date.fromisoformat(str(row["period_start"])),
        period_end=date.fromisoformat(str(row["period_end"])),
    )


def _field_values(connection: sqlite3.Connection, doc_id: str) -> list[FieldValue]:
    rows = connection.execute(_SELECT_FIELDS, {"doc_id": doc_id}).fetchall()
    return [_field_value(row) for row in rows]


def _field_value(row: sqlite3.Row) -> FieldValue:
    """One reading, with the reviewer's value standing in wherever there is one.

    A correction writes text and nothing else, so the typed columns beside it are
    still the model's reading and have to be re-derived from what the human wrote.
    """
    name, line_no = FieldName(row["name"]), row["line_no"]
    corrected = row["corrected"]
    if corrected is None:
        stored_date = row["value_date"]
        return FieldValue(
            name=name,
            line_no=line_no,
            value=row["value"],
            value_cents=row["value_cents"],
            value_date=None if stored_date is None else date.fromisoformat(str(stored_date)),
        )
    family = FieldFamily(row["family"])
    text = str(corrected)
    return FieldValue(
        name=name,
        line_no=line_no,
        value=text,
        value_cents=parse_amount_to_cents(text) if family is FieldFamily.AMOUNT else None,
        value_date=parse_date(text) if family is FieldFamily.DATE else None,
    )


def _replace_exceptions(
    connection: sqlite3.Connection, doc_id: str, records: Sequence[ExceptionRecord]
) -> ReconciliationDelta:
    """Swap the document's exception rows for the fresh ones, keeping decisions."""
    existing = connection.execute(_SELECT_EXCEPTIONS, {"doc_id": doc_id}).fetchall()
    before = {_row_key(row): row for row in existing}
    connection.execute(_DELETE_EXCEPTIONS, {"doc_id": doc_id})
    after: dict[_Key, tuple[ExceptionRecord, sqlite3.Row | None]] = {}
    for record in records:
        key = _record_key(record)
        prior = before.get(key)
        after[key] = (record, prior)
        connection.execute(_INSERT_EXCEPTION, _exception_row(record, prior))
    return ReconciliationDelta(
        exceptions_removed=len(before.keys() - after.keys()),
        exceptions_added=len(after.keys() - before.keys()),
        dollars_at_risk_change_cents=_at_risk(after.values()) - _open_risk(before.values()),
    )


def _at_risk(written: Iterable[tuple[ExceptionRecord, sqlite3.Row | None]]) -> int:
    """Absolute impact of the open exceptions among the rows just written."""
    return sum(
        abs(record.dollar_impact_cents)
        for record, prior in written
        if _status(prior) is ExceptionStatus.OPEN
    )


def _open_risk(rows: Iterable[sqlite3.Row]) -> int:
    return sum(
        abs(int(row["dollar_impact_cents"]))
        for row in rows
        if ExceptionStatus(str(row["status"])) is ExceptionStatus.OPEN
    )


def _status(prior: sqlite3.Row | None) -> ExceptionStatus:
    return ExceptionStatus.OPEN if prior is None else ExceptionStatus(str(prior["status"]))


def _row_key(row: sqlite3.Row) -> _Key:
    return (
        str(row["exception_type"]),
        row["statement_line_no"],
        row["ledger_entry_id"],
        row["match_key"],
    )


def _record_key(record: ExceptionRecord) -> _Key:
    return (
        record.exception_type.value,
        record.statement_line_no,
        record.ledger_entry_id,
        record.match_key,
    )


def _exception_row(record: ExceptionRecord, prior: sqlite3.Row | None) -> tuple[object, ...]:
    return (
        record.exception_id,
        record.run_id,
        record.exception_type.value,
        record.doc_id,
        record.statement_line_no,
        record.ledger_entry_id,
        record.match_key,
        record.statement_amount_cents,
        record.ledger_amount_cents,
        record.dollar_impact_cents,
        record.memo_amount_cents,
        record.explanation,
        _status(prior).value,
        record.detected_at.isoformat(),
        None if prior is None else prior["resolution"],
        None if prior is None else prior["resolved_at"],
    )
