"""Extraction results with per-field confidence, the central pipeline contract."""

from datetime import date

from pydantic import BaseModel, ConfigDict

from crossfoot.constants import (
    CropKind,
    DocType,
    ExtractionRoute,
    FieldFamily,
    FieldName,
    FieldSource,
    IngestErrorKind,
    ReviewStatus,
)


class BBox(BaseModel):
    """Normalized page coordinates in 0..1, origin at the top left."""

    model_config = ConfigDict(frozen=True)

    page: int
    x0: float
    y0: float
    x1: float
    y1: float


class FieldSignals(BaseModel):
    """Raw evidence feeding the confidence model. None means signal unavailable.

    Every member is computed from the artifact and the extraction alone. Nothing
    here may come from the dataset manifest: a confidence score is a claim about
    what the pipeline can tell without an answer key, so a feature only the
    generator could supply inflates that claim by exactly what the generator knew.
    """

    model_config = ConfigDict(frozen=True)

    self_consistency: float | None = None
    det_llm_agreement: float | None = None
    validator_pass: float | None = None
    grammar_match: float | None = None
    crossfoot_ok: float | None = None
    crossfoot_residual_suspect: bool = False
    char_ambiguity: float = 0.0
    # Which extractor the router sent this document to, decided from the file's
    # own bytes. It stands where the generator's quality tier used to: nothing in
    # a real PDF announces that it is a heavy scan, but every document announces
    # whether it carries a text layer, and that is what this says.
    route: ExtractionRoute | None = None


class ExtractedField(BaseModel):
    model_config = ConfigDict(frozen=True)

    field_id: str
    doc_id: str
    line_no: int | None = None  # None for header fields
    name: FieldName
    family: FieldFamily
    raw_text: str | None = None  # verbatim as seen in the source
    value: str | None = None  # canonical string form
    value_cents: int | None = None  # set for amount fields
    value_date: date | None = None  # set for date fields
    source: FieldSource
    bbox: BBox | None = None
    crop_kind: CropKind = CropKind.FULL_PAGE
    signals: FieldSignals
    confidence: float = 0.0
    status: ReviewStatus = ReviewStatus.NEEDS_REVIEW


class IngestError(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: IngestErrorKind
    detail: str


class ExtractedDocument(BaseModel):
    model_config = ConfigDict(frozen=True)

    doc_id: str
    file_path: str
    route: ExtractionRoute
    doc_type: DocType | None = None
    doc_type_confidence: float = 0.0
    header_fields: tuple[ExtractedField, ...] = ()
    line_fields: tuple[ExtractedField, ...] = ()
    crossfoot_delta_cents: int | None = None
    error: IngestError | None = None  # set when route is UNPROCESSABLE
