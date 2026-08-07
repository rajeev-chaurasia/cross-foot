"""A clock that gives concurrent tasks one shared simulated timeline.

The FakeClock the other suites use adds every sleep to one counter, which
double counts the moment four workers sleep at once. This one parks each
sleeper on a deadline and advances to the earliest deadline only once nothing
else can run, so eight one second documents at concurrency four measure two
seconds rather than eight. Nothing here ever really sleeps.
"""

from __future__ import annotations

import asyncio
import heapq
import itertools
from collections.abc import Coroutine
from typing import Any, TypeVar

# Yields that count as the event loop having gone quiet. Every wait in these
# tests resolves through a ready callback, so a bounded drain is enough.
DRAIN_ROUNDS = 64
# A run that neither finishes nor parks a sleeper is stuck; fail, never hang.
MAX_IDLE_DRAINS = 100

T = TypeVar("T")


class VirtualClock:
    """Injected clock plus the driver that moves its time forward."""

    def __init__(self) -> None:
        self._now = 0.0
        # Deadline, insertion order (ties break deterministically), waiter.
        self._waiters: list[tuple[float, int, asyncio.Future[None]]] = []
        self._sequence = itertools.count()
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self._now

    async def sleep(self, seconds: float) -> None:
        if seconds <= 0.0:
            await asyncio.sleep(0)
            return
        self.sleeps.append(seconds)
        waiter: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        heapq.heappush(self._waiters, (self._now + seconds, next(self._sequence), waiter))
        await waiter

    async def run(self, main: Coroutine[Any, Any, T]) -> T:
        """Drive one coroutine to completion, advancing time whenever work stalls."""
        task = asyncio.ensure_future(main)
        idle = 0
        while not task.done():
            await self._drain(task)
            if task.done():
                break
            if not self._waiters:
                idle += 1
                if idle > MAX_IDLE_DRAINS:
                    raise TimeoutError("nothing is running and nothing is sleeping")
                continue
            idle = 0
            self._advance()
        return await task

    async def _drain(self, task: asyncio.Future[T]) -> None:
        for _ in range(DRAIN_ROUNDS):
            if task.done():
                return
            await asyncio.sleep(0)

    def _advance(self) -> None:
        self._now = max(self._now, self._waiters[0][0])
        while self._waiters and self._waiters[0][0] <= self._now:
            _, _, waiter = heapq.heappop(self._waiters)
            if not waiter.done():
                waiter.set_result(None)
