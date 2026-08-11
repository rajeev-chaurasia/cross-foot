"""Eval runner: route every document by its bytes, score what came back.

Routing is the extractor's own decision, made from the file signature rather
than from the manifest, so a mislabelled artifact is caught the same way it
would be in production. No document can stop a run: an unreadable file becomes
an UNPROCESSABLE result with a typed error and the loop continues.
"""

import logging
import subprocess
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from crossfoot.confidence.calibration import (
    FIT_SPLIT,
    THRESHOLD_SPLIT,
    ConfidenceSample,
    PlattScaler,
    TrainingSample,
    choose_thresholds,
    fit_platt_scalers,
    fit_scorers,
    platt_cells,
    reliability_bins,
    rescale,
    sweep_point,
)
from crossfoot.confidence.scorer import LogisticModel
from crossfoot.confidence.signals import attach_signals
from crossfoot.constants import (
    ExtractionRoute,
    FieldFamily,
    FieldName,
    IngestErrorKind,
    LineType,
    Provider,
    SplitName,
)
from crossfoot.costs.ledger import models_for_run
from crossfoot.evals.metrics import field_is_correct, score_fields
from crossfoot.evals.paths import UnsafeDatasetPathError, resolve_dataset_path
from crossfoot.extraction.pdf_text import extract_pdf
from crossfoot.extraction.router import route_file
from crossfoot.extraction.tabular import extract_csv
from crossfoot.extraction.xlsx import extract_xlsx
from crossfoot.models.extraction import ExtractedDocument, ExtractedField, FieldSignals, IngestError
from crossfoot.models.ledger import LedgerBook
from crossfoot.models.manifest import DatasetManifest, ManifestRecord
from crossfoot.models.scorecard import CalibrationBin, PlattCell, Scorecard, ThresholdPoint
from crossfoot.models.statement import StatementDoc, StatementLine

_LOGGER = logging.getLogger(__name__)

MANIFEST_FILENAME = "manifest.json"
LEDGER_FILENAME = "ledger.json"
# Where `crossfoot extract` writes, so an eval reports the live run by default.
DEFAULT_EXTRACTIONS_DIR = Path("data/extractions")
# Where the vision path bills itself. The scorecard names its models from here,
# so a run that called a model cannot publish a blank where the model should be.
DEFAULT_COST_DB = Path("data/costs.db")

_GIT_SHA_FALLBACK = "unknown"
_GIT_TIMEOUT_SECONDS = 10

Extractor = Callable[[Path, str], ExtractedDocument]

# The published sweep is a curve dense enough to read the shape of and short
# enough to commit beside the numbers it explains. Thresholds run the whole
# confidence range, because a review rate only means something against it.
SWEEP_GRID_POINTS = 41

LabelledField = tuple[FieldFamily, FieldSignals, bool]

