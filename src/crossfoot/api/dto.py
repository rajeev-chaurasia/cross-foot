"""Response and request shapes. The OpenAPI schema they produce is the frontend contract.

Rows come in as `sqlite3.Row` and leave as pydantic models, so the mapping from
column to field lives here and nowhere else. Money stays in integer cents and LLM
cost in microusd, matching `CostCell.list_price_microusd`.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from enum import StrEnum
from typing import Annotated
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, StringConstraints

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
from crossfoot.models.reconciliation import ExceptionRecord, ReconciliationDelta
from crossfoot.models.scorecard import CalibrationBin, Scorecard, ThresholdPoint

# SQLite binds an offset as a 64-bit integer, so a paged route has to refuse one
# it cannot bind; a billion rows is past any listing a reviewer pages to and far
# inside that range.
MAX_PAGE_OFFSET = 1_000_000_000

# A reviewer replaces one cell of a statement, so a correction is a cell's worth
# of text: the longest value the pipeline has read is an order of magnitude under
# this, and a resolution is a note rather than an attachment.
MAX_CORRECTION_VALUE_LENGTH = 256
MAX_REVIEWER_LENGTH = 128
MAX_RESOLUTION_LENGTH = 1_000

CorrectionValue = Annotated[str, StringConstraints(max_length=MAX_CORRECTION_VALUE_LENGTH)]
# The corrections table exists to say who changed what, so an attribution that is
# blank or only spaces is not an attribution. Stripping first makes the two the
# same rejection.
Reviewer = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_REVIEWER_LENGTH)
]
Resolution = Annotated[str, StringConstraints(max_length=MAX_RESOLUTION_LENGTH)]


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


class CropUnavailableReason(StrEnum):
    """Why a field that exists still has no pixels beside it."""

    SOURCE_MISSING = "source_missing"
    SOURCE_UNREADABLE = "source_unreadable"
    SOURCE_UNREACHABLE = "source_unreachable"
    PAGE_MISSING = "page_missing"
    # Not a fault of any kind: a spreadsheet has rows, not pages. Reporting a
    # healthy CSV as unreadable told a reviewer their file was corrupt, so a
    # format that was never going to have pixels says so in its own word.
    NO_PAGE_IMAGE = "no_page_image"


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


class ReviewItemDetail(ReviewItem):
    """The queue item plus everything a reviewer needs to judge it in one screen."""

    # How the value was located on the page, so the crop panel can caption what
    # the reader is looking at instead of leaving them to guess whether a whole
    # page means "here it is" or "we could not find it". This is the render's own
    # answer, never the extractor's guess in fields.crop_kind, because the caption
    # has to describe the picture underneath it. Null when there is no picture,
    # and then crop_unavailable_reason says why. Exactly one of the two is set.
    crop_kind: CropKind | None
    crop_unavailable_reason: CropUnavailableReason | None
    signals: FieldSignals
    document: DocumentSummary
    neighbors: tuple[ReviewItem, ...]

    @classmethod
    def build(
        cls,
        row: sqlite3.Row,
        *,
        document: sqlite3.Row,
        neighbors: list[sqlite3.Row],
        crop_kind: CropKind | None,
        crop_unavailable_reason: CropUnavailableReason | None,
    ) -> ReviewItemDetail:
        return cls(
            **ReviewItem.from_row(row).model_dump(),
            crop_kind=crop_kind,
            crop_unavailable_reason=crop_unavailable_reason,
            signals=FieldSignals.model_validate_json(str(row["signals"])),
            document=DocumentSummary.from_row(document),
            neighbors=tuple(ReviewItem.from_row(neighbor) for neighbor in neighbors),
        )


class CorrectedItem(ReviewItem):
    """The updated item plus what the reviewer's value did to the document's exceptions.

    Null when the document cannot be reconciled: no ledger under the dataset
    directory, or an extraction that found no statement line to match. A
    reconciled document reports the change even when it is three zeroes, because
    "nothing moved" and "nothing could be checked" are different answers.
    """

    model_config = ConfigDict(frozen=True)

    reconciliation: ReconciliationDelta | None


class CorrectionRequest(BaseModel):
    """A reviewer's replacement value, validated against the field's family."""

    value: CorrectionValue
    reviewer: Reviewer

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

    resolution: Resolution


class Summary(BaseModel):
    """The tile above the queue. Every number is counted in SQL, never in the UI.

    Every count here is over the whole database, every split included, and it is
    a reading taken now rather than a result. `review_queue_share` in particular
    falls each time a reviewer accepts a field, which is exactly what makes it a
    different quantity from the review rate a scorecard publishes: that one is
    measured on the held out split at a fixed threshold and does not move. The
    two are never to be printed as though they were the same figure.
    """

    model_config = ConfigDict(frozen=True)

    documents_processed: int
    fields_extracted: int
    auto_accept_rate: float
    review_queue_depth: int
    # The depth as a share of every extracted field, so the queue never divides
    # two counts in the browser to get it.
    review_queue_share: float
    open_exception_count: int
    gross_dollars_at_risk_cents: int
    cost_per_document_microusd: int

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Summary:
        fields_extracted = int(row["fields_extracted"])
        documents_processed = int(row["documents_processed"])
        review_queue_depth = int(row["review_queue_depth"])
        list_price = int(row["list_price_microusd"])
        return cls(
            documents_processed=documents_processed,
            fields_extracted=fields_extracted,
            auto_accept_rate=(
                int(row["auto_accepted"]) / fields_extracted if fields_extracted else 0.0
            ),
            review_queue_depth=review_queue_depth,
            review_queue_share=(review_queue_depth / fields_extracted if fields_extracted else 0.0),
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
    def of(cls, scorecard: Scorecard) -> MetricsPayload:
        """The scorecard's own sweep, whole and in its published order.

        The order is load bearing. A sweep runs one family at a time: the
        calibration curve in ascending threshold order, then one last point
        holding what the reported split reached at the applied threshold, which
        is the only held out number in the section. Nothing here filters,
        reorders, or substitutes for it.

        The `applied_thresholds` table is deliberately not published in its
        place. Those rows are the point chosen on the calibration split, so their
        precision and review rate are calibration figures by construction;
        serving them under a scorecard whose split is `test` would put a
        calibration number under a test heading and drop the held out result
        entirely.
        """
        return cls(
            scorecard=scorecard,
            calibration=scorecard.calibration,
            threshold_sweep=scorecard.threshold_sweep,
        )
