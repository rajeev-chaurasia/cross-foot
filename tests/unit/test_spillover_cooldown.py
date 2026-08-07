"""Waiting a pool-wide cooldown out instead of burning the rest of a batch.

One document that exhausts every provider puts them all into a cooldown at
once, so every document behind it used to fail instantly and be marked
unprocessable. A two minute outage would quietly destroy a slice of a 220
document run, and the scorecard would read as an extraction quality problem
rather than an infrastructure blip. A long batch job waits instead.

Offline: an httpx.MockTransport answers per provider host and the clock is
virtual, so every 300 second wait is simulated.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest
from virtual_clock import VirtualClock

from crossfoot.config import ProviderProfile
from crossfoot.constants import PROVIDER_BASE_URLS, PROVIDER_DEFAULT_MODELS, Provider
from crossfoot.costs import CostLedger, Purpose
from crossfoot.llm.ratelimit import RetryPolicy
from crossfoot.llm.results import ChatResult
from crossfoot.llm.spillover import (
    MAX_COOLDOWN_WAIT_SECONDS,
    AllProvidersFailedError,
    SpilloverClient,
)

RUN_ID = "run-cooldown-01"
DOC_ID = "doc-01"
NEXT_DOC_ID = "doc-02"
MESSAGES: list[dict[str, Any]] = [{"role": "user", "content": "read this statement"}]

CHAIN = (Provider.GEMINI, Provider.GROQ)
COOLDOWN_SECONDS = 300.0
OK = int(httpx.codes.OK)
# A spent free allowance, which spills over at once and never retries, so the
# only sleeps these tests record are the cooldown waits themselves.
SPENT = int(httpx.codes.PAYMENT_REQUIRED)
SERVED = "from the pool"

HOSTS: dict[Provider, str] = {
    provider: str(httpx.URL(PROVIDER_BASE_URLS[provider]).host) for provider in CHAIN
}


class ScriptedApi:
    """Answers per provider host from a status script, repeating its last entry."""

    def __init__(self, script: dict[Provider, Sequence[int]]) -> None:
        self._script = {HOSTS[provider]: list(codes) for provider, codes in script.items()}
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        request.read()
        self.requests.append(request)
        codes = self._script.get(str(request.url.host))
        assert codes is not None, f"unexpected host {request.url.host}"
        status = codes.pop(0) if len(codes) > 1 else codes[0]
        if status == OK:
            return httpx.Response(OK, json=_served_body())
        return httpx.Response(status, text="provider said no")

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)

    def calls(self, provider: Provider) -> int:
        return sum(1 for request in self.requests if str(request.url.host) == HOSTS[provider])


def _served_body() -> dict[str, Any]:
    return {
        "model": "served",
        "choices": [{"message": {"role": "assistant", "content": SERVED}}],
        "usage": {"prompt_tokens": 8, "completion_tokens": 1, "total_tokens": 68},
    }


def _profile(provider: Provider) -> ProviderProfile:
    return ProviderProfile(
        name=provider,
        base_url=PROVIDER_BASE_URLS[provider],
        api_key=f"key-for-{provider.value}",
        model=PROVIDER_DEFAULT_MODELS[provider],
    )


def build_pool(
    api: ScriptedApi, book: CostLedger, clock: VirtualClock, *, wait_for_cooldown: bool = False
) -> SpilloverClient:
    return SpilloverClient(
        profiles=[_profile(provider) for provider in CHAIN],
        ledger=book,
        clock=clock,
        retry_policy=RetryPolicy(
            max_attempts=3, base_delay_seconds=0.5, max_delay_seconds=4.0, jitter_fraction=0.25
        ),
        cooldown_seconds=COOLDOWN_SECONDS,
        transport=api.transport,
        timeout_seconds=5.0,
        wait_for_cooldown=wait_for_cooldown,
    )


async def call(pool: SpilloverClient, doc_id: str = DOC_ID) -> ChatResult:
    return await pool.chat(MESSAGES, run_id=RUN_ID, doc_id=doc_id, purpose=Purpose.EXTRACT)


def ledger(tmp_path: Path) -> CostLedger:
    return CostLedger(tmp_path / "costs.db")


async def test_a_cooling_down_pool_waits_the_earliest_expiry_out_and_serves(
    tmp_path: Path,
) -> None:
    # Both allowances are spent, then the outage clears while the pool waits.
    api = ScriptedApi({Provider.GEMINI: (SPENT, OK), Provider.GROQ: (SPENT, OK)})
    clock = VirtualClock()
    pool = build_pool(api, ledger(tmp_path), clock, wait_for_cooldown=True)

    result = await clock.run(call(pool))

    assert result.content == SERVED
    assert clock.sleeps == [COOLDOWN_SECONDS]
    assert clock.now() == pytest.approx(COOLDOWN_SECONDS)
    assert api.calls(Provider.GEMINI) == 2


async def test_the_default_still_fails_the_moment_every_provider_fails(tmp_path: Path) -> None:
    # The frozen contract: an interactive caller wants the failure now.
    api = ScriptedApi({Provider.GEMINI: (SPENT, OK), Provider.GROQ: (SPENT, OK)})
    clock = VirtualClock()
    pool = build_pool(api, ledger(tmp_path), clock)

    with pytest.raises(AllProvidersFailedError):
        await clock.run(call(pool))

    assert clock.sleeps == []
    assert api.calls(Provider.GEMINI) == 1
    assert api.calls(Provider.GROQ) == 1


async def test_the_document_behind_an_exhausted_one_waits_instead_of_being_lost(
    tmp_path: Path,
) -> None:
    # Long enough to outlast the bounded wait, so the first document is lost the
    # way a real outage loses one, and the second arrives to a cold pool.
    api = ScriptedApi({Provider.GEMINI: (SPENT, SPENT, SPENT, SPENT, OK), Provider.GROQ: (SPENT,)})
    clock = VirtualClock()
    pool = build_pool(api, ledger(tmp_path), clock, wait_for_cooldown=True)

    with pytest.raises(AllProvidersFailedError):
        await clock.run(call(pool))
    lost_at = clock.now()

    result = await clock.run(call(pool, doc_id=NEXT_DOC_ID))

    assert result.content == SERVED
    # The second document slept out the cooldown the first one left behind
    # rather than being marked unprocessable the instant it started.
    assert clock.now() == pytest.approx(lost_at + COOLDOWN_SECONDS)


async def test_a_dead_pool_gives_up_after_the_bounded_total_wait(tmp_path: Path) -> None:
    api = ScriptedApi({Provider.GEMINI: (SPENT,), Provider.GROQ: (SPENT,)})
    clock = VirtualClock()
    pool = build_pool(api, ledger(tmp_path), clock, wait_for_cooldown=True)

    with pytest.raises(AllProvidersFailedError):
        await clock.run(call(pool))

    waits = int(MAX_COOLDOWN_WAIT_SECONDS // COOLDOWN_SECONDS)
    assert clock.sleeps == [COOLDOWN_SECONDS] * waits
    assert clock.now() == pytest.approx(MAX_COOLDOWN_WAIT_SECONDS)
    # One walk of the chain per wait, plus the walk that broke the budget.
    assert api.calls(Provider.GEMINI) == waits + 1
