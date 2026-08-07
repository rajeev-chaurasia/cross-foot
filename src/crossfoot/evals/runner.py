"""Eval runner: route every document by its bytes, score what came back.

Routing is the extractor's own decision, made from the file signature rather
than from the manifest, so a mislabelled artifact is caught the same way it
would be in production. No document can stop a run: an unreadable file becomes
an UNPROCESSABLE result with a typed error and the loop continues.
"""

import logging
import subprocess
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from crossfoot.confidence.signals import SignalContext
from crossfoot.constants import (
    ExtractionRoute,
    FieldName,
    IngestErrorKind,
    LineType,
    Provider,
    SplitName,
)
from crossfoot.evals.metrics import score_fields
from crossfoot.evals.paths import UnsafeDatasetPathError, resolve_dataset_path
from crossfoot.extraction.pdf_text import extract_pdf
from crossfoot.extraction.router import route_file
from crossfoot.extraction.tabular import extract_csv
from crossfoot.models.extraction import ExtractedDocument, ExtractedField, IngestError
from crossfoot.models.ledger import LedgerBook
from crossfoot.models.manifest import DatasetManifest, ManifestRecord
from crossfoot.models.scorecard import Scorecard
from crossfoot.models.statement import StatementDoc, StatementLine

_LOGGER = logging.getLogger(__name__)

MANIFEST_FILENAME = "manifest.json"
LEDGER_FILENAME = "ledger.json"

_GIT_SHA_FALLBACK = "unknown"
_GIT_TIMEOUT_SECONDS = 10

Extractor = Callable[[Path, str], ExtractedDocument]

# Routes this offline runner can serve. The scanned tier needs a live vision
# client, so `crossfoot extract` drives it and the notes record its absence.
ROUTE_EXTRACTORS: dict[ExtractionRoute, Extractor] = {
    ExtractionRoute.CSV: extract_csv,
    ExtractionRoute.DIGITAL_PDF: extract_pdf,
}


@dataclass(frozen=True, slots=True)
class VisionDegradations:
    """Quality the vision path lost, counted so a run publishes it.

    Zero for the deterministic routes this offline runner serves; `crossfoot
    extract` fills these in from the vision extractor's counters.
    """

    structured_output_failures: int = 0
    consistency_degradations: int = 0
    provider_failures: int = 0
    # Providers whose allowance ran out mid run. Named rather than counted: the
    # fix is a key or a wait, and a reader cannot act on a number.
    quota_exhausted: tuple[Provider, ...] = ()

    def notes(self) -> str:
        """A sentence naming every nonzero counter, empty when nothing degraded."""
        parts: list[str] = []
        if self.structured_output_failures:
            parts.append(f"{self.structured_output_failures} failed structured output twice")
        if self.consistency_degradations:
            parts.append(
                f"{self.consistency_degradations} lost the consistency sample"
                " and carry no self_consistency signal"
            )
        if self.provider_failures:
            parts.append(f"{self.provider_failures} failed on every provider")
        if self.quota_exhausted:
            named = ", ".join(provider.value for provider in self.quota_exhausted)
            parts.append(f"quota exhausted on {named}")
        return f" Vision degradations: {'; '.join(parts)}." if parts else ""


@dataclass(frozen=True, slots=True)
class ExtractionRun:
    """Everything one pass over a split produced, including what it could not."""

    documents: tuple[ExtractedDocument, ...]
    unprocessable: tuple[ExtractedDocument, ...]
    unserved: Counter[ExtractionRoute]
    degradations: VisionDegradations = VisionDegradations()

    def total(self) -> int:
        return len(self.documents) + len(self.unprocessable) + sum(self.unserved.values())


def load_manifest(dataset_dir: Path) -> DatasetManifest:
    return DatasetManifest.model_validate_json((dataset_dir / MANIFEST_FILENAME).read_bytes())


def load_ledger(dataset_dir: Path) -> LedgerBook:
    return LedgerBook.model_validate_json((dataset_dir / LEDGER_FILENAME).read_bytes())


