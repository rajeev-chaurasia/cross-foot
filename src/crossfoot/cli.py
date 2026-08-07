"""Crossfoot command line interface."""

import asyncio
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import httpx
import typer

from crossfoot import __version__
from crossfoot.config import NoProviderConfiguredError, ProviderProfile, Settings
from crossfoot.constants import (
    VISION_CAPABILITIES,
    ExtractionRoute,
    FieldFamily,
    IngestErrorKind,
    LlmMode,
    Provider,
    ReconMode,
    SplitName,
)
from crossfoot.extraction.batch import DEFAULT_EXTRACT_CONCURRENCY
from crossfoot.llm.client import LlmClient, LlmError

if TYPE_CHECKING:  # imported for signatures only; the commands load them lazily
    from crossfoot.evals.runner import VisionDegradations
    from crossfoot.extraction.batch import DocumentOutcome
    from crossfoot.llm.runstate import RunState
    from crossfoot.models.extraction import ExtractedDocument, FieldSignals
    from crossfoot.models.scorecard import Scorecard

app = typer.Typer(no_args_is_help=True, add_completion=False)

PROBE_PROMPT = "Reply with the single word: ok"
DEFAULT_DATASET_DIR = Path("data/dataset")
SCORECARDS_DIR = Path("scorecards")
DEFAULT_DATA_DIR = Path("data")
EXTRACTIONS_DIR = DEFAULT_DATA_DIR / "extractions"
CASSETTE_DIR = Path("tests/fixtures/cassettes")
COST_DB = DEFAULT_DATA_DIR / "costs.db"
RUN_STATE_DB = DEFAULT_DATA_DIR / "runstate.db"
RESPONSE_CACHE_DB = DEFAULT_DATA_DIR / "llm_cache.db"

# Details for the documents this command finishes without extracting. Both are
# properties of the document, so both are final and both reach the output file.
NO_EXTRACTOR_DETAIL = "no extractor for {route}"
NO_DOC_TYPE_DETAIL = "the dataset supplies no doc type for this scan"


@dataclass(frozen=True, slots=True)
class ExtractCounts:
    """What one extract command produced, degraded paths included."""

    extracted: int
    # Finished either way: unprocessable will not change on a rerun, while
    # pending_retry is work this run still owes and a resume will take.
    unprocessable: int
    pending_retry: int
    skipped: int
    degradations: "VisionDegradations"


def vision_profiles(settings: Settings) -> list[ProviderProfile]:
    """The provider chain `extract` gives its vision pool.

    Filtered, not merely ordered: a provider that cannot read images answers
    400, which neither retries nor spills over, so leaving it in the chain kills
    every document that reaches it.
    """
    return settings.profile_pool(requires=VISION_CAPABILITIES)


@app.command()
def version() -> None:
    """Print the installed version."""
    typer.echo(__version__)


@app.command()
def probe(
    provider: Annotated[Provider | None, typer.Option(help="Probe only this provider.")] = None,
) -> None:
    """One tiny live call per configured provider; prints usage and rate limit headers."""
    settings = Settings()
    profiles = settings.configured_profiles()
    if provider is not None:
        profiles = [p for p in profiles if p.name == provider]
    if not profiles:
        typer.echo("No matching provider key found. Copy .env.example to .env and add keys.")
        raise typer.Exit(code=2)
    ok_count = asyncio.run(_probe_all(profiles, settings.llm_timeout_seconds))
    typer.echo(f"{ok_count}/{len(profiles)} providers responded")
    raise typer.Exit(code=0 if ok_count else 1)


async def _probe_all(profiles: list[ProviderProfile], timeout_seconds: float) -> int:
    ok_count = 0
    for profile in profiles:
        label = f"{profile.name} ({profile.model})"
        try:
            result = await LlmClient(profile, timeout_seconds).chat(
                [{"role": "user", "content": PROBE_PROMPT}]
            )
        except (LlmError, httpx.HTTPError) as error:
            typer.echo(f"{label}: FAILED {error}")
            continue
        ok_count += 1
        typer.echo(
            f"{label}: ok in {result.latency_ms}ms,"
            f" content={result.content.strip()!r},"
            f" tokens prompt={result.usage.prompt_tokens}"
            f" completion={result.usage.completion_tokens}"
            f" total={result.usage.total_tokens}"
        )
        for name, value in sorted(result.rate_limit_headers.items()):
            typer.echo(f"  {name}: {value}")
    return ok_count


