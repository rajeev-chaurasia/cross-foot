"""The one row a crop needs: which file a value was read from, and where on it.

The crop route asks for both ids, so the lookup keys on both. A pair that names
no field is the only 404 the route has; a field the database holds always has a
document behind it, and therefore always has pixels.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from crossfoot.constants import CropKind
from crossfoot.models.extraction import BBox

# The page a field cites, for a field that cites none. A full_page crop stores
# no page number, and the first page is the only defensible guess.
DEFAULT_PAGE_INDEX = 0

_SOURCE = """
SELECT f.crop_kind AS crop_kind,
       f.page AS page,
       f.x0 AS x0,
       f.y0 AS y0,
       f.x1 AS x1,
       f.y1 AS y1,
       d.file_path AS file_path
FROM fields f JOIN documents d ON d.doc_id = f.doc_id
WHERE f.doc_id = :doc_id AND f.field_id = :field_id
"""


@dataclass(frozen=True, slots=True)
class CropSource:
    """Where a field's pixels are: the source file, the page, and the box if it has one."""

    file_path: str
    page: int
    bbox: BBox | None


def source(connection: sqlite3.Connection, *, doc_id: str, field_id: str) -> CropSource | None:
    """The crop source for one field, or None when the pair names no field."""
    row: sqlite3.Row | None = connection.execute(
        _SOURCE, {"doc_id": doc_id, "field_id": field_id}
    ).fetchone()
    if row is None:
        return None
    page = row["page"]
    return CropSource(
        file_path=str(row["file_path"]),
        page=DEFAULT_PAGE_INDEX if page is None else int(page),
        bbox=_bbox(row),
    )


def _bbox(row: sqlite3.Row) -> BBox | None:
    """The stored box, or None when the field claims no usable coordinates.

    A row_band or full_page field has no box by definition, and an exact_bbox
    row that lost a coordinate is treated the same way rather than trusted.
    """
    corners = (row["page"], row["x0"], row["y0"], row["x1"], row["y1"])
    if row["crop_kind"] != CropKind.EXACT_BBOX or any(value is None for value in corners):
        return None
    page, x0, y0, x1, y1 = corners
    return BBox(page=int(page), x0=float(x0), y0=float(y0), x1=float(x1), y1=float(y1))
