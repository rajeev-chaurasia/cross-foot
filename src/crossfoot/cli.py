"""Crossfoot command line interface."""

import asyncio
from pathlib import Path
from typing import Annotated

import httpx
import typer

from crossfoot import __version__
from crossfoot.config import ProviderProfile, Settings
from crossfoot.constants import Provider, SplitName
from crossfoot.llm.client import LlmClient, LlmError

app = typer.Typer(no_args_is_help=True, add_completion=False)

PROBE_PROMPT = "Reply with the single word: ok"
DEFAULT_DATASET_DIR = Path("data/dataset")
SCORECARDS_DIR = Path("scorecards")


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
    """Run the phase 1 baseline eval and write a scorecard JSON."""
    from crossfoot.evals.runner import run_eval  # lazy: keeps other commands fast

    try:
        split_name = SplitName(split)
    except ValueError as error:
        typer.echo(f"Unknown split {split!r}; expected one of: {', '.join(SplitName)}.")
        raise typer.Exit(code=2) from error
    scorecard = run_eval(dataset, split_name)
    out_path = SCORECARDS_DIR / scorecard.run_id / "scorecard.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(scorecard.model_dump_json(indent=2), encoding="utf-8")
    for cell in scorecard.field_accuracy:
        accuracy = cell.correct_canonical / cell.fields_expected
        typer.echo(
            f"{cell.field_family}/{cell.quality_tier}:"
            f" {cell.correct_canonical}/{cell.fields_expected} canonical ({accuracy:.1%}),"
            f" {cell.correct_raw} raw"
        )
    typer.echo(str(out_path))


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


def main() -> None:
    app()
