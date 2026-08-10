"""Reconcile one stored document and replace the exception rows it owns.

The build calls this for every document and the review API calls it for the one
document a correction touched, so there is a single reconciliation called twice
rather than two that can disagree. Everything it reads is a row: the values come
from `fields` with the newest correction applied, and the blocking identity comes
from the `documents` row that ingest wrote it to.

A human decision outranks a re-derivation of the same facts, and only those. A
finding that comes back carrying the money it was closed about keeps the note a
reviewer gave it, which is what stops a correction elsewhere on the document from
quietly reopening closed work. A finding whose amounts moved is open again: the
decision was made about a number that no longer holds.

The read and the write are one decision, so the write lock is taken before the
first read rather than at the first write. Two corrections landing on one
document at once would otherwise each read the state the other is about to
change, and each report the whole move as its own.
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
    ledger_entry_id, statement_amount_cents, ledger_amount_cents,
    dollar_impact_cents, memo_amount_cents, explanation, status, detected_at,
    resolution, resolved_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

# Keyed by exception_id, which is the finding's identity, so a decision is found
# again even when the finding cleared and a later correction brought it back.
_SELECT_RESOLUTIONS = "SELECT * FROM exception_resolutions WHERE exception_id IN ({placeholders})"

_SELECT_RUN_ID = "SELECT run_id FROM exceptions WHERE doc_id = :doc_id LIMIT 1"

_BEGIN_WRITE = "BEGIN IMMEDIATE"


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

    None when the document carries no blocking identity it can act on, which is
    the case for a file nothing could be extracted from: there is no dealer and no
    period to match against, so there is nothing to reconcile. A document that
    yields no statement line is still reconciled, because the ledger entries its
    period expected are findings of their own.
    """
    _begin_write(connection)
    identity = _identity(connection, doc_id)
    if identity is None:
        return None
    statement = statement_from_fields(doc_id, _field_values(connection, doc_id), identity)
    result = reconcile(statement, book, mode=ReconMode.END_TO_END, run_id=run_id, now=now)
    return _replace_exceptions(connection, doc_id, result.exceptions)


def _begin_write(connection: sqlite3.Connection) -> None:
    """Take the write lock before the first read, not at the first write.

    Everything here reads the document's current state and then replaces what was
    derived from it, so a reader that is about to write has to hold the lock the
    whole way. A caller already inside a transaction holds it for its own reasons,
    so this opens one only when there is none.
    """
    if not connection.in_transaction:
        connection.execute(_BEGIN_WRITE)


def _identity(connection: sqlite3.Connection, doc_id: str) -> StatementIdentity | None:
    row = connection.execute(_SELECT_IDENTITY, {"doc_id": doc_id}).fetchone()
    if row is None or any(row[column] is None for column in _IDENTITY_COLUMNS):
        return None
    try:
        return StatementIdentity(
            dealer_id=str(row["dealer_id"]),
            doc_type=DocType(str(row["doc_type"])),
            oem=Oem(str(row["oem"])),
            period_start=date.fromisoformat(str(row["period_start"])),
            period_end=date.fromisoformat(str(row["period_end"])),
        )
    except ValueError:
        # An identity nothing can read is an identity the reconciler does not
        # have, the same as a missing one. This runs after the correction that
        # triggered it has committed, so raising would lose the delta and answer
        # a landed write with a 500.
        return None


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
    """Swap the document's exception rows for the fresh ones, keeping decisions.

    Every number in the delta describes one population, the open findings. A
    resolved finding is not risk, so its arrival or departure is not a change in
    risk either, and counting it beside a dollar figure that ignores it would put
    two different quantities in one panel.
    """
    existing = connection.execute(_SELECT_EXCEPTIONS, {"doc_id": doc_id}).fetchall()
    before = {str(row["exception_id"]): row for row in existing}
    decisions = _decisions(connection, records)
    connection.execute(_DELETE_EXCEPTIONS, {"doc_id": doc_id})
    after: dict[str, tuple[ExceptionRecord, sqlite3.Row | None]] = {}
    for record in records:
        decision = decisions.get(record.exception_id)
        after[record.exception_id] = (record, decision)
        connection.execute(_INSERT_EXCEPTION, _exception_row(record, decision))
    open_before = {key for key, row in before.items() if _row_status(row) is ExceptionStatus.OPEN}
    open_after = {key for key, (_, decision) in after.items() if decision is None}
    return ReconciliationDelta(
        exceptions_removed=len(open_before - open_after),
        exceptions_added=len(open_after - open_before),
        dollars_at_risk_change_cents=_at_risk(after.values()) - _open_risk(before.values()),
    )


def _decisions(
    connection: sqlite3.Connection, records: Sequence[ExceptionRecord]
) -> dict[str, sqlite3.Row]:
    """The standing resolution behind each fresh finding, where it still applies.

    A reviewer closes a finding on the facts in front of them, so a re-derivation
    that moves those facts is not the thing they decided about and the finding is
    open again.
    """
    if not records:
        return {}
    placeholders = ", ".join("?" * len(records))
    rows = connection.execute(
        _SELECT_RESOLUTIONS.format(placeholders=placeholders),
        [record.exception_id for record in records],
    ).fetchall()
    standing = {str(row["exception_id"]): row for row in rows}
    return {
        record.exception_id: decision
        for record in records
        if (decision := standing.get(record.exception_id)) is not None
        and _same_money(record, decision)
    }


def _same_money(record: ExceptionRecord, decision: sqlite3.Row) -> bool:
    """Whether the finding still carries the money its resolution was written about."""
    return bool(
        decision["dollar_impact_cents"] == record.dollar_impact_cents
        and decision["statement_amount_cents"] == record.statement_amount_cents
        and decision["ledger_amount_cents"] == record.ledger_amount_cents
    )


def _at_risk(written: Iterable[tuple[ExceptionRecord, sqlite3.Row | None]]) -> int:
    """Absolute impact of the open exceptions among the rows just written."""
    return sum(
        abs(record.dollar_impact_cents)
        for record, decision in written
        if _decided_status(decision) is ExceptionStatus.OPEN
    )


def _open_risk(rows: Iterable[sqlite3.Row]) -> int:
    return sum(
        abs(int(row["dollar_impact_cents"]))
        for row in rows
        if _row_status(row) is ExceptionStatus.OPEN
    )


def _row_status(row: sqlite3.Row) -> ExceptionStatus:
    return ExceptionStatus(str(row["status"]))


def _decided_status(decision: sqlite3.Row | None) -> ExceptionStatus:
    """The status a standing decision implies, as against the one a row records."""
    return ExceptionStatus.OPEN if decision is None else ExceptionStatus.RESOLVED


def _exception_row(record: ExceptionRecord, decision: sqlite3.Row | None) -> tuple[object, ...]:
    return (
        record.exception_id,
        record.run_id,
        record.exception_type.value,
        record.doc_id,
        record.statement_line_no,
        record.ledger_entry_id,
        record.statement_amount_cents,
        record.ledger_amount_cents,
        record.dollar_impact_cents,
        record.memo_amount_cents,
        record.explanation,
        _decided_status(decision).value,
        record.detected_at.isoformat(),
        None if decision is None else decision["resolution"],
        None if decision is None else decision["resolved_at"],
    )
