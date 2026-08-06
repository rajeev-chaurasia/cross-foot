"""Single async OpenAI-compatible chat client.

Every LLM call in the project goes through this module. A client is bound to
one ProviderProfile; the caller decides priority and spillover across profiles.
Record and replay, the response cache, and ledger accounting all sit behind the
same two methods, so a caller never learns where an answer came from.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import httpx

from crossfoot.config import ProviderProfile
from crossfoot.constants import CHAT_COMPLETIONS_PATH, RATE_LIMIT_HEADER_MARKERS, LlmMode
from crossfoot.costs import FREE_TIER_ACTUAL_COST_MICROUSD, CallContext, CostLedger
from crossfoot.llm import cassettes
from crossfoot.llm.cache import ResponseCache
from crossfoot.llm.results import ChatResult, ChatUsage, LlmError, PageImage

__all__ = [
    "ChatResult",
    "ChatUsage",
    "LlmClient",
    "LlmError",
    "PageImage",
]

DEFAULT_TIMEOUT_SECONDS = 120.0
# Enough of a failing body to diagnose, not enough to flood a log line.
ERROR_BODY_CHARS = 300
# No HTTP status was reached: a timeout, a DNS failure, a refused connection.
TRANSPORT_FAILURE_STATUS = 0

USER_ROLE = "user"
# OpenAI content parts name their payload key after the part type, so each of
# these serves as both the "type" value and the key beside it.
TEXT_PART = "text"
IMAGE_PART = "image_url"

NO_USAGE = ChatUsage(prompt_tokens=0, completion_tokens=0, total_tokens=0)


class LlmClient:
    def __init__(
        self,
        profile: ProviderProfile,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        *,
        mode: LlmMode = LlmMode.LIVE,
        cassette_dir: Path | None = None,
        ledger: CostLedger | None = None,
        cache: ResponseCache | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._profile = profile
        self._timeout_seconds = timeout_seconds
        self._mode = mode
        self._cassette_dir = cassette_dir
        self._ledger = ledger
        self._cache = cache
        # Test seam only: production paths leave this None and get the default
        # httpx transport.
        self._transport = transport

    async def chat(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        response_format: Mapping[str, Any] | None = None,
        temperature: float | None = None,
        context: CallContext | None = None,
    ) -> ChatResult:
        return await self._complete(
            messages,
            images=(),
            response_format=response_format,
            temperature=temperature,
            context=context,
        )

    async def chat_vision(
        self,
        messages: Sequence[Mapping[str, Any]],
        images: Sequence[PageImage],
        *,
        response_format: Mapping[str, Any] | None = None,
        temperature: float | None = None,
        context: CallContext | None = None,
    ) -> ChatResult:
        return await self._complete(
            messages,
            images=tuple(sorted(images, key=lambda image: image.page_index)),
            response_format=response_format,
            temperature=temperature,
            context=context,
        )

    async def _complete(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        images: Sequence[PageImage],
        response_format: Mapping[str, Any] | None,
        temperature: float | None,
        context: CallContext | None,
    ) -> ChatResult:
        key = cassettes.request_key(
            model=self._profile.model,
            messages=messages,
            response_format=response_format,
            temperature=temperature,
            image_digests=[image.digest() for image in images],
        )
        if self._mode is LlmMode.REPLAY:
            replayed = cassettes.load(self._cassette_directory(), key)
            self._record(
                context, usage=replayed.usage, latency_ms=replayed.latency_ms, cached=False
            )
            return replayed
        if self._cache is not None:
            hit = self._cache.get(key)
            if hit is not None:
                self._record(context, usage=NO_USAGE, latency_ms=hit.latency_ms, cached=True)
                return hit
        result = await self._post(
            self._payload(messages, images, response_format, temperature), context
        )
        if self._cache is not None:
            self._cache.put(key, result)
        if self._mode is LlmMode.RECORD:
            cassettes.save(self._cassette_directory(), key, result)
        return result

    async def _post(self, payload: dict[str, Any], context: CallContext | None) -> ChatResult:
        url = self._profile.base_url.rstrip("/") + CHAT_COMPLETIONS_PATH
        headers = {"Authorization": f"Bearer {self._profile.api_key}"}
        started = time.monotonic()
        async with httpx.AsyncClient(
            timeout=self._timeout_seconds, transport=self._transport
        ) as client:
            try:
                response = await client.post(url, json=payload, headers=headers)
            except httpx.HTTPError as error:
                self._record(
                    context,
                    usage=NO_USAGE,
                    latency_ms=_elapsed_ms(started),
                    cached=False,
                    http_status=TRANSPORT_FAILURE_STATUS,
                )
                raise LlmError(f"no response from {url}: {error}") from error
        latency_ms = _elapsed_ms(started)
        status = int(response.status_code)
        if status != httpx.codes.OK:
            self._record(
                context, usage=NO_USAGE, latency_ms=latency_ms, cached=False, http_status=status
            )
            raise LlmError(
                f"{status} from {url}: {response.text[:ERROR_BODY_CHARS]}",
                status_code=status,
                retry_after_seconds=_retry_after_seconds(response.headers),
            )
        body = response.json()
        choices = body.get("choices")
        if not choices:
            # Some providers report the failure inside a 200 body.
            self._record(
                context, usage=NO_USAGE, latency_ms=latency_ms, cached=False, http_status=status
            )
            raise LlmError(
                f"no choices from {url}: {str(body)[:ERROR_BODY_CHARS]}", status_code=status
            )
        usage = body.get("usage") or {}
        result = ChatResult(
            content=choices[0]["message"]["content"] or "",
            model=body.get("model", self._profile.model),
            # Recorded as reported: hidden reasoning tokens make the total more
            # than the sum of its parts, so recomputing it understates cost.
            usage=ChatUsage(
                prompt_tokens=int(usage.get("prompt_tokens", 0)),
                completion_tokens=int(usage.get("completion_tokens", 0)),
                total_tokens=int(usage.get("total_tokens", 0)),
            ),
            latency_ms=latency_ms,
            rate_limit_headers=_rate_limit_headers(response.headers),
        )
        self._record(context, usage=result.usage, latency_ms=latency_ms, cached=False)
        return result

    def _payload(
        self,
        messages: Sequence[Mapping[str, Any]],
        images: Sequence[PageImage],
        response_format: Mapping[str, Any] | None,
        temperature: float | None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self._profile.model,
            "messages": _with_images(messages, images),
        }
        if response_format is not None:
            payload["response_format"] = dict(response_format)
        if temperature is not None:
            payload["temperature"] = temperature
        return payload

    def _cassette_directory(self) -> Path:
        if self._cassette_dir is None:
            raise LlmError(f"mode {self._mode} needs a cassette_dir")
        return self._cassette_dir

    def _record(
        self,
        context: CallContext | None,
        *,
        usage: ChatUsage,
        latency_ms: int,
        cached: bool,
        http_status: int = int(httpx.codes.OK),
    ) -> None:
        if self._ledger is None or context is None:
            return
        self._ledger.record(
            run_id=context.run_id,
            doc_id=context.doc_id,
            purpose=context.purpose,
            provider=self._profile.name,
            model=self._profile.model,
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            total_tokens=usage.total_tokens,
            cached=cached,
            latency_ms=latency_ms,
            http_status=http_status,
            attempt=context.attempt,
            actual_cost_microusd=FREE_TIER_ACTUAL_COST_MICROUSD,
        )


def _with_images(
    messages: Sequence[Mapping[str, Any]], images: Sequence[PageImage]
) -> list[dict[str, Any]]:
    """Copy the messages, attaching image parts to the last user message."""
    copied = [dict(message) for message in messages]
    if not images:
        return copied
    index = _last_user_index(copied)
    parts = _content_parts(copied[index].get("content"))
    parts.extend({"type": IMAGE_PART, IMAGE_PART: {"url": image.data_uri()}} for image in images)
    copied[index]["content"] = parts
    return copied


def _last_user_index(messages: list[dict[str, Any]]) -> int:
    for index in reversed(range(len(messages))):
        if messages[index].get("role") == USER_ROLE:
            return index
    messages.append({"role": USER_ROLE, "content": []})
    return len(messages) - 1


def _content_parts(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"type": TEXT_PART, TEXT_PART: content}]
    if isinstance(content, list):
        return [dict(part) for part in content]
    return []


def _rate_limit_headers(headers: httpx.Headers) -> dict[str, str]:
    return {
        name: value
        for name, value in headers.items()
        if any(marker in name.lower() for marker in RATE_LIMIT_HEADER_MARKERS)
    }


def _retry_after_seconds(headers: httpx.Headers) -> float | None:
    raw = headers.get("retry-after")
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        # The HTTP-date form exists; backoff falls through to the policy.
        return None


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
