"""Document reads: the context a crop and a queue item hang off."""

from __future__ import annotations

import sqlite3

from crossfoot.constants import ExtractionRoute, SplitName

_FILTERS = """
    WHERE (:route IS NULL OR route = :route)
      AND (:split IS NULL OR split = :split)
"""

_LISTING = f"SELECT * FROM documents {_FILTERS} ORDER BY doc_id ASC"

_TOTAL = f"SELECT COUNT(*) AS total FROM documents {_FILTERS}"

_ONE = "SELECT * FROM documents WHERE doc_id = :doc_id"


def listing(
    connection: sqlite3.Connection, *, route: ExtractionRoute | None, split: SplitName | None
) -> tuple[list[sqlite3.Row], int]:
    filters = {
        "route": None if route is None else route.value,
        "split": None if split is None else split.value,
    }
    rows = connection.execute(_LISTING, filters).fetchall()
    (total,) = connection.execute(_TOTAL, filters).fetchone()
    return list(rows), int(total)


def one(connection: sqlite3.Connection, doc_id: str) -> sqlite3.Row | None:
    row: sqlite3.Row | None = connection.execute(_ONE, {"doc_id": doc_id}).fetchone()
    return row