@app.command()
def eval(
    dataset: Annotated[
        Path, typer.Option(help="Dataset directory written by crossfoot gen.")
    ] = DEFAULT_DATASET_DIR,
    split: Annotated[
        str, typer.Option(help="Split to score: train, calibration, or test.")
    ] = "train",
) -> None:
    """Run the offline eval over every routed tier and write a scorecard JSON."""
    from crossfoot.evals.runner import run_eval  # lazy: keeps other commands fast

    scorecard = run_eval(dataset, _split_name(split))
    for cell in scorecard.field_accuracy:
        accuracy = cell.correct_canonical / cell.fields_expected
        typer.echo(
            f"{cell.field_family}/{cell.quality_tier}:"
            f" {cell.correct_canonical}/{cell.fields_expected} canonical ({accuracy:.1%}),"
            f" {cell.correct_raw} raw,"
            f" {cell.fields_in_truth} in truth,"
            f" {cell.fields_spurious} spurious"
        )
    typer.echo(_write_scorecard(scorecard))


@app.command()
def gen(
    seed: Annotated[
        int, typer.Option(help="Master seed; the same seed reproduces the same bytes.")
    ] = 42,
    out: Annotated[
        Path, typer.Option(help="Output directory for the dataset.")
    ] = DEFAULT_DATASET_DIR,
    profile: Annotated[str, typer.Option(help="Dataset profile: full or small.")] = "full",
) -> None:
    """Generate the synthetic dealer statement dataset."""
    from crossfoot.generator.dataset import DatasetProfile, generate_dataset

    try:
        chosen = DatasetProfile(profile)
    except ValueError as error:
        allowed = ", ".join(DatasetProfile)
        raise typer.BadParameter(f"profile must be one of: {allowed}") from error
    manifest = generate_dataset(master_seed=seed, out_dir=out, profile=chosen)
    typer.echo(f"Wrote {len(manifest.records)} records to {out}")


@app.command()
def extract(
    dataset: Annotated[
        Path, typer.Option(help="Dataset directory written by crossfoot gen.")
    ] = DEFAULT_DATASET_DIR,
    split: Annotated[
        str, typer.Option(help="Split to extract: train, calibration, or test.")
    ] = "train",
    resume: Annotated[bool, typer.Option(help="Skip documents this run already finished.")] = False,
    mode: Annotated[str, typer.Option(help="LLM mode: live, record, or replay.")] = "replay",
    concurrency: Annotated[
        int,
        typer.Option(help="Documents extracted at once; the rate limiter still paces requests."),
    ] = DEFAULT_EXTRACT_CONCURRENCY,
) -> None:
    """Extract a split, sending scanned documents through the vision model."""
    if concurrency < 1:
        raise typer.BadParameter("concurrency must be at least 1")
    try:
        counts = asyncio.run(
            _extract_split(dataset, _split_name(split), _llm_mode(mode), resume, concurrency)
        )
    except NoProviderConfiguredError as error:
        # Only the scanned tier needs a key, so this fires when one is reached.
        typer.echo(f"{error} The deterministic tiers still run without one.")
        raise typer.Exit(code=2) from error
    typer.echo(
        f"{counts.extracted} extracted, {counts.unprocessable} unprocessable,"
        f" {counts.pending_retry} pending retry,"
        f" {counts.skipped} already done{counts.degradations.notes()}"
    )


