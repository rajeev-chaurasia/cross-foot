"""Exception dashboard reads and the resolve writer.

Ranking and filtering both use absolute dollar impact, so a 600.00 credit the
dealer never received outranks a 120.00 overcharge and clears the same floor.
Signed impact stays in the row, because the direction is what a reviewer acts on.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from enum import StrEnum

from crossfoot.constants import ExceptionStatus, ExceptionType


class ExceptionSort(StrEnum):
    """The orders the dashboard can be asked for. Each one is total."""

    IMPACT = "impact"


_ORDER_BY: dict[ExceptionSort, str] = {
    ExceptionSort.IMPACT: "ABS(dollar_impact_cents) DESC, exception_id ASC"
}

# min_impact_cents compares the same absolute impact the ranking sorts by, so a
# floor of zero keeps a timing difference that carries no dollars.
_FILTERS = """
    WHERE (:exception_type IS NULL OR exception_type = :exception_type)
      AND (:status IS NULL OR status = :status)
      AND (:min_impact_cents IS NULL OR ABS(dollar_impact_cents) >= :min_impact_cents)
"""

_LISTING = f"SELECT * FROM exceptions {_FILTERS} ORDER BY {{order_by}} LIMIT :limit OFFSET :offset"

_TOTAL = f"SELECT COUNT(*) AS total FROM exceptions {_FILTERS}"

_ONE = "SELECT * FROM exceptions WHERE exception_id = :exception_id"

_RESOLVE = """
UPDATE exceptions
SET status = :status, resolution = :resolution, resolved_at = :resolved_at
WHERE exception_id = :exception_id
"""

# The durable half of the same decision. The exceptions row is rebuilt from
# scratch every time its document is reconciled again, so a note kept only there
# is lost the moment the finding clears; this survives, keyed by the finding's
# own id, and carries the amounts the reviewer decided about.
_RECORD_RESOLUTION = """
INSERT INTO exception_resolutions (
    exception_id, resolution, resolved_at,
    dollar_impact_cents, statement_amount_cents, ledger_amount_cents
)
SELECT exception_id, :resolution, :resolved_at,
       dollar_impact_cents, statement_amount_cents, ledger_amount_cents
FROM exceptions WHERE exception_id = :exception_id
ON CONFLICT(exception_id) DO UPDATE SET
    resolution = excluded.resolution,
    resolved_at = excluded.resolved_at,
    dollar_impact_cents = excluded.dollar_impact_cents,
    statement_amount_cents = excluded.statement_amount_cents,
    ledger_amount_cents = excluded.ledger_amount_cents
"""


def listing(
    connection: sqlite3.Connection,
    *,
    exception_type: ExceptionType | None,
    status: ExceptionStatus | None,
    min_impact_cents: int | None,
    sort: ExceptionSort,
    limit: int,
    offset: int,
) -> tuple[list[sqlite3.Row], int]:
    """One page of the ranked listing, and the count the filter matched.

    The count is the whole filter, never the page, so a dashboard can say what it
    is a page of. The ranking is total, so walking the offsets visits every row
    exactly once.
    """
    filters = {
        "exception_type": None if exception_type is None else exception_type.value,
        "status": None if status is None else status.value,
        "min_impact_cents": min_impact_cents,
    }
    rows = connection.execute(
        _LISTING.format(order_by=_ORDER_BY[sort]), filters | {"limit": limit, "offset": offset}
    ).fetchall()
    (total,) = connection.execute(_TOTAL, filters).fetchone()
    return list(rows), int(total)


def one(connection: sqlite3.Connection, exception_id: str) -> sqlite3.Row | None:
    row: sqlite3.Row | None = connection.execute(_ONE, {"exception_id": exception_id}).fetchone()
    return row


def resolve(connection: sqlite3.Connection, *, exception_id: str, resolution: str) -> None:
    """Close an exception. Idempotent: resolving twice records the later note."""
    decided = {
        "resolution": resolution,
        "resolved_at": datetime.now(UTC).isoformat(),
        "exception_id": exception_id,
    }
    with connection:
        connection.execute(_RECORD_RESOLUTION, decided)
        connection.execute(_RESOLVE, decided | {"status": ExceptionStatus.RESOLVED.value})
