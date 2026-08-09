"""Materialize the review database from a dataset, its extractions, and the ledger.

Four sources, one file. The manifest says which documents exist and how they
split; the saved extractions say what was read out of them and how much each
reading is trusted; the reconciliation engine says which of those readings
disagree with the ledger and by how much; the phase 2 cost ledger says what the
work cost. The API then reads only this file, so a number on screen is a row.

Building over an existing database replaces the extraction and exception rows.
The corrections history is never touched, and the field status it implies is
reapplied afterwards, so a rebuild cannot quietly discard human work.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from crossfoot.constants import ExtractionRoute, IngestErrorKind, ReconMode, ReviewStatus, SplitName
from crossfoot.db import connect
from crossfoot.db.schema import ensure_schema
from crossfoot.evals.paths import UnsafeDatasetPathError, resolve_dataset_path
from crossfoot.evals.runner import (
    load_ledger,
    load_manifest,
    split_records,
    statement_from_extraction,
)
from crossfoot.extraction.router import route_file
from crossfoot.models.extraction import ExtractedDocument, ExtractedField
from crossfoot.models.ledger import LedgerBook
from crossfoot.models.manifest import ManifestRecord
from crossfoot.models.reconciliation import ExceptionRecord
from crossfoot.reconcile.engine import reconcile

EXTRACTION_RUN_PREFIX = "extract"
INGEST_RUN_PREFIX = "ingest"
# The dataset hash is what makes a run id name a dataset rather than a moment.
RUN_ID_HASH_CHARS = 8

_INSERT_DOCUMENT = """
INSERT OR REPLACE INTO documents (
    doc_id, file_path, doc_type, quality_tier, route, split, error_kind
) VALUES (?, ?, ?, ?, ?, ?, ?)
"""

_INSERT_FIELD = """
INSERT OR REPLACE INTO fields (
    field_id, doc_id, line_no, name, family, raw_text, value,
    value_cents, value_date, source, crop_kind, page,
    x0, y0, x1, y1, confidence, status, signals
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_INSERT_EXCEPTION = """
INSERT OR REPLACE INTO exceptions (
    exception_id, run_id, exception_type, doc_id, statement_line_no,
    ledger_entry_id, match_key, statement_amount_cents, ledger_amount_cents,
    dollar_impact_cents, memo_amount_cents, explanation, status, detected_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

# A field a human corrected stays corrected, however its row was rebuilt.
_RESTORE_CORRECTED = """
UPDATE fields SET status = ?
WHERE field_id IN (SELECT field_id FROM corrections)
"""

_COST_SCHEMA = "costs"
_ATTACH_COSTS = f"ATTACH DATABASE ? AS {_COST_SCHEMA}"
_DETACH_COSTS = f"DETACH DATABASE {_COST_SCHEMA}"
_COPY_LEDGER = f"INSERT OR REPLACE INTO llm_calls SELECT * FROM {_COST_SCHEMA}.llm_calls"
_COUNT_LEDGER = "SELECT COUNT(*) FROM llm_calls"


@dataclass(frozen=True, slots=True)
class IngestCounts:
    """What one build wrote, so the command can say something true about it."""

    documents: int
    fields: int
    exceptions: int
    llm_calls: int


def extraction_run_id(split: SplitName, config_hash: str) -> str:
    """One id per (split, dataset), so extract, calibrate, and serve agree on the file."""
    return f"{EXTRACTION_RUN_PREFIX}-{split}-{config_hash[:RUN_ID_HASH_CHARS]}"


def saved_extractions(extractions_dir: Path, run_id: str) -> list[ExtractedDocument] | None:
    """Documents a live extract run wrote, or None when that run never ran."""
    path = extractions_dir / f"{run_id}.json"
    if not path.is_file():
        return None
    return [
        ExtractedDocument.model_validate(item)
        for item in json.loads(path.read_text(encoding="utf-8"))
    ]


def build_database(
    *, dataset_dir: Path, db_path: Path, extractions_dir: Path, cost_db: Path
) -> IngestCounts:
    """Write the review database and return what went into it."""
    manifest = load_manifest(dataset_dir)
    book = load_ledger(dataset_dir)
    run_id = f"{INGEST_RUN_PREFIX}-{manifest.config_hash[:RUN_ID_HASH_CHARS]}"
    records = [record for split in SplitName for record in split_records(manifest, split)]
    extracted = _load_extractions(extractions_dir, manifest.config_hash)

    connection = connect(db_path)
    try:
        with connection:
            ensure_schema(connection)
            fields = _write_documents(connection, records, extracted, dataset_dir)
            exceptions = _write_exceptions(connection, records, extracted, book, run_id)
            connection.execute(_RESTORE_CORRECTED, (ReviewStatus.HUMAN_CORRECTED.value,))
        llm_calls = _copy_cost_ledger(connection, cost_db)
    finally:
        connection.close()
    return IngestCounts(
        documents=len(records), fields=fields, exceptions=exceptions, llm_calls=llm_calls
    )


def _load_extractions(extractions_dir: Path, config_hash: str) -> dict[str, ExtractedDocument]:
    """Every split's saved extraction, keyed by doc_id. A split never run contributes none."""
    documents: dict[str, ExtractedDocument] = {}
    for split in SplitName:
        saved = saved_extractions(extractions_dir, extraction_run_id(split, config_hash))
        for document in saved or ():
            documents[document.doc_id] = document
    return documents


def _write_documents(
    connection: sqlite3.Connection,
    records: Sequence[ManifestRecord],
    extracted: dict[str, ExtractedDocument],
    dataset_dir: Path,
) -> int:
    """One row per manifest record, plus every field read out of it. Returns the field count."""
    written = 0
    for record in records:
        document = extracted.get(record.doc_id)
        route, error_kind = _routing(record, document, dataset_dir)
        connection.execute(
            _INSERT_DOCUMENT,
            (
                record.doc_id,
                record.file_path,
                _doc_type(record, document),
                record.quality_tier.value,
                route.value,
                None if record.split is None else record.split.value,
                None if error_kind is None else error_kind.value,
            ),
        )
        if document is None:
            continue
        for field in (*document.header_fields, *document.line_fields):
            connection.execute(_INSERT_FIELD, _field_row(field))
            written += 1
    return written


def _write_exceptions(
    connection: sqlite3.Connection,
    records: Sequence[ManifestRecord],
    extracted: dict[str, ExtractedDocument],
    book: LedgerBook,
    run_id: str,
) -> int:
    """Reconcile every extracted document against the ledger and store what it found."""
    now = datetime.now(UTC)
    written = 0
    for record in records:
        document = extracted.get(record.doc_id)
        if document is None or document.route is ExtractionRoute.UNPROCESSABLE:
            continue
        statement = statement_from_extraction(document, record)
        if statement is None:
            continue
        result = reconcile(statement, book, mode=ReconMode.END_TO_END, run_id=run_id, now=now)
        for exception in result.exceptions:
            connection.execute(_INSERT_EXCEPTION, _exception_row(exception))
            written += 1
    return written


def _copy_cost_ledger(connection: sqlite3.Connection, cost_db: Path) -> int:
    """Bring phase 2's llm_calls into the same file, since the summary tile reads it."""
    if not cost_db.is_file():
        return 0
    # ATTACH and DETACH cannot run inside a transaction, so the copy owns its own.
    connection.execute(_ATTACH_COSTS, (str(cost_db),))
    try:
        with connection:
            connection.execute(_COPY_LEDGER)
        (total,) = connection.execute(_COUNT_LEDGER).fetchone()
    finally:
        connection.execute(_DETACH_COSTS)
    return int(total)


def _routing(
    record: ManifestRecord, document: ExtractedDocument | None, dataset_dir: Path
) -> tuple[ExtractionRoute, IngestErrorKind | None]:
    """How the file routed, taken from the extraction or read off its bytes."""
    if document is not None:
        return document.route, None if document.error is None else document.error.kind
    try:
        path = resolve_dataset_path(dataset_dir, record.file_path)
    except UnsafeDatasetPathError:
        # The manifest is data, not an instruction: a path that leaves the
        # dataset is a document nothing can be extracted from, not a file read.
        return ExtractionRoute.UNPROCESSABLE, IngestErrorKind.UNRECOGNIZED
    routing = route_file(path)
    return routing.route, None if routing.error is None else routing.error.kind


def _doc_type(record: ManifestRecord, document: ExtractedDocument | None) -> str | None:
    if document is not None and document.doc_type is not None:
        return document.doc_type.value
    return None if record.truth is None else record.truth.doc_type.value


def _field_row(field: ExtractedField) -> tuple[object, ...]:
    bbox = field.bbox
    return (
        field.field_id,
        field.doc_id,
        field.line_no,
        field.name.value,
        field.family.value,
        field.raw_text,
        field.value,
        field.value_cents,
        None if field.value_date is None else field.value_date.isoformat(),
        field.source.value,
        field.crop_kind.value,
        None if bbox is None else bbox.page,
        None if bbox is None else bbox.x0,
        None if bbox is None else bbox.y0,
        None if bbox is None else bbox.x1,
        None if bbox is None else bbox.y1,
        field.confidence,
        field.status.value,
        field.signals.model_dump_json(),
    )


def _exception_row(exception: ExceptionRecord) -> tuple[object, ...]:
    return (
        exception.exception_id,
        exception.run_id,
        exception.exception_type.value,
        exception.doc_id,
        exception.statement_line_no,
        exception.ledger_entry_id,
        exception.match_key,
        exception.statement_amount_cents,
        exception.ledger_amount_cents,
        exception.dollar_impact_cents,
        exception.memo_amount_cents,
        exception.explanation,
        exception.status.value,
        exception.detected_at.isoformat(),
    )
