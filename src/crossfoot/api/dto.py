"""Response and request shapes. The OpenAPI schema they produce is the frontend contract.

Rows come in as `sqlite3.Row` and leave as pydantic models, so the mapping from
column to field lives here and nowhere else. Money stays in integer cents and LLM
cost in microusd, matching `CostCell.list_price_microusd`.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from enum import StrEnum
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict

from crossfoot.api.deps import API_PREFIX, CROP_PATH_TEMPLATE
from crossfoot.constants import (
    CropKind,
    DocType,
    ExtractionRoute,
    FieldFamily,
    FieldName,
    IngestErrorKind,
    QualityTier,
    ReviewStatus,
    SplitName,
)
from crossfoot.extraction.normalize import format_cents, parse_amount_to_cents, parse_date
from crossfoot.models.extraction import FieldSignals
from crossfoot.models.reconciliation import ExceptionRecord
from crossfoot.models.scorecard import CalibrationBin, Scorecard, ThresholdPoint


class Page[ItemT](BaseModel):
    """A listing plus the count its filter matched, which is not the page size."""

    model_config = ConfigDict(frozen=True)

    items: tuple[ItemT, ...]
    total: int


def crop_url(doc_id: str, field_id: str) -> str:
    """Where the pixels behind a value are served from."""
    return API_PREFIX + CROP_PATH_TEMPLATE.format(
        doc_id=quote(doc_id, safe=""), field_id=quote(field_id, safe="")
    )


class ReviewItem(BaseModel):
    """One field in the queue: what was read, how much it is trusted, and the crop."""

    model_config = ConfigDict(frozen=True)

    field_id: str
    doc_id: str
    line_no: int | None
    name: FieldName
    family: FieldFamily
    raw_text: str | None
    # The newest correction when there is one, the model's own reading otherwise.
    value: str | None
    confidence: float
    status: ReviewStatus
    crop_url: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> ReviewItem:
        doc_id, field_id = str(row["doc_id"]), str(row["field_id"])
        return cls(
            field_id=field_id,
            doc_id=doc_id,
            line_no=row["line_no"],
            name=FieldName(row["name"]),
            family=FieldFamily(row["family"]),
            raw_text=row["raw_text"],
            value=row["value"],
            confidence=float(row["confidence"]),
            status=ReviewStatus(row["status"]),
            crop_url=crop_url(doc_id, field_id),
        )


class DocumentSummary(BaseModel):
    """The document a field was read from."""

    model_config = ConfigDict(frozen=True)

    doc_id: str
    file_path: str
    doc_type: DocType | None
    quality_tier: QualityTier
    route: ExtractionRoute
    split: SplitName | None
    error_kind: IngestErrorKind | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> DocumentSummary:
        return cls(
            doc_id=str(row["doc_id"]),
            file_path=str(row["file_path"]),
            doc_type=None if row["doc_type"] is None else DocType(row["doc_type"]),
            quality_tier=QualityTier(row["quality_tier"]),
            route=ExtractionRoute(row["route"]),
            split=None if row["split"] is None else SplitName(row["split"]),
            error_kind=None if row["error_kind"] is None else IngestErrorKind(row["error_kind"]),
        )


class ReviewItemDetail(ReviewItem):
    """The queue item plus everything a reviewer needs to judge it in one screen."""

    # How the value was located on the page, so the crop panel can caption what
    # the reader is looking at instead of leaving them to guess whether a whole
    # page means "here it is" or "we could not find it". This is what the
    # extractor recorded; a stored box that turns out to be degenerate still
    # renders as the full page underneath it.
    crop_kind: CropKind
    signals: FieldSignals
    document: DocumentSummary
    neighbors: tuple[ReviewItem, ...]

    @classmethod
    def build(
        cls, row: sqlite3.Row, *, document: sqlite3.Row, neighbors: list[sqlite3.Row]
    ) -> ReviewItemDetail:
        return cls(
            **ReviewItem.from_row(row).model_dump(),
            crop_kind=CropKind(row["crop_kind"]),
            signals=FieldSignals.model_validate_json(str(row["signals"])),
            document=DocumentSummary.from_row(document),
            neighbors=tuple(ReviewItem.from_row(neighbor) for neighbor in neighbors),
        )


class CropUnavailableReason(StrEnum):
    """Why a field that exists still has no pixels beside it."""

    SOURCE_MISSING = "source_missing"
    SOURCE_UNREADABLE = "source_unreadable"
    SOURCE_UNREACHABLE = "source_unreachable"
    PAGE_MISSING = "page_missing"


class CropUnavailable(BaseModel):
    """The typed answer when a crop cannot be rendered, so the queue shows the value alone.

    A corrupted scan is a fact about the document, not a server fault, and the
    reviewer still has to be able to work the field, so it is this rather than a
    500 or a bare broken image.
    """

    model_config = ConfigDict(frozen=True)

    doc_id: str
    field_id: str
    reason: CropUnavailableReason
    detail: str


class CorrectionRequest(BaseModel):
    """A reviewer's replacement value, validated against the field's family."""

    value: str
    reviewer: str

    def canonical_for(self, family: FieldFamily) -> str | None:
        """The value as the family stores it, or None when the family cannot parse it.

        Parsing is `crossfoot.extraction.normalize`, the same code the pipeline
        uses, so the API and the extractor agree on what a valid amount or date is.
        """
        text = self.value.strip()
        if not text:
            return None
        if family is FieldFamily.AMOUNT:
            cents = parse_amount_to_cents(text)
            return None if cents is None else format_cents(cents)
        if family is FieldFamily.DATE:
            parsed = parse_date(text)
            return None if parsed is None else parsed.isoformat()
        return text


class ExceptionItem(ExceptionRecord):
    """The frozen exception record plus the two columns resolving one writes."""

    resolution: str | None = None
    resolved_at: datetime | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> ExceptionItem:
        return cls.model_validate(dict(row))


class ResolutionRequest(BaseModel):
    """What a reviewer did about an exception, recorded as they close it."""

    resolution: str


class Summary(BaseModel):
    """The tile above the queue. Every number is counted in SQL, never in the UI."""

    model_config = ConfigDict(frozen=True)

    documents_processed: int
    fields_extracted: int
    auto_accept_rate: float
    review_queue_depth: int
    open_exception_count: int
    gross_dollars_at_risk_cents: int
    cost_per_document_microusd: int

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Summary:
        fields_extracted = int(row["fields_extracted"])
        documents_processed = int(row["documents_processed"])
        list_price = int(row["list_price_microusd"])
        return cls(
            documents_processed=documents_processed,
            fields_extracted=fields_extracted,
            auto_accept_rate=(
                int(row["auto_accepted"]) / fields_extracted if fields_extracted else 0.0
            ),
            review_queue_depth=int(row["review_queue_depth"]),
            open_exception_count=int(row["open_exception_count"]),
            gross_dollars_at_risk_cents=int(row["gross_dollars_at_risk_cents"]),
            cost_per_document_microusd=(
                list_price // documents_processed if documents_processed else 0
            ),
        )


class MetricsPayload(BaseModel):
    """The latest committed scorecard, with the two curves the metrics page draws."""

    model_config = ConfigDict(frozen=True)

    scorecard: Scorecard
    calibration: tuple[CalibrationBin, ...]
    threshold_sweep: tuple[ThresholdPoint, ...]

    @classmethod
    def of(
        cls, scorecard: Scorecard, *, applied: tuple[ThresholdPoint, ...] = ()
    ) -> MetricsPayload:
        """The operating point actually applied wins over whatever sweep a scorecard recorded.

        A scorecard says what one run found possible; the applied points say what
        the fields on screen were cut at. When a build has applied none, the
        scorecard's own sweep is still the honest answer.
        """
        return cls(
            scorecard=scorecard,
            calibration=scorecard.calibration,
            threshold_sweep=applied or scorecard.threshold_sweep,
        )
