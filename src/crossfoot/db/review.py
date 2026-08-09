"""Review queue reads, plus the append only writer behind a correction.

The queue order is a total order, ascending confidence then field_id, and it is
ordered in SQL so paging is consistent: two pages of the same data can never
disagree about what row 4 is.

A correction never rewrites the fields row. The value a reader sees is the
newest correction if there is one and the model's own reading otherwise, which
is what keeps the original extraction recoverable as an audit trail and as an
eval label later.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from enum import StrEnum

from ulid import ULID

from crossfoot.constants import FieldFamily, QualityTier, ReviewStatus


class QueueSort(StrEnum):
    """The orders the queue can be asked for. Each one is total."""

    CONFIDENCE = "confidence"


# Least trusted first, then field_id so a tie has exactly one answer.
_ORDER_BY: dict[QueueSort, str] = {QueueSort.CONFIDENCE: "f.confidence ASC, f.field_id ASC"}

_EFFECTIVE_VALUE = """COALESCE(
    (
        SELECT c.new_value FROM corrections c
        WHERE c.field_id = {alias}.field_id
        ORDER BY c.rowid DESC
        LIMIT 1
    ),
    {alias}.value
)"""

_ITEM_COLUMNS = f"""
    f.field_id AS field_id,
    f.doc_id AS doc_id,
    f.line_no AS line_no,
    f.name AS name,
    f.family AS family,
    f.raw_text AS raw_text,
    {_EFFECTIVE_VALUE.format(alias="f")} AS value,
    f.confidence AS confidence,
    f.status AS status,
    f.signals AS signals
"""

# An unset filter means no filter, so the unfiltered queue is every field.
_FILTERS = """
    WHERE (:status IS NULL OR f.status = :status)
      AND (:family IS NULL OR f.family = :family)
      AND (:tier IS NULL OR d.quality_tier = :tier)
"""

_FROM = "FROM fields f JOIN documents d ON d.doc_id = f.doc_id"

_QUEUE = (
    f"SELECT {_ITEM_COLUMNS} {_FROM} {_FILTERS} ORDER BY {{order_by}} LIMIT :limit OFFSET :offset"
)

_QUEUE_TOTAL = f"SELECT COUNT(*) AS total {_FROM} {_FILTERS}"

_ITEM = f"SELECT {_ITEM_COLUMNS} FROM fields f WHERE f.field_id = :field_id"

# The other fields printed on the same line of the same document. A line number
# belongs to its document, so doc_id is part of the key.
_NEIGHBORS = f"""
SELECT {_ITEM_COLUMNS}
FROM fields f
WHERE f.doc_id = :doc_id AND f.line_no = :line_no AND f.field_id <> :field_id
ORDER BY f.field_id ASC
"""

_SET_STATUS = "UPDATE fields SET status = :status WHERE field_id = :field_id"

_APPEND_CORRECTION = f"""
INSERT INTO corrections (correction_id, field_id, old_value, new_value, reviewer, created_at)
VALUES (
    :correction_id,
    :field_id,
    (SELECT {_EFFECTIVE_VALUE.format(alias="f")} FROM fields f WHERE f.field_id = :field_id),
    :new_value,
    :reviewer,
    :created_at
)
"""


def queue(
    connection: sqlite3.Connection,
    *,
    status: ReviewStatus | None,
    family: FieldFamily | None,
    tier: QualityTier | None,
    sort: QueueSort,
    limit: int,
    offset: int,
) -> tuple[list[sqlite3.Row], int]:
    """One page of the queue and the count the whole filter matches."""
    filters = {
        "status": None if status is None else status.value,
        "family": None if family is None else family.value,
        "tier": None if tier is None else tier.value,
    }
    page = connection.execute(
        _QUEUE.format(order_by=_ORDER_BY[sort]), {**filters, "limit": limit, "offset": offset}
    ).fetchall()
    (total,) = connection.execute(_QUEUE_TOTAL, filters).fetchone()
    return list(page), int(total)


def item(connection: sqlite3.Connection, field_id: str) -> sqlite3.Row | None:
    row: sqlite3.Row | None = connection.execute(_ITEM, {"field_id": field_id}).fetchone()
    return row


def neighbors(connection: sqlite3.Connection, row: sqlite3.Row) -> list[sqlite3.Row]:
    """Fields sharing the line, or nothing at all for a header field."""
    if row["line_no"] is None:
        return []
    return list(
        connection.execute(
            _NEIGHBORS,
            {
                "doc_id": str(row["doc_id"]),
                "line_no": int(row["line_no"]),
                "field_id": str(row["field_id"]),
            },
        ).fetchall()
    )


def accept(connection: sqlite3.Connection, field_id: str) -> None:
    """Move a field to HUMAN_ACCEPTED. Idempotent: the same call twice is one state."""
    with connection:
        connection.execute(
            _SET_STATUS, {"status": ReviewStatus.HUMAN_ACCEPTED.value, "field_id": field_id}
        )


def correct(
    connection: sqlite3.Connection, *, field_id: str, new_value: str, reviewer: str
) -> None:
    """Append a correction and move the field to HUMAN_CORRECTED, in one transaction.

    `old_value` is read inside the transaction rather than passed in, so it is
    always the value this correction actually replaced.
    """
    with connection:
        connection.execute(
            _APPEND_CORRECTION,
            {
                "correction_id": str(ULID()),
                "field_id": field_id,
                "new_value": new_value,
                "reviewer": reviewer,
                "created_at": datetime.now(UTC).isoformat(),
            },
        )
        connection.execute(
            _SET_STATUS, {"status": ReviewStatus.HUMAN_CORRECTED.value, "field_id": field_id}
        )
