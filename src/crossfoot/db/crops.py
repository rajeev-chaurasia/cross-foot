"""The one row a crop needs: which file a value was read from, and where on it.

The crop route asks for both ids, so the lookup keys on both. A pair that names
no field is the only 404 the route has; a field the database holds always has a
document behind it, and therefore always has pixels.

A vision field carries no coordinates, only the row it was read from, so it also
carries what is needed to check that anchor: how many rows the model reported for
the document. The renderer refuses a band unless it finds exactly that many.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from crossfoot.constants import CropKind, FieldSource
from crossfoot.models.extraction import BBox

# The page a field cites, for a field that cites none. A full_page crop stores
# no page number, and the first page is the only defensible guess.
DEFAULT_PAGE_INDEX = 0

_SOURCE = """
SELECT f.crop_kind AS crop_kind,
       f.source AS source,
       f.line_no AS line_no,
       f.page AS page,
       f.x0 AS x0,
       f.y0 AS y0,
       f.x1 AS x1,
       f.y1 AS y1,
       d.file_path AS file_path
FROM fields f JOIN documents d ON d.doc_id = f.doc_id
WHERE f.doc_id = :doc_id AND f.field_id = :field_id
"""

# How many rows the model reported for this document, and the last index it used.
# The two disagree when the model skipped or repeated a row, and then its
# row_position is not an index into anything the page can be asked about.
_REPORTED_ROWS = """
SELECT COUNT(DISTINCT f.line_no) AS rows, MAX(f.line_no) AS last
FROM fields f
WHERE f.doc_id = :doc_id AND f.source = :source AND f.line_no IS NOT NULL
"""


@dataclass(frozen=True, slots=True)
class CropSource:
    """Where a field's pixels are: the file, the page, and how to find the value.

    `bbox` is evidence and only an exact_bbox field has one. `row_position` and
    `expected_rows` locate a row band. `hint` is the model's own box, which
    refines a band it agrees with and is discarded otherwise.
    """

    file_path: str
    page: int
    bbox: BBox | None
    row_position: int | None = None
    expected_rows: int | None = None
    hint: BBox | None = None


def source(connection: sqlite3.Connection, *, doc_id: str, field_id: str) -> CropSource | None:
    """The crop source for one field, or None when the pair names no field."""
    row: sqlite3.Row | None = connection.execute(
        _SOURCE, {"doc_id": doc_id, "field_id": field_id}
    ).fetchone()
    if row is None:
        return None
    page = row["page"]
    vision_line = row["source"] == FieldSource.LLM_VISION and row["line_no"] is not None
    return CropSource(
        file_path=str(row["file_path"]),
        page=DEFAULT_PAGE_INDEX if page is None else int(page),
        bbox=_bbox(row),
        row_position=int(row["line_no"]) if vision_line else None,
        expected_rows=_expected_rows(connection, doc_id) if vision_line else None,
        # The same stored corners the exact path refuses, offered as a hint only.
        hint=_corners(row) if vision_line else None,
    )


def record_kind(
    connection: sqlite3.Connection, *, doc_id: str, field_id: str, kind: CropKind
) -> None:
    """Record how the served crop was actually found, so the queue can caption it.

    The extractor records what it could see without the page in front of it. The
    renderer is the only place that knows whether the row was located, so it is
    the place that gets to say so.
    """
    with connection:
        connection.execute(
            "UPDATE fields SET crop_kind = :kind WHERE doc_id = :doc_id AND field_id = :field_id",
            {"kind": kind.value, "doc_id": doc_id, "field_id": field_id},
        )


def _expected_rows(connection: sqlite3.Connection, doc_id: str) -> int | None:
    """The model's own row count, when its row numbering is a usable index."""
    row: sqlite3.Row | None = connection.execute(
        _REPORTED_ROWS, {"doc_id": doc_id, "source": FieldSource.LLM_VISION.value}
    ).fetchone()
    if row is None or row["rows"] is None or row["last"] is None:
        return None
    rows, last = int(row["rows"]), int(row["last"])
    return rows if rows == last else None


def _bbox(row: sqlite3.Row) -> BBox | None:
    """The stored box, or None when the field claims no usable coordinates.

    A row_band or full_page field has no box by definition, and an exact_bbox
    row that lost a coordinate is treated the same way rather than trusted.
    """
    if row["crop_kind"] != CropKind.EXACT_BBOX:
        return None
    return _corners(row)


def _corners(row: sqlite3.Row) -> BBox | None:
    """The four stored coordinates, or None when any of them is missing."""
    corners = (row["page"], row["x0"], row["y0"], row["x1"], row["y1"])
    if any(value is None for value in corners):
        return None
    page, x0, y0, x1, y1 = corners
    return BBox(page=int(page), x0=float(x0), y0=float(y0), x1=float(x1), y1=float(y1))