@app.command()
def reconcile(
    dataset: Annotated[
        Path, typer.Option(help="Dataset directory written by crossfoot gen.")
    ] = DEFAULT_DATASET_DIR,
    split: Annotated[
        str, typer.Option(help="Split to reconcile: train, calibration, or test.")
    ] = "train",
    mode: Annotated[str, typer.Option(help="Recon mode: end_to_end or oracle.")] = "end_to_end",
) -> None:
    """Reconcile a split against the ledger and write the reconciliation scorecard."""
    from crossfoot.evals.metrics import score_recon
    from crossfoot.evals.runner import (
        extract_split,
        git_short_sha,
        load_ledger,
        load_manifest,
        split_records,
        statement_from_extraction,
    )
    from crossfoot.models.reconciliation import ExceptionRecord
    from crossfoot.models.scorecard import Scorecard
    from crossfoot.models.statement import StatementDoc
    from crossfoot.reconcile.engine import reconcile as reconcile_doc

    recon_mode = _recon_mode(mode)
    split_name = _split_name(split)
    manifest = load_manifest(dataset)
    book = load_ledger(dataset)
    records = {record.doc_id: record for record in split_records(manifest, split_name)}
    now = datetime.now(UTC)
    run_id = f"recon-{recon_mode}-{now:%Y%m%dT%H%M%S}"

    statements: list[StatementDoc] = []
    if recon_mode is ReconMode.ORACLE:
        statements = [record.truth for record in records.values() if record.truth is not None]
    else:
        for doc in extract_split(dataset, manifest, split_name).documents:
            statement = statement_from_extraction(doc, records[doc.doc_id])
            if statement is not None:
                statements.append(statement)

    exceptions: list[ExceptionRecord] = []
    for statement in statements:
        result = reconcile_doc(statement, book, mode=recon_mode, run_id=run_id, now=now)
        exceptions.extend(result.exceptions)
    cells = score_recon(exceptions, manifest, split_name, recon_mode)
    for cell in cells:
        typer.echo(
            f"{cell.exception_type}: {cell.detected_true}/{cell.injected} caught,"
            f" {cell.detected_false} false,"
            f" {cell.caught_dollar_cents}/{cell.injected_dollar_cents} cents"
        )
    scorecard = Scorecard(
        run_id=run_id,
        created_at=now,
        git_sha=git_short_sha(),
        dataset_config_hash=manifest.config_hash,
        master_seed=manifest.master_seed,
        split=split_name,
        models_used=(),
        documents_total=len(records),
        documents_processed=len(statements),
        documents_unprocessable=len(records) - len(statements),
        field_accuracy=(),
        reconciliation=cells,
        notes=f"Reconciliation in {recon_mode} mode over {len(statements)} statements.",
    )
    typer.echo(_write_scorecard(scorecard))


@app.command()
def calibrate(
    dataset: Annotated[
        Path, typer.Option(help="Dataset directory written by crossfoot gen.")
    ] = DEFAULT_DATASET_DIR,
) -> None:
    """Fit the confidence scorers on train and choose thresholds on calibration."""
    from crossfoot.confidence.calibration import (
        ConfidenceSample,
        TrainingSample,
        choose_thresholds,
        expected_calibration_error,
        fit_scorers,
        reliability_bins,
    )

    train = _labelled_fields(dataset, SplitName.TRAIN)
    calibration = _labelled_fields(dataset, SplitName.CALIBRATION)
    if not train or not calibration:
        typer.echo("Not enough extracted fields to fit; run crossfoot gen first.")
        raise typer.Exit(code=1)
    models = fit_scorers(
        [
            TrainingSample(family, signals, correct, SplitName.TRAIN)
            for family, signals, correct in train
        ],
        split=SplitName.TRAIN,
    )
    samples = [
        ConfidenceSample(family, models[family].predict(signals), correct, SplitName.CALIBRATION)
        for family, signals, correct in calibration
        if family in models
    ]
    for point in choose_thresholds(samples, split=SplitName.CALIBRATION):
        bins = reliability_bins(samples, point.field_family)
        typer.echo(
            f"{point.field_family}: threshold {point.threshold:.4f},"
            f" precision {point.auto_accept_precision:.4f},"
            f" review rate {point.review_rate:.2%},"
            f" ece {expected_calibration_error(bins):.4f}"
        )


