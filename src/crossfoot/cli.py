"""Crossfoot command line interface."""

import asyncio
from typing import Annotated

import httpx
import typer

from crossfoot import __version__
from crossfoot.config import ProviderProfile, Settings
from crossfoot.constants import Provider
from crossfoot.llm.client import LlmClient, LlmError

app = typer.Typer(no_args_is_help=True, add_completion=False)

PROBE_PROMPT = "Reply with the single word: ok"


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


def main() -> None:
    app()