# Routes this offline runner can serve. The scanned tier needs a live vision
# client, so `crossfoot extract` drives it and the notes record its absence.
ROUTE_EXTRACTORS: dict[ExtractionRoute, Extractor] = {
    ExtractionRoute.CSV: extract_csv,
    ExtractionRoute.DIGITAL_PDF: extract_pdf,
    ExtractionRoute.XLSX: extract_xlsx,
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


@dataclass(frozen=True, slots=True)
class ConfidenceSections:
    """The confidence half of a scorecard, plus the one sentence its notes carry.

    Empty when the saved extractions a fit needs are absent, because a published
    operating point that was fit on nothing is worse than no operating point.
    """

    calibration: tuple[CalibrationBin, ...] = ()
    threshold_sweep: tuple[ThresholdPoint, ...] = ()
    scored_fields: int = 0
    auto_accept_precision: float = 0.0
    review_rate: float = 0.0
    # The rescaling the numbers above were measured under, headed for the
    # scorecard. A reliability diagram means a different thing depending on it
    # and a reader cannot tell the two apart by eye, so the run publishes the
    # cells themselves rather than a claim that a correction happened.
    platt_scaling: tuple[PlattCell, ...] = ()

    @property
    def calibrated(self) -> bool:
        """Read off the published cells, so the notes cannot contradict the scorecard."""
        return bool(self.platt_scaling)

    def notes(self, split: SplitName) -> str:
        if not self.scored_fields:
            return ""
        scaling = (
            f" Scores Platt scaled on {THRESHOLD_SPLIT} before the threshold was chosen."
            if self.calibrated
            else ""
        )
        return (
            f" Confidence fit on {FIT_SPLIT}, thresholds chosen on {THRESHOLD_SPLIT},"
            f" reported on {split}: {self.scored_fields} scored fields,"
            f" {self.auto_accept_precision:.2%} auto accept precision"
            f" at {self.review_rate:.2%} review.{scaling}"
        )


def _saved_run(
    manifest: DatasetManifest, split: SplitName, extractions_dir: Path | None
) -> ExtractionRun | None:
    """The live extraction for this split, split into served and unprocessable."""
    from crossfoot.ingest_db import extraction_run_id, saved_extractions

    if extractions_dir is None:
        extractions_dir = DEFAULT_EXTRACTIONS_DIR
    run_id = extraction_run_id(split, manifest.config_hash)
    documents = saved_extractions(extractions_dir, run_id)
    if documents is None:
        return None
    served = tuple(d for d in documents if d.route is not ExtractionRoute.UNPROCESSABLE)
    failed = tuple(d for d in documents if d.route is ExtractionRoute.UNPROCESSABLE)
    return ExtractionRun(documents=served, unprocessable=failed, unserved=Counter())


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


def run_eval(
    dataset_dir: Path,
    split: SplitName,
    extractions_dir: Path | None = None,
    cost_db: Path | None = None,
    *,
    calibrate: bool = False,
) -> Scorecard:
    """Score a split and write a scorecard.

    Prefers the documents a live `crossfoot extract` saved. Re-extracting here
    runs the offline routes only, so every scanned document would score zero and
    the published scorecard would understate the pipeline it is meant to report.

    The models the scorecard names are read from the cost ledger for that same
    saved run, never declared here: the scanned tier is served by whichever
    vision provider answered on the day, and only the ledger knows which.
    """
    manifest = load_manifest(dataset_dir)
    # The ledger is a legitimate pipeline input; validate it is present and well formed.
    load_ledger(dataset_dir)
    run = _saved_run(manifest, split, extractions_dir) or extract_split(
        dataset_dir, manifest, split
    )
    sections = confidence_sections(
        manifest, split, extractions_dir or DEFAULT_EXTRACTIONS_DIR, calibrate=calibrate
    )
    now = datetime.now(UTC)
    git_sha = git_short_sha()
    return Scorecard(
        run_id=f"{now:%Y%m%dT%H%M%S}-{git_sha}",
        created_at=now,
        git_sha=git_sha,
        dataset_config_hash=manifest.config_hash,
        master_seed=manifest.master_seed,
        split=split,
        models_used=models_used(manifest.config_hash, split, cost_db or DEFAULT_COST_DB),
        documents_total=run.total(),
        documents_processed=len(run.documents),
        documents_unprocessable=len(run.unprocessable),
        field_accuracy=score_fields(run.documents, manifest, split),
        calibration=sections.calibration,
        platt_scaling=sections.platt_scaling,
        threshold_sweep=sections.threshold_sweep,
        notes=run_notes(run) + sections.notes(split),
    )


def models_used(config_hash: str, split: SplitName, cost_db: Path) -> tuple[str, ...]:
    """The models the extraction being scored actually called, from the cost ledger.

    Public because both scorecard write sites need it and neither may guess: a
    scorecard that publishes an empty tuple is saying it has no record, which is
    not the same claim as no model having run.

    Scoped to the attempt whose extractions survived. A run id names a split and
    a dataset rather than one invocation, so an abandoned attempt leaves calls
    behind under the same id and would otherwise be published as a model that
    produced these numbers.
    """
    from crossfoot.ingest_db import extraction_run_id

    run_id = extraction_run_id(split, config_hash)
    return models_for_run(cost_db, run_id, since=_attempt_started_at(run_id))


def _attempt_started_at(run_id: str) -> str | None:
    """When the surviving attempt first checkpointed, or None when nothing did."""
    from crossfoot.llm.runstate import RunState

    state_db = Path("data/runstate.db")
    if not state_db.is_file():
        return None
    state = RunState(state_db)
    try:
        return state.run_started_at(run_id)
    finally:
        state.close()


def confidence_sections(
    manifest: DatasetManifest, split: SplitName, extractions_dir: Path, *, calibrate: bool = False
) -> ConfidenceSections:
    """Reliability and the threshold sweep for one split, or nothing when a fit is impossible.

    Split discipline is delegated, not restated: `fit_scorers` and
    `choose_thresholds` refuse any split but their own, so the only decision made
    here is which rows to hand them. The reported split is measured at the
    threshold that came back and never contributes to choosing it.

    `calibrate` puts every score through a Platt scaler fit on CALIBRATION, and
    the scalers themselves are published so the numbers here are reproducible
    from the scorecard alone. Rescaling happens before the threshold is chosen,
    never after: a threshold picked on uncalibrated scores names a different
    operating point once the scores underneath it move.

    The sweep is laid out the way `crossfoot.evals.plots.family_sweeps` reads it:
    per family, the calibration curve in ascending threshold order, then one last
    point holding what the reported split reached at the applied threshold. That
    last point is the generalization gap, published rather than left to a reader
    to reconstruct.
    """
    train = _labelled_fields(manifest, FIT_SPLIT, extractions_dir)
    calibration = _labelled_fields(manifest, THRESHOLD_SPLIT, extractions_dir)
    reported = _labelled_fields(manifest, split, extractions_dir)
    if not train or not calibration or not reported:
        return ConfidenceSections()

    models = fit_scorers(
        [TrainingSample(family, signals, correct, FIT_SPLIT) for family, signals, correct in train],
        split=FIT_SPLIT,
    )
    calibration_samples = _samples(calibration, models, THRESHOLD_SPLIT)
    reported_samples = _samples(reported, models, split)
    if not calibration_samples or not reported_samples:
        return ConfidenceSections()

    scalers: Mapping[FieldFamily, PlattScaler] = (
        fit_platt_scalers(calibration_samples, split=THRESHOLD_SPLIT) if calibrate else {}
    )
    # An empty mapping passes every score through untouched, so the uncalibrated
    # path is the same two lines rather than a branch that could drift from them.
    calibration_samples = list(rescale(calibration_samples, scalers))
    reported_samples = list(rescale(reported_samples, scalers))

    bins: list[CalibrationBin] = []
    sweep: list[ThresholdPoint] = []
    accepted = 0
    correct_accepted = 0
    for point in choose_thresholds(calibration_samples, split=THRESHOLD_SPLIT):
        family = point.field_family
        family_reported = [s for s in reported_samples if s.field_family is family]
        if not family_reported:
            continue  # nothing of this family reached the reported split
        family_calibration = [s for s in calibration_samples if s.field_family is family]
        bins.extend(reliability_bins(reported_samples, family))
        sweep.extend(sweep_curve(family, family_calibration, point.threshold))
        sweep.append(sweep_point(family, family_reported, point.threshold))
        auto = [s for s in family_reported if s.confidence >= point.threshold]
        accepted += len(auto)
        correct_accepted += sum(1 for sample in auto if sample.correct)

    scored = len(reported_samples)
    return ConfidenceSections(
        calibration=tuple(bins),
        threshold_sweep=tuple(sweep),
        scored_fields=scored,
        auto_accept_precision=correct_accepted / accepted if accepted else 1.0,
        review_rate=1.0 - accepted / scored,
        platt_scaling=platt_cells(scalers),
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


def _labelled_fields(
    manifest: DatasetManifest, split: SplitName, extractions_dir: Path
) -> list[LabelledField]:
    """One split's saved fields paired with truth, ready for a scorer.

    Truth appears here as a label and nowhere else. The signals a scorer will
    learn from are recomputed from the extraction alone, so a row is a feature
    vector a production document could have produced plus a bit saying whether it
    was right.

    Only the saved extractions count. Re-extracting here would run the offline
    routes alone, so every scanned document would vanish and the fit would see
    nothing but the tiers the pipeline already gets right, which reads as a
    perfectly calibrated model with nothing left to review.
    """
    from crossfoot.ingest_db import extraction_run_id, saved_extractions

    documents = saved_extractions(extractions_dir, extraction_run_id(split, manifest.config_hash))
    if documents is None:
        return []
    records = {record.doc_id: record for record in split_records(manifest, split)}
    rows: list[LabelledField] = []
    for doc in documents:
        record = records.get(doc.doc_id)
        if record is None or record.truth is None:
            continue
        # FEATURES from the extraction, LABELS from truth, and the two lines
        # below are the whole boundary. `attach_signals` is handed the document
        # and nothing else, so no manifest fact can reach a feature; `truth` is
        # read only to decide whether a reading was right. Passing a record into
        # the signals call is what the audit caught, and it is why the call takes
        # no context here.
        scored = attach_signals(doc)
        for field in (*scored.header_fields, *scored.line_fields):
            correct = field_is_correct(field, record.truth)
            if correct is not None:
                rows.append((field.family, field.signals, correct))
    return rows


def _samples(
    rows: Sequence[LabelledField],
    models: Mapping[FieldFamily, LogisticModel],
    split: SplitName,
) -> list[ConfidenceSample]:
    """Scored rows tagged with the split they came from, so the guards can check them."""
    return [
        ConfidenceSample(family, models[family].predict(signals), correct, split)
        for family, signals, correct in rows
        if family in models
    ]


def sweep_curve(
    family: FieldFamily, samples: Sequence[ConfidenceSample], applied: float
) -> list[ThresholdPoint]:
    """The published curve: an even grid plus the threshold actually applied.

    Public because it is half of a layout `crossfoot.evals.plots.family_sweeps`
    reads: the applied threshold has to be a point on this curve, or a figure
    cannot mark the operating point it was drawn to show.

    A threshold that accepts nothing is vacuously precise, so it is left off the
    curve rather than drawn as a perfect score nobody earned. The applied point
    stays whatever it looks like: it is the point the build committed to, and a
    family that ended up reviewing everything must show that.
    """
    grid = {index / (SWEEP_GRID_POINTS - 1) for index in range(SWEEP_GRID_POINTS)}
    points = [sweep_point(family, samples, threshold) for threshold in sorted(grid | {applied})]
    return [point for point in points if point.review_rate < 1.0 or point.threshold == applied]


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
    # A printed total of zero is a reading, not a missing reading. Falling back to the
    # sum of the lines here would repair the one case the crossfoot check exists to
    # catch, a total that contradicts the lines it sits under.
    extracted_total = _header_cents(doc, FieldName.TOTAL)
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
        total_cents=subtotal if extracted_total is None else extracted_total,
        lines=lines,
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
