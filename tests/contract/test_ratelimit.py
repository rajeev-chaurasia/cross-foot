"""Contract tests for the token bucket, retry backoff, and injected clock.

Written against docs/contracts-phase2.md before the implementation exists. The
clock is injected, so every wait here is simulated: the tests assert on the
recorded sleep durations, and a real sleep is made to fail rather than to pass
slowly.

Frozen shape of a backoff delay: the nominal wait is
min(base_delay_seconds * 2 ** (attempt - 1), max_delay_seconds), raised to the
Retry-After value when the provider sent one, and the returned delay lands in
[nominal, nominal * (1 + jitter_fraction)].
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

ratelimit = pytest.importorskip("crossfoot.llm.ratelimit")

SAMPLES = 50


def forbid_real_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Turn a real sleep into a failure, so bypassing the clock cannot pass slowly."""

    async def refuse(seconds: float) -> None:
        raise AssertionError(f"a real sleep of {seconds}s escaped the injected clock")

    monkeypatch.setattr(asyncio, "sleep", refuse)


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


def policy(
    max_attempts: int = 6,
    base_delay_seconds: float = 1.0,
    max_delay_seconds: float = 8.0,
    jitter_fraction: float = 0.25,
) -> Any:
    return ratelimit.RetryPolicy(
        max_attempts=max_attempts,
        base_delay_seconds=base_delay_seconds,
        max_delay_seconds=max_delay_seconds,
        jitter_fraction=jitter_fraction,
    )


def delay(attempt: int, retry_after_seconds: float | None = None) -> float:
    return float(policy().delay_for(attempt=attempt, retry_after_seconds=retry_after_seconds))


def limiter(clock: FakeClock, requests_per_minute: int, tokens_per_minute: int) -> Any:
    return ratelimit.RateLimiter(
        requests_per_minute=requests_per_minute,
        tokens_per_minute=tokens_per_minute,
        clock=clock,
    )


# Token bucket.


async def test_request_bucket_admits_the_configured_burst_then_blocks() -> None:
    clock = FakeClock()
    bucket = limiter(clock, requests_per_minute=3, tokens_per_minute=100_000)
    for _ in range(3):
        await bucket.acquire(tokens=1)
    assert clock.sleeps == []
    # A 4th request at 3 per minute waits 60 / 3 seconds for one refill.
    await bucket.acquire(tokens=1)
    assert sum(clock.sleeps) == pytest.approx(20.0, abs=0.001)


async def test_token_bucket_admits_the_configured_burst_then_blocks() -> None:
    clock = FakeClock()
    bucket = limiter(clock, requests_per_minute=100_000, tokens_per_minute=600)
    await bucket.acquire(tokens=600)
    assert clock.sleeps == []
    # 600 per minute refills 10 per second, so 300 more tokens need 30 seconds.
    await bucket.acquire(tokens=300)
    assert sum(clock.sleeps) == pytest.approx(30.0, abs=0.001)


async def test_waiting_out_the_bucket_lets_the_next_request_through_free() -> None:
    clock = FakeClock()
    bucket = limiter(clock, requests_per_minute=3, tokens_per_minute=100_000)
    for _ in range(3):
        await bucket.acquire(tokens=1)
    clock.advance(60.0)
    await bucket.acquire(tokens=1)
    assert clock.sleeps == []


async def test_the_two_buckets_are_independent() -> None:
    clock = FakeClock()
    bucket = limiter(clock, requests_per_minute=60, tokens_per_minute=60)
    # Both buckets refill at 1 per second. One 60 token request empties the
    # token bucket while the request bucket still holds 59.
    await bucket.acquire(tokens=60)
    assert clock.sleeps == []
    await bucket.acquire(tokens=30)
    assert sum(clock.sleeps) == pytest.approx(30.0, abs=0.001)


# Retry-After and backoff.


def test_retry_after_is_waited_out_with_bounded_jitter() -> None:
    for _ in range(SAMPLES):
        value = delay(attempt=1, retry_after_seconds=7.0)
        assert 7.0 <= value <= 8.75  # 7 * 1.25


def test_retry_after_wins_over_a_smaller_exponential_delay() -> None:
    # Nominal at attempt 2 is 2 seconds, so Retry-After sets the floor and the
    # jitter ceiling rides on it rather than on the exponential value.
    for _ in range(SAMPLES):
        value = delay(attempt=2, retry_after_seconds=30.0)
        assert 30.0 <= value <= 37.5  # 30 * 1.25


def test_exponential_backoff_grows_across_attempts() -> None:
    # Nominal 1, 2, 4 seconds. At most 25 percent jitter keeps them ordered
    # because 2 ** n * 1.25 stays below 2 ** (n + 1).
    first, second, third = (delay(attempt=n) for n in (1, 2, 3))
    assert 1.0 <= first <= 1.25
    assert 2.0 <= second <= 2.5
    assert 4.0 <= third <= 5.0
    assert first < second < third


def test_exponential_backoff_is_capped() -> None:
    # Doubling would reach 8, 16, 32, 64 at attempts 4 through 7; the cap is 8.
    for attempt in (4, 5, 6, 7):
        assert 8.0 <= delay(attempt=attempt) <= 10.0


def test_backoff_jitter_varies_between_calls() -> None:
    values = {delay(attempt=3) for _ in range(SAMPLES)}
    assert len(values) > 1


# The attempt ceiling is a number the retry loop reads, so it is asserted where
# the loop runs: tests/contract/test_llm_spillover.py.


# The injected clock.


async def test_every_wait_goes_to_the_injected_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    forbid_real_sleep(monkeypatch)
    clock = FakeClock()
    bucket = limiter(clock, requests_per_minute=2, tokens_per_minute=100_000)
    for _ in range(12):
        await bucket.acquire(tokens=1)
    # The first 2 are free; the other 10 wait 30 seconds each, one wait apiece.
    assert clock.sleeps == [pytest.approx(30.0, abs=0.001)] * 10
    assert clock.now() == pytest.approx(300.0, abs=0.001)
