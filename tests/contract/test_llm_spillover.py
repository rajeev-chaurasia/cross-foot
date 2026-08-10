"""Contract tests for provider spillover.

Written against docs/contracts-phase2.md before the implementation exists.
Every provider is an httpx.MockTransport keyed by host, and the clock is
injected, so the retry waits are simulated rather than slept.

Spillover triggers: 429 after retries, 402 immediately, 5xx after retries. A
400 is a bad request, not a provider problem, so it never spills over.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest

from crossfoot import costs
from crossfoot.config import ProviderProfile
from crossfoot.constants import PROVIDER_BASE_URLS, PROVIDER_DEFAULT_MODELS, Provider
from crossfoot.llm.client import ChatResult, LlmError

spillover = pytest.importorskip("crossfoot.llm.spillover")
ratelimit = pytest.importorskip("crossfoot.llm.ratelimit")

RUN_ID = "run-0001"
DOC_ID = "doc-01"
MESSAGES: list[dict[str, Any]] = [{"role": "user", "content": "read this statement"}]
CHAIN = (Provider.GEMINI, Provider.GROQ)
COOLDOWN_SECONDS = 300.0

HOSTS: dict[Provider, str] = {
    provider: str(httpx.URL(PROVIDER_BASE_URLS[provider]).host)
    for provider in (Provider.GEMINI, Provider.GROQ, Provider.OPENROUTER, Provider.MISTRAL)
}

Responder = Callable[[httpx.Request], httpx.Response]
Factory = Callable[[], httpx.Response]


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
    """Records every outbound request and answers it per provider host."""

    def __init__(self, routes: dict[Provider, Factory]) -> None:
        self._routes = {HOSTS[provider]: factory for provider, factory in routes.items()}
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        request.read()
        self.requests.append(request)
        factory = self._routes.get(str(request.url.host))
        assert factory is not None, f"unexpected host {request.url.host}"
        return factory()

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)

    def calls(self, provider: Provider) -> int:
        return sum(1 for request in self.requests if str(request.url.host) == HOSTS[provider])


def ok_response(content: str) -> httpx.Response:
    body: dict[str, Any] = {
        "model": "served",
        "choices": [{"message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 8, "completion_tokens": 1, "total_tokens": 68},
    }
    return httpx.Response(httpx.codes.OK, json=body)


def status_response(status: int, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(status, text="provider said no", headers=headers or {})


def serves(content: str) -> Factory:
    def factory() -> httpx.Response:
        return ok_response(content)

    return factory


def fails(status: int, headers: dict[str, str] | None = None) -> Factory:
    def factory() -> httpx.Response:
        return status_response(status, headers)

    return factory


def profile_for(provider: Provider) -> ProviderProfile:
    return ProviderProfile(
        name=provider,
        base_url=PROVIDER_BASE_URLS[provider],
        api_key=f"key-for-{provider.value}",
        model=PROVIDER_DEFAULT_MODELS[provider],
    )


def build_pool(
    fake: FakeApi,
    book: Any,
    clock: FakeClock,
    *,
    providers: Sequence[Provider] = CHAIN,
    max_attempts: int = 3,
) -> Any:
    return spillover.SpilloverClient(
        profiles=[profile_for(provider) for provider in providers],
        ledger=book,
        clock=clock,
        retry_policy=ratelimit.RetryPolicy(
            max_attempts=max_attempts,
            base_delay_seconds=0.5,
            max_delay_seconds=4.0,
            jitter_fraction=0.25,
        ),
        cooldown_seconds=COOLDOWN_SECONDS,
        transport=fake.transport,
        timeout_seconds=5.0,
    )


async def call(pool: Any, doc_id: str = DOC_ID) -> ChatResult:
    result = await pool.chat(MESSAGES, run_id=RUN_ID, doc_id=doc_id, purpose=costs.Purpose.EXTRACT)
    assert isinstance(result, ChatResult)
    return result


def ledger(tmp_path: Path) -> Any:
    return costs.CostLedger(tmp_path / "costs.db")


# Which statuses spill over.


async def test_429_spills_over_after_retries_are_exhausted(tmp_path: Path) -> None:
    fake = FakeApi({Provider.GEMINI: fails(429), Provider.GROQ: serves("from groq")})
    clock = FakeClock()
    result = await call(build_pool(fake, ledger(tmp_path), clock, max_attempts=3))
    assert result.content == "from groq"
    assert fake.calls(Provider.GEMINI) == 3
    assert fake.calls(Provider.GROQ) == 1


async def test_402_spills_over_immediately(tmp_path: Path) -> None:
    # A payment required answer will not improve on a retry; Cerebras returns
    # this the moment a free allowance is gone.
    fake = FakeApi({Provider.GEMINI: fails(402), Provider.GROQ: serves("from groq")})
    clock = FakeClock()
    result = await call(build_pool(fake, ledger(tmp_path), clock, max_attempts=3))
    assert result.content == "from groq"
    assert fake.calls(Provider.GEMINI) == 1
    assert fake.calls(Provider.GROQ) == 1
    assert clock.sleeps == []


async def test_5xx_spills_over_after_retries_are_exhausted(tmp_path: Path) -> None:
    fake = FakeApi({Provider.GEMINI: fails(503), Provider.GROQ: serves("from groq")})
    clock = FakeClock()
    result = await call(build_pool(fake, ledger(tmp_path), clock, max_attempts=3))
    assert result.content == "from groq"
    assert fake.calls(Provider.GEMINI) == 3
    assert fake.calls(Provider.GROQ) == 1


@pytest.mark.parametrize("max_attempts", [1, 2, 4])
async def test_the_configured_attempt_ceiling_bounds_the_retry_loop(
    tmp_path: Path, max_attempts: int
) -> None:
    # The ceiling belongs to the policy but is enforced by this loop, and every
    # other test here configures the module default, so only varying it shows
    # the configured number is read at all.
    fake = FakeApi({Provider.GEMINI: fails(503), Provider.GROQ: serves("from groq")})
    clock = FakeClock()
    result = await call(build_pool(fake, ledger(tmp_path), clock, max_attempts=max_attempts))
    assert result.content == "from groq"
    assert fake.calls(Provider.GEMINI) == max_attempts
    # One backoff between attempts, never after the last.
    assert len(clock.sleeps) == max_attempts - 1


async def test_400_does_not_spill_over(tmp_path: Path) -> None:
    # A malformed request is the caller's fault, so trying another provider
    # would only burn a second free tier on the same broken call.
    fake = FakeApi({Provider.GEMINI: fails(400), Provider.GROQ: serves("from groq")})
    clock = FakeClock()
    with pytest.raises(LlmError):
        await call(build_pool(fake, ledger(tmp_path), clock, max_attempts=3))
    assert fake.calls(Provider.GEMINI) == 1
    assert fake.calls(Provider.GROQ) == 0


async def test_retry_after_is_waited_out_before_retrying(tmp_path: Path) -> None:
    fake = FakeApi(
        {
            Provider.GEMINI: fails(429, {"retry-after": "7"}),
            Provider.GROQ: serves("from groq"),
        }
    )
    clock = FakeClock()
    await call(build_pool(fake, ledger(tmp_path), clock, max_attempts=2))
    assert clock.sleeps
    # 7 seconds honoured, with at most the configured 25 percent jitter.
    assert all(7.0 <= seconds <= 8.75 for seconds in clock.sleeps)


# Cooldown.


async def test_a_failed_profile_is_skipped_while_it_cools_down(tmp_path: Path) -> None:
    fake = FakeApi({Provider.GEMINI: fails(402), Provider.GROQ: serves("from groq")})
    clock = FakeClock()
    pool = build_pool(fake, ledger(tmp_path), clock, max_attempts=3)
    await call(pool)
    assert fake.calls(Provider.GEMINI) == 1
    await call(pool, doc_id="doc-02")
    assert fake.calls(Provider.GEMINI) == 1
    assert fake.calls(Provider.GROQ) == 2


async def test_a_cooled_down_profile_is_tried_again(tmp_path: Path) -> None:
    fake = FakeApi({Provider.GEMINI: fails(402), Provider.GROQ: serves("from groq")})
    clock = FakeClock()
    pool = build_pool(fake, ledger(tmp_path), clock, max_attempts=3)
    await call(pool)
    clock.advance(COOLDOWN_SECONDS + 1.0)
    await call(pool, doc_id="doc-02")
    assert fake.calls(Provider.GEMINI) == 2


# Ledger accounting.


async def test_every_attempt_including_failures_reaches_the_ledger(tmp_path: Path) -> None:
    fake = FakeApi({Provider.GEMINI: fails(429), Provider.GROQ: serves("from groq")})
    clock = FakeClock()
    book = ledger(tmp_path)
    await call(build_pool(fake, book, clock, max_attempts=3))
    rows = book.rows(RUN_ID)
    assert [row.provider for row in rows] == [
        Provider.GEMINI,
        Provider.GEMINI,
        Provider.GEMINI,
        Provider.GROQ,
    ]
    assert [row.http_status for row in rows] == [429, 429, 429, 200]
    # The attempt counter restarts on each profile.
    assert [row.attempt for row in rows] == [1, 2, 3, 1]
    assert all(row.doc_id == DOC_ID for row in rows)
    assert all(row.purpose == costs.Purpose.EXTRACT for row in rows)


async def test_the_successful_attempt_carries_the_reported_usage(tmp_path: Path) -> None:
    fake = FakeApi({Provider.GEMINI: fails(402), Provider.GROQ: serves("from groq")})
    clock = FakeClock()
    book = ledger(tmp_path)
    await call(build_pool(fake, book, clock, max_attempts=3))
    served = book.rows(RUN_ID)[-1]
    assert served.provider == Provider.GROQ
    assert served.model == PROVIDER_DEFAULT_MODELS[Provider.GROQ]
    assert served.total_tokens == 68


# Exhaustion.


async def test_all_profiles_failing_raises_a_typed_error(tmp_path: Path) -> None:
    fake = FakeApi({Provider.GEMINI: fails(402), Provider.GROQ: fails(402)})
    clock = FakeClock()
    with pytest.raises(spillover.AllProvidersFailedError):
        await call(build_pool(fake, ledger(tmp_path), clock, max_attempts=3))
    assert fake.calls(Provider.GEMINI) == 1
    assert fake.calls(Provider.GROQ) == 1


async def test_exhaustion_still_records_every_attempt(tmp_path: Path) -> None:
    fake = FakeApi({Provider.GEMINI: fails(402), Provider.GROQ: fails(402)})
    clock = FakeClock()
    book = ledger(tmp_path)
    with pytest.raises(spillover.AllProvidersFailedError):
        await call(build_pool(fake, book, clock, max_attempts=3))
    rows = book.rows(RUN_ID)
    assert [row.provider for row in rows] == [Provider.GEMINI, Provider.GROQ]
    assert [row.http_status for row in rows] == [402, 402]


async def test_spillover_walks_the_whole_configured_chain(tmp_path: Path) -> None:
    fake = FakeApi(
        {
            Provider.GEMINI: fails(402),
            Provider.GROQ: fails(402),
            Provider.OPENROUTER: serves("from openrouter"),
        }
    )
    clock = FakeClock()
    pool = build_pool(
        fake,
        ledger(tmp_path),
        clock,
        providers=(Provider.GEMINI, Provider.GROQ, Provider.OPENROUTER),
        max_attempts=3,
    )
    result = await call(pool)
    assert result.content == "from openrouter"
    assert fake.calls(Provider.OPENROUTER) == 1
