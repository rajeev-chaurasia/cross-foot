"""Spillover across provider free tiers.

The pool walks its profiles in order. A profile that answers wins; a profile
that runs out of allowance, dies, or refuses to be retried is left to cool down
and the next one is tried. Every attempt reaches the ledger, so the scorecard
can say which documents the primary model did not extract.

The attempt counter restarts per profile: three exhausted attempts on the
primary followed by a fallback that answers records attempts [1, 2, 3, 1].
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import httpx

from crossfoot.config import ProviderProfile
from crossfoot.constants import LlmMode, Provider
from crossfoot.costs import CallContext, CostLedger, Purpose
from crossfoot.llm.cache import ResponseCache
from crossfoot.llm.client import DEFAULT_TIMEOUT_SECONDS, LlmClient
from crossfoot.llm.ratelimit import (
    Clock,
    MonotonicClock,
    RateLimiter,
    RetryPolicy,
    estimate_tokens,
    limiter_for,
)
from crossfoot.llm.results import ChatResult, LlmError, PageImage

DEFAULT_COOLDOWN_SECONDS = 300.0
DEFAULT_RETRY_POLICY = RetryPolicy(
    max_attempts=3,
    base_delay_seconds=1.0,
    max_delay_seconds=30.0,
    jitter_fraction=0.25,
)

PAYMENT_REQUIRED = int(httpx.codes.PAYMENT_REQUIRED)
TOO_MANY_REQUESTS = int(httpx.codes.TOO_MANY_REQUESTS)
SERVER_ERROR_FLOOR = int(httpx.codes.INTERNAL_SERVER_ERROR)


class AllProvidersFailedError(RuntimeError):
    """Raised when every configured profile failed or is still cooling down."""


class SpilloverClient:
    def __init__(
        self,
        *,
        profiles: Sequence[ProviderProfile],
        ledger: CostLedger,
        clock: Clock | None = None,
        retry_policy: RetryPolicy = DEFAULT_RETRY_POLICY,
        cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        mode: LlmMode = LlmMode.LIVE,
        cassette_dir: Path | None = None,
        cache: ResponseCache | None = None,
        rate_limiters: Mapping[Provider, RateLimiter] | None = None,
    ) -> None:
        self._profiles = tuple(profiles)
        self._clock = MonotonicClock() if clock is None else clock
        self._retry_policy = retry_policy
        self._cooldown_seconds = cooldown_seconds
        self._clients = {
            profile.name: LlmClient(
                profile,
                timeout_seconds,
                mode=mode,
                cassette_dir=cassette_dir,
                ledger=ledger,
                cache=cache,
                transport=transport,
            )
            for profile in self._profiles
        }
        self._limiters = (
            {profile.name: limiter_for(profile.name, self._clock) for profile in self._profiles}
            if rate_limiters is None
            else dict(rate_limiters)
        )
        self._cooldown_until: dict[Provider, float] = {}

    async def chat(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        run_id: str,
        doc_id: str,
        purpose: Purpose,
        images: Sequence[PageImage] = (),
        response_format: Mapping[str, Any] | None = None,
        temperature: float | None = None,
    ) -> ChatResult:
        failures: list[str] = []
        for profile in self._profiles:
            if self._cooling_down(profile.name):
                continue
            outcome = await self._try_profile(
                profile,
                messages,
                images=images,
                response_format=response_format,
                temperature=temperature,
                run_id=run_id,
                doc_id=doc_id,
                purpose=purpose,
            )
            if isinstance(outcome, ChatResult):
                return outcome
            self._cooldown_until[profile.name] = self._clock.now() + self._cooldown_seconds
            failures.append(f"{profile.name}: {outcome}")
        raise AllProvidersFailedError(
            "; ".join(failures) if failures else "every profile is cooling down"
        )

    async def _try_profile(
        self,
        profile: ProviderProfile,
        messages: Sequence[Mapping[str, Any]],
        *,
        images: Sequence[PageImage],
        response_format: Mapping[str, Any] | None,
        temperature: float | None,
        run_id: str,
        doc_id: str,
        purpose: Purpose,
    ) -> ChatResult | LlmError:
        """One profile until it answers or gives up; the error explains giving up."""
        client = self._clients[profile.name]
        limiter = self._limiters.get(profile.name)
        last_error = LlmError(f"{profile.model} was never called")
        for attempt in range(1, self._retry_policy.max_attempts + 1):
            if limiter is not None:
                await limiter.acquire(tokens=estimate_tokens(_prompt_text(messages), len(images)))
            context = CallContext(run_id=run_id, doc_id=doc_id, purpose=purpose, attempt=attempt)
            try:
                if images:
                    return await client.chat_vision(
                        messages,
                        images,
                        response_format=response_format,
                        temperature=temperature,
                        context=context,
                    )
                return await client.chat(
                    messages,
                    response_format=response_format,
                    temperature=temperature,
                    context=context,
                )
            except LlmError as error:
                if not _spills_over(error.status_code):
                    # A bad request is the caller's fault; another free tier
                    # would only burn a second allowance on the same call.
                    raise
                last_error = error
                if error.status_code == PAYMENT_REQUIRED:
                    break
                if attempt < self._retry_policy.max_attempts:
                    await self._clock.sleep(
                        self._retry_policy.delay_for(
                            attempt=attempt, retry_after_seconds=error.retry_after_seconds
                        )
                    )
        return last_error

    def _cooling_down(self, provider: Provider) -> bool:
        until = self._cooldown_until.get(provider)
        return until is not None and self._clock.now() < until


def _prompt_text(messages: Sequence[Mapping[str, Any]]) -> str:
    return " ".join(str(message.get("content", "")) for message in messages)


def _spills_over(status: int | None) -> bool:
    """Out of allowance, refused for payment, or broken: try the next provider."""
    if status is None:
        return True
    return status in (PAYMENT_REQUIRED, TOO_MANY_REQUESTS) or status >= SERVER_ERROR_FLOOR