def split_records(manifest: DatasetManifest, split: SplitName) -> tuple[ManifestRecord, ...]:
    return tuple(record for record in manifest.records if record.split is split)


def extract_split(dataset_dir: Path, manifest: DatasetManifest, split: SplitName) -> ExtractionRun:
    """Route and extract every document in the split with the offline extractors."""
    documents: list[ExtractedDocument] = []
    unprocessable: list[ExtractedDocument] = []
    unserved: Counter[ExtractionRoute] = Counter()
    for record in split_records(manifest, split):
        try:
            path = resolve_dataset_path(dataset_dir, record.file_path)
        except UnsafeDatasetPathError as error:
            _LOGGER.warning("skipping %s: %s", record.doc_id, error)
            unprocessable.append(
                _failed(record, IngestErrorKind.UNRECOGNIZED, f"unsafe manifest path: {error}")
            )
            continue
        routing = route_file(path)
        extractor = ROUTE_EXTRACTORS.get(routing.route)
        if routing.error is not None:
            unprocessable.append(_failed(record, routing.error.kind, routing.error.detail))
            continue
        if extractor is None:
            unserved[routing.route] += 1
            continue
        doc = _guarded(extractor, path, record.doc_id)
        if doc.route is ExtractionRoute.UNPROCESSABLE:
            unprocessable.append(doc)
        else:
            documents.append(doc)
    return ExtractionRun(
        documents=tuple(documents), unprocessable=tuple(unprocessable), unserved=unserved
    )


def run_eval(dataset_dir: Path, split: SplitName) -> Scorecard:
    """Extract the split with every offline route, score it, write a scorecard."""
    manifest = load_manifest(dataset_dir)
    # The ledger is a legitimate pipeline input; validate it is present and well formed.
    load_ledger(dataset_dir)
    run = extract_split(dataset_dir, manifest, split)
    now = datetime.now(UTC)
    git_sha = git_short_sha()
    return Scorecard(
        run_id=f"{now:%Y%m%dT%H%M%S}-{git_sha}",
        created_at=now,
        git_sha=git_sha,
        dataset_config_hash=manifest.config_hash,
        master_seed=manifest.master_seed,
        split=split,
        models_used=(),
        documents_total=run.total(),
        documents_processed=len(run.documents),
        documents_unprocessable=len(run.unprocessable),
        field_accuracy=score_fields(run.documents, manifest, split),
        notes=run_notes(run),
    )


def run_notes(run: ExtractionRun) -> str:
    """Every degraded path is recorded rather than hidden."""
    served = ", ".join(sorted(route.value for route in ROUTE_EXTRACTORS))
    notes = f"Routed by file signature. Deterministic routes served: {served}."
    if run.unserved:
        skipped = ", ".join(
            f"{route.value} ({count})" for route, count in sorted(run.unserved.items())
        )
        notes += f" Routed but left unextracted by this offline run: {skipped}."
    return notes + run.degradations.notes()


def statement_from_extraction(
    doc: ExtractedDocument, record: ManifestRecord
) -> StatementDoc | None:
    """Extraction shaped as a statement so oracle and end to end run one engine.

    Identity the extractor never reads (dealer, marque, period) comes from the
    record; every reference, amount, and date comes from the extraction, which
    is what makes the gap between the two modes an extraction measurement.
    """
    truth = record.truth
    if truth is None:
        return None
    lines = tuple(
        line for line in (_line(doc, line_no) for line_no in _line_numbers(doc)) if line is not None
    )
    subtotal = sum(line.amount_cents for line in lines)
    return StatementDoc(
        doc_id=doc.doc_id,
        dealer_id=truth.dealer_id,
        doc_type=truth.doc_type,
        oem=truth.oem,
        statement_number=_header_text(doc, FieldName.STATEMENT_NUMBER) or truth.statement_number,
        statement_date=_header_date(doc, FieldName.STATEMENT_DATE) or truth.statement_date,
        period_start=truth.period_start,
        period_end=truth.period_end,
        previous_balance_cents=_header_cents(doc, FieldName.PREVIOUS_BALANCE),
        subtotal_cents=subtotal,
        total_cents=_header_cents(doc, FieldName.TOTAL) or subtotal,
        lines=lines,
    )


