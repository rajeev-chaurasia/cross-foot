"""Crossfoot command line interface."""

import asyncio

import typer

from crossfoot import __version__
from crossfoot.config import Settings
from crossfoot.llm.client import ChatResult, LlmClient

app = typer.Typer(no_args_is_help=True, add_completion=False)

PROBE_PROMPT = "Reply with the single word: ok"


@app.command()
def version() -> None:
    """Print the installed version."""
    typer.echo(__version__)


@app.command()
def probe() -> None:
    """Make one live LLM call and print usage plus rate limit headers."""
    settings = Settings()
    if not settings.llm_api_key:
        typer.echo("CROSSFOOT_LLM_API_KEY is not set. Copy .env.example to .env and add a key.")
        raise typer.Exit(code=2)
    result = asyncio.run(_probe(settings))
    typer.echo(f"base_url: {settings.llm_base_url}")
    typer.echo(f"model: {result.model}")
    typer.echo(f"content: {result.content.strip()}")
    typer.echo(f"latency_ms: {result.latency_ms}")
    typer.echo(
        f"tokens: prompt={result.usage.prompt_tokens}"
        f" completion={result.usage.completion_tokens}"
        f" total={result.usage.total_tokens}"
    )
    if result.rate_limit_headers:
        for name, value in sorted(result.rate_limit_headers.items()):
            typer.echo(f"header {name}: {value}")
    else:
        typer.echo("no rate limit headers returned")


async def _probe(settings: Settings) -> ChatResult:
    client = LlmClient(settings)
    return await client.chat([{"role": "user", "content": PROBE_PROMPT}])


def main() -> None:
    app()