async def _extract_split(
    dataset: Path, split: SplitName, mode: LlmMode, resume: bool, concurrency: int
) -> ExtractCounts:
    """Route and extract the split concurrently; no document ends the run."""
    from crossfoot.costs import CostLedger
    from crossfoot.evals.paths import UnsafeDatasetPathError, resolve_dataset_path
    from crossfoot.evals.runner import (
        ROUTE_EXTRACTORS,
        VisionDegradations,
        load_manifest,
        split_records,
    )
    from crossfoot.extraction.batch import (
        BatchExtractor,
        DocumentOutcome,
        report_to_stderr,
        reset_provider_failures,
    )
    from crossfoot.extraction.llm_vision import VisionExtractor, rasterize_pdf
    from crossfoot.extraction.router import route_file
    from crossfoot.llm.cache import ResponseCache
    from crossfoot.llm.runstate import RunState
    from crossfoot.llm.spillover import SpilloverClient

    manifest = load_manifest(dataset)
    records = {record.doc_id: record for record in split_records(manifest, split)}
    settings = Settings()
    ledger = CostLedger(COST_DB)
    state = RunState(RUN_STATE_DB)
    cache = ResponseCache(RESPONSE_CACHE_DB)
    run_id = f"extract-{split}-{manifest.config_hash[:8]}"
    state.start_run(run_id, list(records))
    if resume:
        # One time recovery. Rows checkpointed DONE before a provider failure
        # could be told from a document failure are unfinished work, and this is
        # what lets them be retried without discarding the run's successes.
        reopened = reset_provider_failures(state, run_id, list(records))
        if reopened:
            report_to_stderr(f"{reopened} provider failures reset to pending")

    vision: VisionExtractor | None = None
    vision_pool: SpilloverClient | None = None
    # Workers share one extractor, so its counters and its provider pool stay
    # single; the lock is what keeps first use from building two of them.
    vision_lock = asyncio.Lock()

    async def vision_extractor() -> VisionExtractor:
        """Built once on first use, so a split with no scans needs no provider key."""
        nonlocal vision, vision_pool
        async with vision_lock:
            if vision is None:
                # Every configured profile that can actually serve the call, not
                # just the primary: a 220 document run is long enough that one
                # transient 503 is a certainty, not a risk. A text only provider
                # is filtered out instead, because its 400 neither retries nor
                # spills over and would kill the document outright. It waits
                # cooldowns out too, because skipping the rest of a batch during
                # an outage reads as an extraction quality problem.
                vision_pool = SpilloverClient(
                    profiles=vision_profiles(settings),
                    ledger=ledger,
                    timeout_seconds=settings.llm_timeout_seconds,
                    mode=mode,
                    cassette_dir=CASSETTE_DIR,
                    cache=cache,
                    wait_for_cooldown=True,
                )
                vision = VisionExtractor(vision_pool, run_id=run_id)
            return vision

    async def extract_one(doc_id: str) -> DocumentOutcome:
        """Every failure below is the document's own, so each is a finished result."""
        record = records[doc_id]
        try:
            path = resolve_dataset_path(dataset, record.file_path)
        except UnsafeDatasetPathError as error:
            return _unprocessable_outcome(
                doc_id, record.file_path, IngestErrorKind.UNRECOGNIZED, str(error)
            )
        routing = route_file(path)
        offline = ROUTE_EXTRACTORS.get(routing.route)
        if offline is not None:
            return DocumentOutcome(document=offline(path, doc_id))
        if routing.error is not None:
            return _unprocessable_outcome(
                doc_id, path.as_posix(), routing.error.kind, routing.error.detail
            )
        if routing.route is not ExtractionRoute.SCANNED_PDF:
            return _unprocessable_outcome(
                doc_id,
                path.as_posix(),
                IngestErrorKind.UNRECOGNIZED,
                NO_EXTRACTOR_DETAIL.format(route=routing.route.value),
            )
        if record.truth is None:
            return _unprocessable_outcome(
                doc_id, path.as_posix(), IngestErrorKind.UNRECOGNIZED, NO_DOC_TYPE_DETAIL
            )
        extractor = await vision_extractor()
        return DocumentOutcome(
            document=await extractor.extract_document(
                doc_id=doc_id,
                file_path=path.as_posix(),
                # The harness supplies the doc type; classification is its own
                # purpose in the ledger and is not wired yet.
                doc_type=record.truth.doc_type,
                quality_tier=record.quality_tier,
                images=rasterize_pdf(path),
            )
        )

    def degradations() -> VisionDegradations:
        if vision is None:
            return VisionDegradations()
        return VisionDegradations(
            structured_output_failures=vision.structured_output_failures,
            consistency_degradations=vision.consistency_degradations,
            provider_failures=vision.provider_failures,
            quota_exhausted=() if vision_pool is None else vision_pool.exhausted_providers,
        )

    def degradation_count() -> int:
        counts = degradations()
        return (
            counts.structured_output_failures
            + counts.consistency_degradations
            + counts.provider_failures
        )

    try:
        result = await BatchExtractor(
            state=state,
            run_id=run_id,
            extract=extract_one,
            concurrency=concurrency,
            degradations=degradation_count,
            fatal=(NoProviderConfiguredError,),
        ).run(list(records), resume=resume)
        # Write every document the run has ever finished, not just this pass.
        # A resumed run only re-extracts what was pending, so writing its
        # results alone would drop everything an earlier pass completed.
        finished = _finished_documents(state, run_id, records)
    finally:
        state.close()
        ledger.close()
        cache.close()
    _write_extractions(run_id, finished)
    # Counted off the whole run, so a resume keeps reporting the permanent
    # failures earlier passes recorded; only the retry queue is this pass's.
    extracted = [doc for doc in finished if doc.route is not ExtractionRoute.UNPROCESSABLE]
    return ExtractCounts(
        extracted=len(extracted),
        unprocessable=len(finished) - len(extracted),
        pending_retry=result.pending_retry,
        skipped=result.skipped,
        degradations=degradations(),
    )


