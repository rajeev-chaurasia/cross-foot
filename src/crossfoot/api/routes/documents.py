"""Document listings: what was ingested, how it routed, and which split it belongs to."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Query, status

from crossfoot.api.deps import Connection
from crossfoot.api.dto import DocumentSummary, Page
from crossfoot.constants import ExtractionRoute, SplitName
from crossfoot.db import documents

router = APIRouter(tags=["documents"])

UNKNOWN_DOCUMENT_DETAIL = "no document {doc_id}"

DocId = Annotated[str, Path(description="Document identifier from the dataset manifest.")]
RouteFilter = Annotated[ExtractionRoute | None, Query(description="How the file was routed.")]
SplitFilter = Annotated[SplitName | None, Query(description="train, calibration, or test.")]


@router.get("/documents")
def list_documents(
    connection: Connection, route: RouteFilter = None, split: SplitFilter = None
) -> Page[DocumentSummary]:
    """Every ingested document matching the filter, in doc_id order."""
    rows, total = documents.listing(connection, route=route, split=split)
    return Page(items=tuple(DocumentSummary.from_row(row) for row in rows), total=total)


@router.get("/documents/{doc_id}")
def get_document(connection: Connection, doc_id: DocId) -> DocumentSummary:
    """One document, including the error kind when nothing could be extracted from it."""
    row = documents.one(connection, doc_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=UNKNOWN_DOCUMENT_DETAIL.format(doc_id=doc_id),
        )
    return DocumentSummary.from_row(row)
