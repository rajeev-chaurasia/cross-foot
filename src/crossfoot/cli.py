"""Crossfoot command line interface."""

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import httpx
import typer

from crossfoot import __version__
from crossfoot.config import NoProviderConfiguredError, ProviderProfile, Settings
from crossfoot.constants import (
    ExtractionRoute,
    FieldFamily,
    LlmMode,
    Provider,
    ReconMode,
    SplitName,
)
from crossfoot.llm.client import LlmClient, LlmError

if TYPE_CHECKING:  # imported for signatures only; the commands load them lazily
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
) -> None:
    """Extract a split, sending scanned documents through the vision model."""
    try:
        counts = asyncio.run(_extract_split(dataset, _split_name(split), _llm_mode(mode), resume))
    except NoProviderConfiguredError as error:
        # Only the scanned tier needs a key, so this fires when one is reached.
        typer.echo(f"{error} The deterministic tiers still run without one.")
        raise typer.Exit(code=2) from error
    extracted, unprocessable, skipped = counts
    typer.echo(f"{extracted} extracted, {unprocessable} unprocessable, {skipped} already done")


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
    dataset: Path, split: SplitName, mode: LlmMode, resume: bool
) -> tuple[int, int, int]:
    """Route and extract every document in the split; returns the three counts."""
    from crossfoot.costs import CostLedger
    from crossfoot.evals.paths import UnsafeDatasetPathError, resolve_dataset_path
    from crossfoot.evals.runner import ROUTE_EXTRACTORS, load_manifest, split_records
    from crossfoot.extraction.llm_vision import VisionExtractor, rasterize_pdf
    from crossfoot.extraction.router import route_file
    from crossfoot.llm.cache import ResponseCache
    from crossfoot.llm.runstate import DocStatus, RunState
    from crossfoot.models.extraction import ExtractedDocument

    manifest = load_manifest(dataset)
    records = split_records(manifest, split)
    settings = Settings()
    ledger = CostLedger(COST_DB)
    state = RunState(RUN_STATE_DB)
    cache = ResponseCache(RESPONSE_CACHE_DB)
    run_id = f"extract-{split}-{manifest.config_hash[:8]}"
    state.start_run(run_id, [record.doc_id for record in records])

    def vision_extractor() -> VisionExtractor:
        """Built on first use, so a split with no scans needs no provider key."""
        client = LlmClient(
            settings.primary_profile(),
            settings.llm_timeout_seconds,
            mode=mode,
            cassette_dir=CASSETTE_DIR,
            ledger=ledger,
            cache=cache,
        )
        return VisionExtractor(client, run_id=run_id)

    vision: VisionExtractor | None = None
    extracted: list[ExtractedDocument] = []
    unprocessable = 0
    skipped = 0
    try:
        for record in records:
            if resume and state.status(run_id, record.doc_id) is DocStatus.DONE:
                skipped += 1
                continue
            state.mark_in_progress(run_id, record.doc_id)
            try:
                path = resolve_dataset_path(dataset, record.file_path)
            except UnsafeDatasetPathError as error:
                state.mark_failed(run_id, record.doc_id, str(error))
                unprocessable += 1
                continue
            routing = route_file(path)
            offline = ROUTE_EXTRACTORS.get(routing.route)
            if offline is not None:
                doc = offline(path, record.doc_id)
            elif routing.route is ExtractionRoute.SCANNED_PDF and record.truth is not None:
                vision = vision or vision_extractor()
                doc = await vision.extract_document(
                    doc_id=record.doc_id,
                    file_path=path.as_posix(),
                    # The harness supplies the doc type; classification is its own
                    # purpose in the ledger and is not wired yet.
                    doc_type=record.truth.doc_type,
                    quality_tier=record.quality_tier,
                    images=rasterize_pdf(path),
                )
            else:
                state.mark_failed(run_id, record.doc_id, f"no extractor for {routing.route}")
                unprocessable += 1
                continue
            state.mark_done(run_id, record.doc_id, doc.model_dump_json())
            if doc.route is ExtractionRoute.UNPROCESSABLE:
                unprocessable += 1
            else:
                extracted.append(doc)
    finally:
        state.close()
        ledger.close()
        cache.close()
    _write_extractions(run_id, extracted)
    return len(extracted), unprocessable, skipped


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


def _write_extractions(run_id: str, documents: list["ExtractedDocument"]) -> Path:
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
