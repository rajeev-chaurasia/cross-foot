"""Spillover across provider free tiers.

The pool walks its profiles in order. A profile that answers wins; a profile
that runs out of allowance, dies, or refuses to be retried is left to cool down
and the next one is tried. Every attempt reaches the ledger, so the scorecard
can say which documents the primary model did not extract.

The attempt counter restarts per profile: three exhausted attempts on the
primary followed by a fallback that answers records attempts [1, 2, 3, 1].

Text and vision share one attempt loop. A live run proved why: the vision call
bypassed this module entirely, so a single transient 503 on the second k=2
sample ended a whole extraction run.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
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

# Rationing and provider faults both clear with time, so these earn a backoff on
# the same profile before the pool moves on.
RETRYABLE_STATUSES: frozenset[int] = frozenset(
    {
        TOO_MANY_REQUESTS,
        int(httpx.codes.INTERNAL_SERVER_ERROR),
        int(httpx.codes.BAD_GATEWAY),
        int(httpx.codes.SERVICE_UNAVAILABLE),
        int(httpx.codes.GATEWAY_TIMEOUT),
    }
)

# A spent free allowance does not refill on a retry: Cerebras answers 402 the
# moment it is gone, so the next profile is the only useful move.
IMMEDIATE_SPILLOVER_STATUSES: frozenset[int] = frozenset({PAYMENT_REQUIRED})


class FailureAction(StrEnum):
    """What a failed attempt earns."""

    RETRY = "retry"  # back off on the same profile, then spill over
    SPILL = "spill"  # the next profile, with no retry
    RAISE = "raise"  # no provider can fix this call


def action_for(status: int | None) -> FailureAction:
    """Classify one failed attempt by its HTTP status.

    A transport failure carries no status and is treated like a 5xx. Every
    other 4xx is a malformed request, so retrying it wastes time and spilling
    it over wastes three more free allowances on the same broken call.
    """
    if status is None or status in RETRYABLE_STATUSES:
        return FailureAction.RETRY
    if status in IMMEDIATE_SPILLOVER_STATUSES:
        return FailureAction.SPILL
    return FailureAction.RAISE


class AllProvidersFailedError(LlmError):
    """Raised when every configured profile failed or is still cooling down."""


@dataclass(frozen=True)
class _Call:
    """One logical call, unchanged across every retry and every profile."""

    messages: tuple[Mapping[str, Any], ...]
    images: tuple[PageImage, ...]
    response_format: Mapping[str, Any] | None
    temperature: float | None
    context: CallContext | None

    def context_at(self, attempt: int) -> CallContext | None:
        """The ledger context for one attempt; attempt counts within a profile."""
        return None if self.context is None else replace(self.context, attempt=attempt)

    def estimated_tokens(self) -> int:
        text = " ".join(str(message.get("content", "")) for message in self.messages)
        return estimate_tokens(text, len(self.images))


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
        return await self._serve(
            _Call(
                messages=tuple(messages),
                images=tuple(images),
                response_format=response_format,
                temperature=temperature,
                context=CallContext(run_id=run_id, doc_id=doc_id, purpose=purpose),
            )
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
        """The vision twin of chat, matching the extractor's client protocol.

        Same limiter, retry policy, cooldown, ledger, and profile walk; the
        caller supplies the ledger context because the extractor already owns
        the run, document, and purpose.
        """
        return await self._serve(
            _Call(
                messages=tuple(messages),
                images=tuple(images),
                response_format=response_format,
                temperature=temperature,
                context=context,
            )
        )

    async def _serve(self, call: _Call) -> ChatResult:
        """Walk the profiles until one answers; the shared path for both methods."""
        failures: list[str] = []
        for profile in self._profiles:
            if self._cooling_down(profile.name):
                continue
            outcome = await self._try_profile(profile, call)
            if isinstance(outcome, ChatResult):
                return outcome
            self._cooldown_until[profile.name] = self._clock.now() + self._cooldown_seconds
            failures.append(f"{profile.name}: {outcome}")
        raise AllProvidersFailedError(
            "; ".join(failures) if failures else "every profile is cooling down"
        )

    async def _try_profile(self, profile: ProviderProfile, call: _Call) -> ChatResult | LlmError:
        """One profile until it answers or gives up; the error explains giving up."""
        client = self._clients[profile.name]
        limiter = self._limiters.get(profile.name)
        last_error = LlmError(f"{profile.model} was never called")
        for attempt in range(1, self._retry_policy.max_attempts + 1):
            if limiter is not None:
                await limiter.acquire(tokens=call.estimated_tokens())
            try:
                return await _dispatch(client, call, attempt)
            except LlmError as error:
                action = action_for(error.status_code)
                if action is FailureAction.RAISE:
                    raise
                last_error = error
                if action is FailureAction.SPILL:
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


async def _dispatch(client: LlmClient, call: _Call, attempt: int) -> ChatResult:
    """The single place a call reaches the client, so no path can drift."""
    context = call.context_at(attempt)
    if call.images:
        return await client.chat_vision(
            call.messages,
            call.images,
            response_format=call.response_format,
            temperature=call.temperature,
            context=context,
        )
    return await client.chat(
        call.messages,
        response_format=call.response_format,
        temperature=call.temperature,
        context=context,
    )
