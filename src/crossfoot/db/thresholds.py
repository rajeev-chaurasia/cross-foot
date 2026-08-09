"""The operating point a build applied, stored beside the rows it decided.

A scorecard's `threshold_sweep` is a sweep: it says what one run found possible.
This table says what was used. Keeping the two apart is what lets the metrics
page report the threshold the fields table was actually cut at rather than
recomputing a point at read time and assuming it lands in the same place.

One build owns the whole table: writing replaces it, so a family that stops
earning a threshold stops having one rather than keeping a stale row.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime

from crossfoot.constants import FieldFamily, SplitName
from crossfoot.models.scorecard import ThresholdPoint

_CLEAR = "DELETE FROM applied_thresholds"

_INSERT = """
INSERT OR REPLACE INTO applied_thresholds (
    field_family, threshold, auto_accept_precision, review_rate,
    fit_split, threshold_split, run_id, applied_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
"""

_SELECT = """
SELECT field_family, threshold, auto_accept_precision, review_rate
FROM applied_thresholds
"""

# Read back in the order the enum declares families, which is the order the
# sweep is chosen in, so no reader sees two orderings of one run.
_FAMILY_ORDER = {family: index for index, family in enumerate(FieldFamily)}


def replace(
    connection: sqlite3.Connection,
    points: Sequence[ThresholdPoint],
    *,
    run_id: str,
    fit_split: SplitName,
    threshold_split: SplitName,
    applied_at: datetime | None = None,
) -> None:
    """Swap in the points this build applied. The splits are recorded, not assumed."""
    stamp = (applied_at or datetime.now(UTC)).isoformat()
    connection.execute(_CLEAR)
    for point in points:
        connection.execute(
            _INSERT,
            (
                point.field_family.value,
                point.threshold,
                point.auto_accept_precision,
                point.review_rate,
                fit_split.value,
                threshold_split.value,
                run_id,
                stamp,
            ),
        )


def applied(connection: sqlite3.Connection) -> tuple[ThresholdPoint, ...]:
    """The operating point in force, empty when no build has applied one yet."""
    points = [
        ThresholdPoint(
            field_family=FieldFamily(row["field_family"]),
            threshold=float(row["threshold"]),
            auto_accept_precision=float(row["auto_accept_precision"]),
            review_rate=float(row["review_rate"]),
        )
        for row in connection.execute(_SELECT).fetchall()
    ]
    return tuple(sorted(points, key=lambda point: _FAMILY_ORDER[point.field_family]))
