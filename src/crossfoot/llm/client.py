"""Single async OpenAI-compatible chat client.

Every LLM call in the project goes through this module. A client is bound to
one ProviderProfile; the caller decides priority and spillover across
profiles. Record/replay, caching, and checkpointing arrive in Phase 2 behind
the same interface.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import httpx

from crossfoot.config import ProviderProfile
from crossfoot.constants import CHAT_COMPLETIONS_PATH, RATE_LIMIT_HEADER_MARKERS

DEFAULT_TIMEOUT_SECONDS = 120.0


@dataclass(frozen=True)
class ChatUsage:
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


@dataclass(frozen=True)
class ChatResult:
    content: str
    model: str
    usage: ChatUsage
    latency_ms: int
    rate_limit_headers: dict[str, str]


class LlmError(RuntimeError):
    """Raised when the provider returns a non-success response."""


def _rate_limit_headers(headers: httpx.Headers) -> dict[str, str]:
    return {
        name: value
        for name, value in headers.items()
        if any(marker in name.lower() for marker in RATE_LIMIT_HEADER_MARKERS)
    }


class LlmClient:
    def __init__(
        self, profile: ProviderProfile, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    ) -> None:
        self._profile = profile
        self._timeout_seconds = timeout_seconds

    async def chat(self, messages: list[dict[str, Any]]) -> ChatResult:
        url = self._profile.base_url.rstrip("/") + CHAT_COMPLETIONS_PATH
        payload = {"model": self._profile.model, "messages": messages}
        headers = {"Authorization": f"Bearer {self._profile.api_key}"}
        started = time.monotonic()
        async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
            response = await client.post(url, json=payload, headers=headers)
        latency_ms = int((time.monotonic() - started) * 1000)
        if response.status_code != httpx.codes.OK:
            raise LlmError(f"{response.status_code} from {url}: {response.text[:300]}")
        body = response.json()
        usage = body.get("usage") or {}
        return ChatResult(
            content=body["choices"][0]["message"]["content"] or "",
            model=body.get("model", self._profile.model),
            usage=ChatUsage(
                prompt_tokens=int(usage.get("prompt_tokens", 0)),
                completion_tokens=int(usage.get("completion_tokens", 0)),
                total_tokens=int(usage.get("total_tokens", 0)),
            ),
            latency_ms=latency_ms,
            rate_limit_headers=_rate_limit_headers(response.headers),
        )
