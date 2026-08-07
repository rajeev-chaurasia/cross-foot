"""Per provider pacing, and what happens when an allowance is spent for good.

Two failures from the 2026-08-06 run drive this. Gemini's free tier is 10
requests a minute and four concurrent workers walked straight through it, so 16
of roughly 31 calls came back 429 and the daily cap went with them. And a spent
daily quota kept being retried on every document, because a 429 that means
"gone" was handled like a 429 that means "slow down".

Offline: an httpx.MockTransport answers per provider host and the clock is
injected, so every wait is simulated rather than slept.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

import httpx
import pytest

from crossfoot.config import ProviderProfile
from crossfoot.constants import (
    PROVIDER_BASE_URLS,
    PROVIDER_DEFAULT_MODELS,
    PROVIDER_RATE_LIMITS,
    Provider,
)
from crossfoot.costs import CostLedger, Purpose
from crossfoot.evals.runner import ExtractionRun, VisionDegradations, run_notes
from crossfoot.llm.ratelimit import SECONDS_PER_MINUTE, RetryPolicy
from crossfoot.llm.results import ChatResult
from crossfoot.llm.spillover import (
    DEFAULT_COOLDOWN_SECONDS,
    QUOTA_COOLDOWN_SECONDS,
    SpilloverClient,
)

RUN_ID = "run-limits-01"
MESSAGES: list[dict[str, Any]] = [{"role": "user", "content": "read this statement"}]
SERVED = "from the pool"

OK = int(httpx.codes.OK)
TOO_MANY_REQUESTS = int(httpx.codes.TOO_MANY_REQUESTS)

# Gemini's own words on 2026-08-06, once the daily cap was spent.
QUOTA_BODY = "You exceeded your current quota, please check your plan and billing details."
# A 429 that means "slow down", which the ordinary retry path already handles.
RATE_BODY = "Too many requests, please retry shortly."

SLOW = Provider.GEMINI
FAST = Provider.MISTRAL
SLOW_RPM = PROVIDER_RATE_LIMITS[SLOW].requests_per_minute
FAST_RPM = PROVIDER_RATE_LIMITS[FAST].requests_per_minute

HOSTS: dict[Provider, str] = {
    provider: str(httpx.URL(base_url).host) for provider, base_url in PROVIDER_BASE_URLS.items()
}


class FakeClock:
    """Advances only when something asks it to sleep. Never blocks."""

    def __init__(self) -> None:
        self.current = 0.0
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.current

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.current += seconds

    def advance(self, seconds: float) -> None:
        self.current += seconds


class FakeApi:
    """Answers each provider host with one canned status and body."""

    def __init__(self, routes: dict[Provider, tuple[int, str]]) -> None:
        self._routes = {HOSTS[provider]: answer for provider, answer in routes.items()}
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        request.read()
        self.requests.append(request)
        answer = self._routes.get(str(request.url.host))
        assert answer is not None, f"unexpected host {request.url.host}"
        status, body = answer
        if status == OK:
            return httpx.Response(OK, json=_served_body())
        return httpx.Response(status, text=body)

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


SERVES: tuple[int, str] = (OK, "")


def _profile(provider: Provider) -> ProviderProfile:
    return ProviderProfile(
        name=provider,
        base_url=PROVIDER_BASE_URLS[provider],
        api_key=f"key-for-{provider.value}",
        model=PROVIDER_DEFAULT_MODELS[provider],
    )


def build_pool(
    api: FakeApi,
    book: CostLedger,
    clock: FakeClock,
    chain: tuple[Provider, ...],
) -> SpilloverClient:
    return SpilloverClient(
        profiles=[_profile(provider) for provider in chain],
        ledger=book,
        clock=clock,
        retry_policy=RetryPolicy(
            max_attempts=3, base_delay_seconds=0.5, max_delay_seconds=4.0, jitter_fraction=0.25
        ),
        cooldown_seconds=DEFAULT_COOLDOWN_SECONDS,
        transport=api.transport,
        timeout_seconds=5.0,
    )


async def call(pool: SpilloverClient, doc_id: str) -> ChatResult:
    return await pool.chat(MESSAGES, run_id=RUN_ID, doc_id=doc_id, purpose=Purpose.EXTRACT)


async def call_times(pool: SpilloverClient, count: int) -> None:
    for index in range(count):
        await call(pool, doc_id=f"doc-{index:03d}")


def ledger(tmp_path: Path) -> CostLedger:
    return CostLedger(tmp_path / "costs.db")


# Per profile rate limits.


def test_the_table_keeps_the_two_providers_this_file_contrasts_apart() -> None:
    # The premise of the pair below: one provider is paced tighter than the
    # other, so a global limiter and per profile limiters differ observably.
    assert SLOW_RPM < FAST_RPM


async def test_a_slow_provider_paces_itself_at_its_own_limit(tmp_path: Path) -> None:
    api = FakeApi({SLOW: SERVES})
    clock = FakeClock()
    pool = build_pool(api, ledger(tmp_path), clock, chain=(SLOW, FAST))

    await call_times(pool, SLOW_RPM + 1)

    assert api.calls(SLOW) == SLOW_RPM + 1
    # The burst is one minute of allowance; the request after it waits exactly
    # one refill at the provider's own rate.
    assert clock.sleeps == [pytest.approx(SECONDS_PER_MINUTE / SLOW_RPM)]


async def test_a_fast_provider_is_not_paced_by_a_slow_one_in_the_same_pool(
    tmp_path: Path,
) -> None:
    # One shared limiter set to the slowest member would have throttled this
    # exactly like the test above. Each profile carries its own instead.
    api = FakeApi({FAST: SERVES})
    clock = FakeClock()
    pool = build_pool(api, ledger(tmp_path), clock, chain=(FAST, SLOW))

    await call_times(pool, SLOW_RPM + 1)

    assert api.calls(FAST) == SLOW_RPM + 1
    assert clock.sleeps == []


# Quota exhaustion versus ordinary rationing.


async def test_a_quota_429_cools_the_provider_down_for_far_longer(tmp_path: Path) -> None:
    api = FakeApi({SLOW: (TOO_MANY_REQUESTS, QUOTA_BODY), FAST: SERVES})
    clock = FakeClock()
    pool = build_pool(api, ledger(tmp_path), clock, chain=(SLOW, FAST))

    result = await call(pool, doc_id="doc-000")

    assert result.content == SERVED
    # Retrying a spent allowance cannot refill it, so the attempt is not repeated.
    assert api.calls(SLOW) == 1
    assert pool.exhausted_providers == (SLOW,)

    # Long past the ordinary cooldown, still out of the chain.
    clock.advance(DEFAULT_COOLDOWN_SECONDS + 1.0)
    await call(pool, doc_id="doc-001")
    assert api.calls(SLOW) == 1

    # Sidelined, not banned: the quota cooldown does expire.
    clock.advance(QUOTA_COOLDOWN_SECONDS)
    await call(pool, doc_id="doc-002")
    assert api.calls(SLOW) == 2


async def test_an_ordinary_429_keeps_the_normal_retry_and_cooldown_path(
    tmp_path: Path,
) -> None:
    api = FakeApi({SLOW: (TOO_MANY_REQUESTS, RATE_BODY), FAST: SERVES})
    clock = FakeClock()
    pool = build_pool(api, ledger(tmp_path), clock, chain=(SLOW, FAST))

    result = await call(pool, doc_id="doc-000")

    assert result.content == SERVED
    # Every configured attempt is spent, because this one clears with time.
    assert api.calls(SLOW) == 3
    assert pool.exhausted_providers == ()

    clock.advance(DEFAULT_COOLDOWN_SECONDS + 1.0)
    await call(pool, doc_id="doc-001")
    assert api.calls(SLOW) == 6


# The run summary.


def _run(degradations: VisionDegradations) -> ExtractionRun:
    return ExtractionRun(
        documents=(), unprocessable=(), unserved=Counter(), degradations=degradations
    )


def test_the_run_summary_names_every_exhausted_provider() -> None:
    notes = run_notes(_run(VisionDegradations(quota_exhausted=(SLOW, FAST))))
    assert f"quota exhausted on {SLOW.value}, {FAST.value}" in notes


def test_a_run_that_exhausted_nothing_says_nothing() -> None:
    assert "quota exhausted" not in run_notes(_run(VisionDegradations()))
