"""The exceptions dashboard, ranked by the only thing that decides what gets worked first."""

from __future__ import annotations

import sqlite3
from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Query, status

from crossfoot.api.deps import Connection
from crossfoot.api.dto import MAX_PAGE_OFFSET, ExceptionItem, Page, ResolutionRequest
from crossfoot.constants import ExceptionStatus, ExceptionType
from crossfoot.db import exceptions

router = APIRouter(tags=["exceptions"])

# The same page geometry the review queue uses, so the two dashboards page alike.
DEFAULT_EXCEPTION_LIMIT = 50
MAX_EXCEPTION_LIMIT = 500

UNKNOWN_EXCEPTION_DETAIL = "no exception {exception_id}"

ExceptionId = Annotated[str, Path(description="Exception being worked.")]
TypeFilter = Annotated[
    ExceptionType | None, Query(alias="type", description="One of the six exception types.")
]
StatusFilter = Annotated[ExceptionStatus | None, Query(alias="status", description="Open state.")]
MinImpact = Annotated[
    int | None,
    Query(ge=0, description="Floor on absolute dollar impact, the same value the rank uses."),
]
Sort = Annotated[exceptions.ExceptionSort, Query(description="Ranking, always total.")]


@router.get("/exceptions")
def list_exceptions(
    connection: Connection,
    exception_type: TypeFilter = None,
    status_filter: StatusFilter = None,
    min_impact_cents: MinImpact = None,
    sort: Sort = exceptions.ExceptionSort.IMPACT,
    limit: Annotated[int, Query(ge=1, le=MAX_EXCEPTION_LIMIT)] = DEFAULT_EXCEPTION_LIMIT,
    offset: Annotated[int, Query(ge=0, le=MAX_PAGE_OFFSET)] = 0,
) -> Page[ExceptionItem]:
    """One page of exceptions ranked by absolute dollar impact, largest first."""
    rows, total = exceptions.listing(
        connection,
        exception_type=exception_type,
        status=status_filter,
        min_impact_cents=min_impact_cents,
        sort=sort,
        limit=limit,
        offset=offset,
    )
    return Page(items=tuple(ExceptionItem.from_row(row) for row in rows), total=total)


@router.post("/exceptions/{exception_id}/resolve")
def resolve_exception(
    connection: Connection, exception_id: ExceptionId, resolution: ResolutionRequest
) -> ExceptionItem:
    """Close an exception, recording what was done about it. The dollars stay as detected."""
    _require(connection, exception_id)
    exceptions.resolve(connection, exception_id=exception_id, resolution=resolution.resolution)
    return ExceptionItem.from_row(_require(connection, exception_id))


def _require(connection: sqlite3.Connection, exception_id: str) -> sqlite3.Row:
    row = exceptions.one(connection, exception_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=UNKNOWN_EXCEPTION_DETAIL.format(exception_id=exception_id),
        )
    return row
