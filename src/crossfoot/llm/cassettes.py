"""Record and replay of LLM calls as one JSON file per request key.

Scrubbing is by construction rather than by filtering: the writer serializes an
allowlist of response fields, so the raw request, the Authorization header, and
every throttling header are structurally absent. Scrubbing wins over round trip
fidelity, which is why a replayed result carries no rate limit headers.

The key is the file name, so it never has to appear inside the file.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from crossfoot.llm.results import ChatResult, ChatUsage

CASSETTE_VERSION = 1
CASSETTE_SUFFIX = ".json"
REPLAY_LATENCY_MS = 0


class CassetteMissError(RuntimeError):
    """Raised in REPLAY mode when no cassette matches the request key."""


def request_key(
    *,
    model: str,
    messages: Sequence[Mapping[str, Any]],
    response_format: Mapping[str, Any] | None = None,
    temperature: float | None = None,
    image_digests: Sequence[str] = (),
) -> str:
    """sha256 over the mode-independent request: model, body, and image digests.

    The api key is not part of the request body, so rotating a key never
    invalidates a cassette.
    """
    body: dict[str, Any] = {
        "model": model,
        "messages": [dict(message) for message in messages],
        "response_format": dict(response_format) if response_format is not None else None,
        "temperature": temperature,
        "images": list(image_digests),
    }
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def save(directory: Path, key: str, result: ChatResult) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": CASSETTE_VERSION,
        "model": result.model,
        "content": result.content,
        "usage": {
            "prompt_tokens": result.usage.prompt_tokens,
            "completion_tokens": result.usage.completion_tokens,
            "total_tokens": result.usage.total_tokens,
        },
    }
    path(directory, key).write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def load(directory: Path, key: str) -> ChatResult:
    cassette = path(directory, key)
    if not cassette.is_file():
        raise CassetteMissError(f"no cassette {cassette.name} under {directory}")
    payload = json.loads(cassette.read_text(encoding="utf-8"))
    usage = payload["usage"]
    return ChatResult(
        content=str(payload["content"]),
        model=str(payload["model"]),
        usage=ChatUsage(
            prompt_tokens=int(usage["prompt_tokens"]),
            completion_tokens=int(usage["completion_tokens"]),
            total_tokens=int(usage["total_tokens"]),
        ),
        latency_ms=REPLAY_LATENCY_MS,
        rate_limit_headers={},
    )


def path(directory: Path, key: str) -> Path:
    return directory / f"{key}{CASSETTE_SUFFIX}"
