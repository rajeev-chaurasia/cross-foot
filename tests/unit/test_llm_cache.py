"""The response cache and the price table it feeds.

The cache has no contract test yet; the contract observes it through the
ledger, so these tests hold both ends: what the cache returns and what a hit
costs.
"""

from pathlib import Path
from typing import Any

import httpx

from crossfoot.config import ProviderProfile
from crossfoot.constants import PROVIDER_BASE_URLS, PROVIDER_DEFAULT_MODELS, LlmMode, Provider
from crossfoot.costs import CallContext, CostLedger, ModelPrice, Purpose, list_price_microusd
from crossfoot.llm.cache import ResponseCache
from crossfoot.llm.client import LlmClient
from crossfoot.llm.results import ChatResult, ChatUsage

RUN_ID = "run-cache-01"
DOC_ID = "doc-01"
MESSAGES: list[dict[str, Any]] = [{"role": "user", "content": "read this statement"}]
MODEL = PROVIDER_DEFAULT_MODELS[Provider.GEMINI]

PRICES = (
    ModelPrice(
        pattern="unit-model",
        prompt_microusd_per_mtok=1_000_000,
        completion_microusd_per_mtok=2_000_000,
    ),
    ModelPrice(
        pattern="unit-model-pro",
        prompt_microusd_per_mtok=10_000_000,
        completion_microusd_per_mtok=20_000_000,
    ),
)


def _result(content: str = "cached body") -> ChatResult:
    return ChatResult(
        content=content,
        model=MODEL,
        usage=ChatUsage(prompt_tokens=8, completion_tokens=1, total_tokens=68),
        latency_ms=412,
        rate_limit_headers={"x-ratelimit-remaining-requests": "17"},
    )


def _profile() -> ProviderProfile:
    return ProviderProfile(
        name=Provider.GEMINI,
        base_url=PROVIDER_BASE_URLS[Provider.GEMINI],
        api_key="not-a-real-key",
        model=MODEL,
    )


def _serve(_: httpx.Request) -> httpx.Response:
    body = {
        "model": MODEL,
        "choices": [{"message": {"role": "assistant", "content": "from the provider"}}],
        "usage": {"prompt_tokens": 8, "completion_tokens": 1, "total_tokens": 68},
    }
    return httpx.Response(httpx.codes.OK, json=body)


def test_a_miss_returns_none(tmp_path: Path) -> None:
    assert ResponseCache(tmp_path / "cache.db").get("no-such-key") is None


def test_a_stored_result_round_trips(tmp_path: Path) -> None:
    cache = ResponseCache(tmp_path / "cache.db")
    cache.put("key-1", _result())
    hit = cache.get("key-1")
    assert hit is not None
    assert hit.content == "cached body"
    assert hit.usage.total_tokens == 68
    # Transport metadata is not cached, so a hit reports no throttling headers.
    assert hit.rate_limit_headers == {}


def test_a_stored_result_survives_reopening(tmp_path: Path) -> None:
    ResponseCache(tmp_path / "cache.db").put("key-1", _result())
    reopened = ResponseCache(tmp_path / "cache.db")
    assert reopened.get("key-1") is not None


async def test_a_cache_hit_skips_the_provider_and_costs_no_tokens(tmp_path: Path) -> None:
    cache = ResponseCache(tmp_path / "cache.db")
    book = CostLedger(tmp_path / "costs.db")
    requests: list[httpx.Request] = []

    def record_and_serve(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _serve(request)

    client = LlmClient(
        _profile(),
        mode=LlmMode.LIVE,
        ledger=book,
        cache=cache,
        transport=httpx.MockTransport(record_and_serve),
    )
    context = CallContext(run_id=RUN_ID, doc_id=DOC_ID, purpose=Purpose.EXTRACT)
    first = await client.chat(MESSAGES, context=context)
    second = await client.chat(MESSAGES, context=context)

    assert len(requests) == 1
    assert second == first
    live, hit = book.rows(RUN_ID)
    assert live.cached is False
    assert live.total_tokens == 68
    assert hit.cached is True
    assert hit.total_tokens == 0
    assert hit.list_price_microusd == 0


def test_the_longest_matching_pattern_prices_the_model() -> None:
    price = list_price_microusd(
        "unit-model-pro", prompt_tokens=1_000_000, completion_tokens=0, prices=PRICES
    )
    assert price == 10_000_000


def test_an_unpriced_model_is_worth_nothing_rather_than_a_guess() -> None:
    price = list_price_microusd(
        "model-nobody-listed", prompt_tokens=1_000_000, completion_tokens=1_000_000, prices=PRICES
    )
    assert price == 0