def _labelled_fields(
    dataset: Path, split: SplitName
) -> list[tuple[FieldFamily, "FieldSignals", bool]]:
    """Extracted fields of one split paired with truth, ready for the scorer."""
    from crossfoot.confidence.signals import attach_signals
    from crossfoot.evals.metrics import field_is_correct
    from crossfoot.evals.runner import extract_split, load_manifest, signal_context, split_records

    manifest = load_manifest(dataset)
    records = {record.doc_id: record for record in split_records(manifest, split)}
    rows: list[tuple[FieldFamily, FieldSignals, bool]] = []
    for doc in extract_split(dataset, manifest, split).documents:
        record = records[doc.doc_id]
        context = signal_context(record)
        if context is None or record.truth is None:
            continue
        scored = attach_signals(doc, context)
        for field in (*scored.header_fields, *scored.line_fields):
            correct = field_is_correct(field, record.truth)
            if correct is not None:
                rows.append((field.family, field.signals, correct))
    return rows


def _unprocessable_outcome(
    doc_id: str, file_path: str, kind: IngestErrorKind, detail: str
) -> "DocumentOutcome":
    """A finished result for a document no extractor here can serve.

    A typed error rather than a bare message, so the checkpoint can see that the
    document itself is the reason and a rerun would only reach the same answer.
    """
    from crossfoot.extraction.batch import DocumentOutcome
    from crossfoot.models.extraction import ExtractedDocument, IngestError

    return DocumentOutcome(
        document=ExtractedDocument(
            doc_id=doc_id,
            file_path=file_path,
            route=ExtractionRoute.UNPROCESSABLE,
            error=IngestError(kind=kind, detail=detail),
        )
    )


def _finished_documents(
    state: "RunState", run_id: str, doc_ids: Iterable[str]
) -> list["ExtractedDocument"]:
    """Every document this run has completed, across all passes, sorted by doc_id.

    RunState is the record of what finished, so a resumed run reports the whole
    set rather than only the documents its final pass happened to touch. A
    permanent failure is finished too and belongs here: it is a real result the
    scorecard counts. A document still owed a retry is not DONE and stays out.
    """
    from crossfoot.llm.runstate import DocStatus
    from crossfoot.models.extraction import ExtractedDocument

    documents = [
        ExtractedDocument.model_validate_json(stored)
        for doc_id in doc_ids
        if state.status(run_id, doc_id) is DocStatus.DONE
        and (stored := state.result(run_id, doc_id)) is not None
    ]
    return sorted(documents, key=lambda doc: doc.doc_id)


def _write_extractions(run_id: str, documents: Sequence["ExtractedDocument"]) -> Path:
    out_path = EXTRACTIONS_DIR / f"{run_id}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = "[\n" + ",\n".join(doc.model_dump_json(indent=2) for doc in documents) + "\n]\n"
    out_path.write_text(payload, encoding="utf-8")
    return out_path


def _write_scorecard(scorecard: "Scorecard") -> Path:
    out_path = SCORECARDS_DIR / scorecard.run_id / "scorecard.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(scorecard.model_dump_json(indent=2), encoding="utf-8")
    return out_path


def _split_name(split: str) -> SplitName:
    try:
        return SplitName(split)
    except ValueError as error:
        raise typer.BadParameter(f"split must be one of: {', '.join(SplitName)}") from error


def _llm_mode(mode: str) -> LlmMode:
    try:
        return LlmMode(mode)
    except ValueError as error:
        raise typer.BadParameter(f"mode must be one of: {', '.join(LlmMode)}") from error


def _recon_mode(mode: str) -> ReconMode:
    try:
        return ReconMode(mode)
    except ValueError as error:
        raise typer.BadParameter(f"mode must be one of: {', '.join(ReconMode)}") from error


def main() -> None:
    app()
