"""Materialize the review database from a dataset, its extractions, and the ledger.

Four sources, one file. The manifest says which documents exist and how they
split; the saved extractions say what was read out of them; the reconciliation
engine says which of those readings disagree with the ledger and by how much; the
phase 2 cost ledger says what the work cost. The API then reads only this file,
so a number on screen is a row.

How much a reading is trusted is decided here rather than carried in from
extraction. `crossfoot.scoring` fits the family scorers on TRAIN, chooses the
thresholds on CALIBRATION, and applies that operating point to every split, so
the queue holds the fields the model is unsure of rather than the fields no
deterministic validator happened to cover. The step lives inside the build so
`crossfoot serve` stays one command.

Building over an existing database replaces the extraction and exception rows.
Human decisions outrank any score: a field a reviewer accepted or corrected keeps
its status and its confidence, an exception a reviewer resolved stays resolved,
and the corrections history is never touched.

Reconciliation runs over the rows this module just wrote, through the same
`crossfoot.db.reconciliation` the review API runs after a correction, so the
exceptions a build produces and the exceptions a correction produces come from
one implementation. That is also why each document's blocking identity lands on
its `documents` row: the serving path has no manifest to ask.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from crossfoot.confidence.calibration import FIT_SPLIT, THRESHOLD_SPLIT
from crossfoot.constants import ExtractionRoute, IngestErrorKind, ReviewStatus, SplitName
from crossfoot.db import connect, reconciliation, thresholds
from crossfoot.db.schema import ensure_schema
from crossfoot.evals.metrics import field_is_correct
from crossfoot.evals.paths import UnsafeDatasetPathError, resolve_dataset_path
from crossfoot.evals.runner import load_ledger, load_manifest, split_records
from crossfoot.extraction.router import route_file
from crossfoot.models.extraction import ExtractedDocument, ExtractedField
from crossfoot.models.ledger import LedgerBook
from crossfoot.models.manifest import ManifestRecord
from crossfoot.models.scorecard import ThresholdPoint
from crossfoot.scoring import FieldLabel, apply_confidence

EXTRACTION_RUN_PREFIX = "extract"
INGEST_RUN_PREFIX = "ingest"
# The dataset hash is what makes a run id name a dataset rather than a moment.
RUN_ID_HASH_CHARS = 8

_INSERT_DOCUMENT = """
INSERT OR REPLACE INTO documents (
    doc_id, file_path, doc_type, quality_tier, route, split, error_kind,
    dealer_id, oem, period_start, period_end
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

# crop_kind here is the extractor's own record of how it located a value, which
# is what decides whether the stored corners are worth reading. What a review
# crop was actually cut to lives in rendered_crops, which this rebuild does not
# touch: that row was written with the PNG still on disk, and the two stay
# together or are settled together on the next request.
_INSERT_FIELD = """
INSERT OR REPLACE INTO fields (
    field_id, doc_id, line_no, name, family, raw_text, value,
    value_cents, value_date, source, crop_kind, page,
    x0, y0, x1, y1, confidence, status, signals
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

# A field a human corrected stays corrected, however its row was rebuilt.
_RESTORE_CORRECTED = """
UPDATE fields SET status = ?
WHERE field_id IN (SELECT field_id FROM corrections)
"""

# A human decision outranks any score, so it is read before the rebuild and put
# back after it, confidence included: a decided field is not re-scored.
# HUMAN_ACCEPTED lives only in this column, so an INSERT OR REPLACE that did not
# carry it forward would lose the decision outright.
_HUMAN_STATUSES = (ReviewStatus.HUMAN_ACCEPTED, ReviewStatus.HUMAN_CORRECTED)
_SELECT_HUMAN = "SELECT field_id, status, confidence FROM fields WHERE status IN (?, ?)"
_RESTORE_HUMAN = "UPDATE fields SET status = ?, confidence = ? WHERE field_id = ?"

_COUNT_AUTO_ACCEPTED = "SELECT COUNT(*) FROM fields WHERE status = ?"
_COUNT_EXCEPTIONS = "SELECT COUNT(*) FROM exceptions"

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
    auto_accepted: int = 0
    # The operating point the fields above were cut at, in the same order the
    # sweep chose it, so the build can print what it applied.
    thresholds: tuple[ThresholdPoint, ...] = ()


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
    # Confidence and status are decided before a row is written, so the fields
    # table never holds a score the operating point below did not produce.
    #
    # The manifest crosses into scoring as LABELS ONLY. `apply_confidence` reads
    # its features off the extractions it is handed and cannot see this module's
    # records at all, so nothing the generator knows about a document reaches the
    # feature vector behind a review queue position.
    scored = apply_confidence(list(extracted.values()), _labels(records, extracted))
    documents = {document.doc_id: document for document in scored.documents}

    connection = connect(db_path)
    try:
        with connection:
            ensure_schema(connection)
            human = _human_decisions(connection)
            fields = _write_documents(connection, records, documents, dataset_dir)
            exceptions = _write_exceptions(connection, records, documents, book, run_id)
            _restore_human(connection, human)
            connection.execute(_RESTORE_CORRECTED, (ReviewStatus.HUMAN_CORRECTED.value,))
            thresholds.replace(
                connection,
                scored.thresholds,
                run_id=run_id,
                fit_split=FIT_SPLIT,
                threshold_split=THRESHOLD_SPLIT,
            )
            (auto_accepted,) = connection.execute(
                _COUNT_AUTO_ACCEPTED, (ReviewStatus.AUTO_ACCEPTED.value,)
            ).fetchone()
        llm_calls = _copy_cost_ledger(connection, cost_db)
    finally:
        connection.close()
    return IngestCounts(
        documents=len(records),
        fields=fields,
        exceptions=exceptions,
        llm_calls=llm_calls,
        auto_accepted=int(auto_accepted),
        thresholds=scored.thresholds,
    )


def _human_decisions(connection: sqlite3.Connection) -> list[tuple[str, float, str]]:
    """(status, confidence, field_id) for every field a reviewer already ruled on."""
    rows = connection.execute(
        _SELECT_HUMAN, tuple(status.value for status in _HUMAN_STATUSES)
    ).fetchall()
    return [(str(row["status"]), float(row["confidence"]), str(row["field_id"])) for row in rows]


def _restore_human(
    connection: sqlite3.Connection, decisions: Sequence[tuple[str, float, str]]
) -> None:
    for decision in decisions:
        connection.execute(_RESTORE_HUMAN, decision)


def _labels(
    records: Sequence[ManifestRecord], extracted: Mapping[str, ExtractedDocument]
) -> list[FieldLabel]:
    """Every extracted field the manifest can judge, as a label and nothing more.

    This is the one place truth touches the confidence pass, and it hands over a
    field id, a bit, and a split. A document's tier, marque, period and line types
    stay here; they are the answer key, not evidence a reader of the document
    could have gathered.
    """
    labels: list[FieldLabel] = []
    for record in records:
        document = extracted.get(record.doc_id)
        if document is None or record.truth is None or record.split is None:
            continue
        for field in (*document.header_fields, *document.line_fields):
            correct = field_is_correct(field, record.truth)
            if correct is None:
                continue  # truth holds no value there, so there is nothing to learn
            labels.append(FieldLabel(field.field_id, correct, record.split))
    return labels


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
        truth = record.truth
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
                # The blocking identity, stored because the serving path has to
                # reconcile this document again with no manifest in reach. It is
                # operational context, not an answer: in production a dealer, a
                # marque and a period are known at ingest.
                None if truth is None else truth.dealer_id,
                None if truth is None else truth.oem.value,
                None if truth is None else truth.period_start.isoformat(),
                None if truth is None else truth.period_end.isoformat(),
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
    extracted: Mapping[str, ExtractedDocument],
    book: LedgerBook,
    run_id: str,
) -> int:
    """Reconcile every extracted document against the ledger and store what it found.

    Reads back the rows written a moment ago rather than the extraction objects,
    because `db.reconciliation` is the same code a correction runs later. Two
    implementations of this would drift the day one of them learned something.
    """
    now = datetime.now(UTC)
    for record in records:
        document = extracted.get(record.doc_id)
        if document is None or document.route is ExtractionRoute.UNPROCESSABLE:
            continue
        reconciliation.reconcile_document(
            connection, doc_id=record.doc_id, book=book, run_id=run_id, now=now
        )
    (total,) = connection.execute(_COUNT_EXCEPTIONS).fetchone()
    return int(total)


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