def signal_context(record: ManifestRecord) -> SignalContext | None:
    """Per-document context the confidence signals cannot infer alone."""
    truth = record.truth
    if truth is None:
        return None
    return SignalContext(
        oem=truth.oem,
        period_start=truth.period_start,
        period_end=truth.period_end,
        quality_tier=record.quality_tier,
        line_types={line.line_no: line.line_type for line in truth.lines},
    )


def git_short_sha() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return _GIT_SHA_FALLBACK
    sha = result.stdout.strip()
    return sha if result.returncode == 0 and sha else _GIT_SHA_FALLBACK


def _guarded(extractor: Extractor, path: Path, doc_id: str) -> ExtractedDocument:
    """No single document may end a run, whatever its bytes do to a parser."""
    try:
        return extractor(path, doc_id)
    except Exception as error:  # the run continues past anything a parser does
        _LOGGER.warning("%s failed to extract: %s", doc_id, error)
        return ExtractedDocument(
            doc_id=doc_id,
            file_path=path.as_posix(),
            route=ExtractionRoute.UNPROCESSABLE,
            error=IngestError(kind=IngestErrorKind.UNRECOGNIZED, detail=str(error)),
        )


def _failed(record: ManifestRecord, kind: IngestErrorKind, detail: str) -> ExtractedDocument:
    return ExtractedDocument(
        doc_id=record.doc_id,
        file_path=record.file_path,
        route=ExtractionRoute.UNPROCESSABLE,
        error=IngestError(kind=kind, detail=detail),
    )


def _line_numbers(doc: ExtractedDocument) -> tuple[int, ...]:
    return tuple(sorted({f.line_no for f in doc.line_fields if f.line_no is not None}))


def _line(doc: ExtractedDocument, line_no: int) -> StatementLine | None:
    """A line the engine can match, or None when the extraction lacks its bones."""
    fields = {f.name: f for f in doc.line_fields if f.line_no == line_no}
    amount = fields.get(FieldName.LINE_AMOUNT)
    line_date = fields.get(FieldName.LINE_DATE)
    if amount is None or amount.value_cents is None or line_date is None:
        return None
    if line_date.value_date is None:
        return None
    description = fields.get(FieldName.DESCRIPTION)
    return StatementLine(
        line_no=line_no,
        # Line type is never extracted; the sign carries the same information
        # for every rule the engine applies.
        line_type=LineType.CHARGE if amount.value_cents >= 0 else LineType.CREDIT,
        claim_number=_text(fields.get(FieldName.CLAIM_NUMBER)),
        ro_number=_text(fields.get(FieldName.RO_NUMBER)),
        vin=_text(fields.get(FieldName.VIN)),
        invoice_number=_text(fields.get(FieldName.INVOICE_NUMBER)),
        program_code=_text(fields.get(FieldName.PROGRAM_CODE)),
        line_date=line_date.value_date,
        description=_text(description) or "",
        amount_cents=amount.value_cents,
    )


def _text(field: ExtractedField | None) -> str | None:
    return None if field is None else field.value


def _header(doc: ExtractedDocument, name: FieldName) -> ExtractedField | None:
    return next((f for f in doc.header_fields if f.name is name), None)


def _header_text(doc: ExtractedDocument, name: FieldName) -> str | None:
    return _text(_header(doc, name))


def _header_cents(doc: ExtractedDocument, name: FieldName) -> int | None:
    field = _header(doc, name)
    return None if field is None else field.value_cents


def _header_date(doc: ExtractedDocument, name: FieldName) -> date | None:
    field = _header(doc, name)
    return None if field is None else field.value_date
