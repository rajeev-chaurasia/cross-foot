"""The review queue: the least trusted field first, and the two things a human can do to it.

Accepting and correcting are both idempotent in the sense that matters: a second
accept is the same state, and a second correction is another row in a history
that never loses the model's original reading.

Reading one item settles its crop, because the caption this route publishes has
to describe the picture the browser is about to fetch, and only the render knows
what it cut. The render is cached, so the fetch that follows is the same picture
and the page is rasterized once rather than twice.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Query, status

from crossfoot.api import crop_cache
from crossfoot.api.crop_render import CropSourceError
from crossfoot.api.deps import ApiPaths, Connection, Paths
from crossfoot.api.dto import (
    MAX_PAGE_OFFSET,
    CorrectedItem,
    CorrectionRequest,
    CropUnavailableReason,
    Page,
    ReviewItem,
    ReviewItemDetail,
)
from crossfoot.api.ledger import ledger_book
from crossfoot.constants import CropKind, FieldFamily, QualityTier, ReviewStatus
from crossfoot.db import documents, reconciliation, review
from crossfoot.evals.paths import UnsafeDatasetPathError
from crossfoot.models.reconciliation import ReconciliationDelta

router = APIRouter(tags=["review"])

DEFAULT_QUEUE_LIMIT = 50
MAX_QUEUE_LIMIT = 500

UNKNOWN_FIELD_DETAIL = "no field {field_id}"
UNKNOWN_DOCUMENT_DETAIL = "no document {doc_id}"
UNPARSEABLE_DETAIL = "{value!r} is not a valid {family} value"

FieldId = Annotated[str, Path(description="Field the reviewer is looking at.")]
QueueSort = Annotated[review.QueueSort, Query(description="Queue order, always total.")]
StatusFilter = Annotated[
    ReviewStatus | None,
    Query(alias="status", description="Unset means every field, not only the queue."),
]
FamilyFilter = Annotated[FieldFamily | None, Query(description="Field family.")]
TierFilter = Annotated[QualityTier | None, Query(description="Quality tier of the document.")]


@router.get("/review/queue")
def review_queue(
    connection: Connection,
    status_filter: StatusFilter = None,
    family: FamilyFilter = None,
    tier: TierFilter = None,
    sort: QueueSort = review.QueueSort.CONFIDENCE,
    limit: Annotated[int, Query(ge=1, le=MAX_QUEUE_LIMIT)] = DEFAULT_QUEUE_LIMIT,
    offset: Annotated[int, Query(ge=0, le=MAX_PAGE_OFFSET)] = 0,
) -> Page[ReviewItem]:
    """Fields ranked least trusted first, with the count the filter matched."""
    rows, total = review.queue(
        connection,
        status=status_filter,
        family=family,
        tier=tier,
        sort=sort,
        limit=limit,
        offset=offset,
    )
    return Page(items=tuple(ReviewItem.from_row(row) for row in rows), total=total)


@router.get("/review/items/{field_id}")
def review_item(paths: Paths, connection: Connection, field_id: FieldId) -> ReviewItemDetail:
    """One field with its signal breakdown, its document, and the rest of its line."""
    row = _require_field(connection, field_id)
    doc_id = str(row["doc_id"])
    document = documents.one(connection, doc_id)
    if document is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=UNKNOWN_DOCUMENT_DETAIL.format(doc_id=doc_id),
        )
    crop_kind, crop_unavailable_reason = _crop_caption(
        paths, connection, doc_id=doc_id, field_id=field_id
    )
    return ReviewItemDetail.build(
        row,
        document=document,
        neighbors=review.neighbors(connection, row),
        crop_kind=crop_kind,
        crop_unavailable_reason=crop_unavailable_reason,
    )


@router.post("/review/items/{field_id}/accept")
def accept_item(connection: Connection, field_id: FieldId) -> ReviewItem:
    """Keep the extracted value and mark it reviewed."""
    _require_field(connection, field_id)
    review.accept(connection, field_id)
    return ReviewItem.from_row(_require_field(connection, field_id))


@router.post("/review/items/{field_id}/correct")
def correct_item(
    paths: Paths, connection: Connection, field_id: FieldId, correction: CorrectionRequest
) -> CorrectedItem:
    """Replace a value with the reviewer's reading, then re-reconcile its document.

    The loop closes here: the exceptions on the dashboard are re-derived from
    what the human just decided rather than from the reading they overruled.
    """
    row = _require_field(connection, field_id)
    family = FieldFamily(row["family"])
    canonical = correction.canonical_for(family)
    if canonical is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=UNPARSEABLE_DETAIL.format(value=correction.value, family=family.value),
        )
    review.correct(connection, field_id=field_id, new_value=canonical, reviewer=correction.reviewer)
    return CorrectedItem(
        **ReviewItem.from_row(_require_field(connection, field_id)).model_dump(),
        reconciliation=_rereconcile(paths, connection, str(row["doc_id"])),
    )


def _rereconcile(
    paths: ApiPaths, connection: sqlite3.Connection, doc_id: str
) -> ReconciliationDelta | None:
    """Re-derive one document's exceptions, or say why there was nothing to derive.

    One document, never the corpus: the ledger is scanned once for this dealer
    and period and the other 200-odd statements are not touched.
    """
    book = ledger_book(paths.dataset_dir)
    if book is None or not reconciliation.has_lines(connection, doc_id):
        return None
    with connection:
        return reconciliation.reconcile_document(
            connection,
            doc_id=doc_id,
            book=book,
            run_id=reconciliation.run_id_for(connection, doc_id),
            now=datetime.now(UTC),
        )


def _crop_caption(
    paths: ApiPaths, connection: sqlite3.Connection, *, doc_id: str, field_id: str
) -> tuple[CropKind | None, CropUnavailableReason | None]:
    """How the crop panel should caption this field: a kind, or why there is none.

    A field with no picture is still a field a reviewer has to work, so nothing
    here refuses the item; it says what the panel will be showing instead.
    """
    try:
        crop = crop_cache.rendered_crop(paths, connection, doc_id=doc_id, field_id=field_id)
    except UnsafeDatasetPathError:
        # This doc_id came out of the fields table rather than off the wire, so a
        # segment that leaves the crop root is a broken row, not an attack.
        return None, CropUnavailableReason.SOURCE_UNREACHABLE
    except CropSourceError as error:
        return None, error.reason
    return crop.kind, None


def _require_field(connection: sqlite3.Connection, field_id: str) -> sqlite3.Row:
    row = review.item(connection, field_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=UNKNOWN_FIELD_DETAIL.format(field_id=field_id),
        )
    return row
