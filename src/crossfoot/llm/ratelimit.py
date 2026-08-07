"""Token buckets, retry backoff, and the injected clock they both use.

Buckets refill continuously and hold at most one minute of allowance, so a
burst is bounded by the per-minute rate. The clock is a protocol, so tests
simulate every wait instead of sleeping through it.
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass
from typing import Protocol

from crossfoot.constants import PROVIDER_RATE_LIMITS, Provider, RateLimit

SECONDS_PER_MINUTE = 60.0
# Below this a wait is float noise from the previous refill, not a real wait.
WAIT_EPSILON_SECONDS = 1e-9

CHARS_PER_TOKEN = 4
# One rasterized statement page, bounded by the vision path's longest-edge cap.
IMAGE_TOKEN_ESTIMATE = 1_500

# For a provider with no entry in the table, which today cannot happen.
FALLBACK_RATE_LIMIT = RateLimit(requests_per_minute=10, tokens_per_minute=100_000)


class Clock(Protocol):
    def now(self) -> float: ...

    async def sleep(self, seconds: float) -> None: ...


class MonotonicClock:
    """The production clock: monotonic time and a real asyncio sleep."""

    def now(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class _Bucket:
    """Continuous-refill bucket whose capacity equals the per-minute rate."""

    def __init__(self, per_minute: int, clock: Clock) -> None:
        if per_minute <= 0:
            raise ValueError("a per-minute rate must be positive")
        self._capacity = float(per_minute)
        self._per_second = per_minute / SECONDS_PER_MINUTE
        self._available = float(per_minute)
        self._clock = clock
        self._updated = clock.now()

    @property
    def capacity(self) -> float:
        return self._capacity

    def wait_seconds(self, amount: float) -> float:
        self._refill()
        deficit = amount - self._available
        if deficit <= WAIT_EPSILON_SECONDS:
            return 0.0
        return deficit / self._per_second

    def take(self, amount: float) -> None:
        self._available -= amount

    def _refill(self) -> None:
        now = self._clock.now()
        elapsed = now - self._updated
        self._available = min(self._capacity, self._available + elapsed * self._per_second)
        self._updated = now


class RateLimiter:
    """Requests per minute and tokens per minute, enforced as two buckets.

    One limiter is shared by every concurrent worker, so it is the single place
    that decides when a request may go out.
    """

    def __init__(self, *, requests_per_minute: int, tokens_per_minute: int, clock: Clock) -> None:
        self._requests = _Bucket(requests_per_minute, clock)
        self._tokens = _Bucket(tokens_per_minute, clock)
        self._clock = clock
        # Checking a bucket and taking from it straddles a sleep, so concurrent
        # callers must not interleave: without this every waiter wakes together
        # and they all spend the one token that refilled.
        self._gate = asyncio.Lock()

    async def acquire(self, *, tokens: int = 1) -> None:
        # A request bigger than a full bucket could never fit, so it waits for a
        # full bucket and leaves the real ceiling to the provider.
        cost = min(float(tokens), self._tokens.capacity)
        async with self._gate:
            while True:
                wait = max(self._requests.wait_seconds(1.0), self._tokens.wait_seconds(cost))
                if wait <= 0.0:
                    break
                await self._clock.sleep(wait)
            self._requests.take(1.0)
            self._tokens.take(cost)


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int
    base_delay_seconds: float
    max_delay_seconds: float
    jitter_fraction: float

    def delay_for(self, *, attempt: int, retry_after_seconds: float | None = None) -> float:
        """Nominal delay, raised to Retry-After, then jittered upward only.

        Jitter comes from the process RNG on purpose: retry timing must differ
        between concurrent callers, and it never feeds dataset determinism.
        """
        nominal = min(self.base_delay_seconds * 2.0 ** (attempt - 1), self.max_delay_seconds)
        if retry_after_seconds is not None:
            nominal = max(nominal, retry_after_seconds)
        return nominal * (1.0 + random.random() * self.jitter_fraction)


def limiter_for(provider: Provider, clock: Clock) -> RateLimiter:
    """One provider's own limiter, so a slow provider never paces a fast one."""
    limit = PROVIDER_RATE_LIMITS.get(provider, FALLBACK_RATE_LIMIT)
    return RateLimiter(
        requests_per_minute=limit.requests_per_minute,
        tokens_per_minute=limit.tokens_per_minute,
        clock=clock,
    )


def estimate_tokens(text: str, image_count: int = 0) -> int:
    """Pre-call estimate, since the true count arrives only with the response."""
    return max(1, len(text) // CHARS_PER_TOKEN) + image_count * IMAGE_TOKEN_ESTIMATE
