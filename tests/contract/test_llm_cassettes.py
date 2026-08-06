"""Contract tests for LLM cassettes: record, replay, keying, and scrubbing.

Written against docs/contracts-phase2.md before the implementation exists.
RECORD is fed by an httpx.MockTransport and REPLAY is given a transport that
fails on contact, so a replay that quietly hits the network fails loudly.

The bearer token used by the scrubbing tests is assembled from parts at import
time so no literal in this file resembles a credential.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest

from crossfoot.config import ProviderProfile
from crossfoot.constants import (
    PROVIDER_BASE_URLS,
    RATE_LIMIT_HEADER_MARKERS,
    LlmMode,
    Provider,
)
from crossfoot.llm.client import ChatResult

cassettes = pytest.importorskip("crossfoot.llm.cassettes")
client_module = pytest.importorskip("crossfoot.llm.client")

PHASE2_PARAMS = frozenset({"mode", "cassette_dir", "ledger", "transport"})
if not set(inspect.signature(client_module.LlmClient).parameters) >= PHASE2_PARAMS:
    pytest.skip("phase 2 LlmClient interface has not landed yet", allow_module_level=True)

MODEL = "gemini-3.5-flash"
OTHER_MODEL = "gemini-3.5-pro"
MESSAGES: list[dict[str, Any]] = [{"role": "user", "content": "read this statement"}]
OTHER_MESSAGES: list[dict[str, Any]] = [{"role": "user", "content": "read the other one"}]

# Assembled from parts, never written as one literal.
_TOKEN_PREFIX = "cfk"
_TOKEN_BODY = "7" * 30
FAKE_BEARER = "_".join((_TOKEN_PREFIX, _TOKEN_BODY))
OTHER_BEARER = "_".join((_TOKEN_PREFIX, "4" * 30))

# Distinctive values so a leak into the cassette is unmistakable.
THROTTLE_HEADERS = {
    "x-ratelimit-limit-requests": "918273",
    "x-ratelimit-remaining-tokens": "645321",
    "retry-after": "37",
}

PAGE_A = b"PNG-page-a"
PAGE_B = b"PNG-page-b"

Responder = Callable[[httpx.Request], httpx.Response]


class FakeApi:
    """Records every outbound request and answers it from a canned responder."""

    def __init__(self, responder: Responder) -> None:
        self._responder = responder
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        request.read()
        self.requests.append(request)
        return self._responder(request)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


def refuse_network(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"replay must not reach the network: {request.url}")


def ok_response(content: str = "ok", headers: dict[str, str] | None = None) -> httpx.Response:
    body: dict[str, Any] = {
        "model": MODEL,
        "choices": [{"message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 8, "completion_tokens": 1, "total_tokens": 68},
    }
    return httpx.Response(httpx.codes.OK, json=body, headers=headers or {})


def always(factory: Callable[[], httpx.Response]) -> Responder:
    def responder(_: httpx.Request) -> httpx.Response:
        return factory()

    return responder


def profile(model: str = MODEL, api_key: str = FAKE_BEARER) -> ProviderProfile:
    return ProviderProfile(
        name=Provider.GEMINI,
        base_url=PROVIDER_BASE_URLS[Provider.GEMINI],
        api_key=api_key,
        model=model,
    )


def page_images(blobs: Sequence[bytes]) -> list[Any]:
    return [
        client_module.PageImage(page_index=index, png_bytes=blob)
        for index, blob in enumerate(blobs)
    ]


async def record(
    cassette_dir: Path,
    fake: FakeApi,
    *,
    model: str = MODEL,
    api_key: str = FAKE_BEARER,
    messages: list[dict[str, Any]] | None = None,
    images: Sequence[Any] | None = None,
) -> ChatResult:
    client = client_module.LlmClient(
        profile(model=model, api_key=api_key),
        timeout_seconds=5.0,
        mode=LlmMode.RECORD,
        cassette_dir=cassette_dir,
        transport=fake.transport,
    )
    payload = MESSAGES if messages is None else messages
    if images is None:
        result = await client.chat(payload)
    else:
        result = await client.chat_vision(payload, images)
    assert isinstance(result, ChatResult)
    return result


async def replay(
    cassette_dir: Path,
    *,
    model: str = MODEL,
    messages: list[dict[str, Any]] | None = None,
    images: Sequence[Any] | None = None,
) -> ChatResult:
    client = client_module.LlmClient(
        profile(model=model),
        timeout_seconds=5.0,
        mode=LlmMode.REPLAY,
        cassette_dir=cassette_dir,
        transport=httpx.MockTransport(refuse_network),
    )
    payload = MESSAGES if messages is None else messages
    if images is None:
        result = await client.chat(payload)
    else:
        result = await client.chat_vision(payload, images)
    assert isinstance(result, ChatResult)
    return result


def files(cassette_dir: Path) -> list[Path]:
    return sorted(cassette_dir.glob("*.json"))


@pytest.fixture
def cassette_dir(tmp_path: Path) -> Path:
    path = tmp_path / "cassettes"
    path.mkdir()
    return path


# Record and replay.


async def test_record_writes_one_json_file_per_request(cassette_dir: Path) -> None:
    fake = FakeApi(always(ok_response))
    await record(cassette_dir, fake)
    assert len(files(cassette_dir)) == 1


async def test_record_writes_a_second_file_for_a_different_request(cassette_dir: Path) -> None:
    fake = FakeApi(always(ok_response))
    await record(cassette_dir, fake)
    await record(cassette_dir, fake, messages=OTHER_MESSAGES)
    assert len(files(cassette_dir)) == 2


async def test_replay_returns_an_identical_result(cassette_dir: Path) -> None:
    fake = FakeApi(always(lambda: ok_response(content="recorded body")))
    recorded = await record(cassette_dir, fake)
    replayed = await replay(cassette_dir)
    assert replayed == recorded
    assert replayed.content == "recorded body"
    assert replayed.usage.total_tokens == 68


async def test_replay_serves_a_vision_call_without_the_network(cassette_dir: Path) -> None:
    fake = FakeApi(always(lambda: ok_response(content="vision body")))
    recorded = await record(cassette_dir, fake, images=page_images([PAGE_A, PAGE_B]))
    replayed = await replay(cassette_dir, images=page_images([PAGE_A, PAGE_B]))
    assert replayed == recorded
    assert len(fake.requests) == 1


async def test_replay_on_an_unknown_key_raises_cassette_miss_error(cassette_dir: Path) -> None:
    with pytest.raises(cassettes.CassetteMissError):
        await replay(cassette_dir)


# Keying.


async def test_the_same_image_bytes_produce_the_same_key(cassette_dir: Path) -> None:
    # Two separate clients, same image bytes: the key is derived from the bytes
    # rather than from the base64 rendering, so the second run reuses the file.
    fake = FakeApi(always(ok_response))
    await record(cassette_dir, fake, images=page_images([PAGE_A]))
    first = files(cassette_dir)
    await record(cassette_dir, fake, images=page_images([bytes(PAGE_A)]))
    second = files(cassette_dir)
    assert len(second) == 1
    assert [path.name for path in second] == [path.name for path in first]


async def test_different_image_bytes_produce_different_keys(cassette_dir: Path) -> None:
    fake = FakeApi(always(ok_response))
    await record(cassette_dir, fake, images=page_images([PAGE_A]))
    await record(cassette_dir, fake, images=page_images([PAGE_B]))
    assert len(files(cassette_dir)) == 2


async def test_the_key_includes_the_model(cassette_dir: Path) -> None:
    fake = FakeApi(always(ok_response))
    await record(cassette_dir, fake, model=MODEL)
    await record(cassette_dir, fake, model=OTHER_MODEL)
    assert len(files(cassette_dir)) == 2


async def test_the_key_ignores_the_api_key(cassette_dir: Path) -> None:
    fake = FakeApi(always(ok_response))
    await record(cassette_dir, fake, api_key=FAKE_BEARER)
    await record(cassette_dir, fake, api_key=OTHER_BEARER)
    assert len(files(cassette_dir)) == 1


async def test_replay_is_stable_across_repeated_reads(cassette_dir: Path) -> None:
    fake = FakeApi(always(lambda: ok_response(content="stable")))
    await record(cassette_dir, fake)
    assert await replay(cassette_dir) == await replay(cassette_dir)


# Scrubbing.


async def test_cassette_bytes_never_contain_the_bearer_token(cassette_dir: Path) -> None:
    fake = FakeApi(always(lambda: ok_response(headers=THROTTLE_HEADERS)))
    await record(cassette_dir, fake)
    blob = files(cassette_dir)[0].read_bytes()
    assert FAKE_BEARER.encode() not in blob
    assert _TOKEN_BODY.encode() not in blob
    assert b"uthorization" not in blob


async def test_cassette_bytes_never_contain_rate_limit_headers(cassette_dir: Path) -> None:
    fake = FakeApi(always(lambda: ok_response(headers=THROTTLE_HEADERS)))
    await record(cassette_dir, fake)
    text = files(cassette_dir)[0].read_text(encoding="utf-8").lower()
    for marker in RATE_LIMIT_HEADER_MARKERS:
        assert marker not in text
    for value in THROTTLE_HEADERS.values():
        assert value not in text


async def test_cassette_still_carries_what_replay_needs(cassette_dir: Path) -> None:
    fake = FakeApi(always(lambda: ok_response(content="kept", headers=THROTTLE_HEADERS)))
    await record(cassette_dir, fake)
    stored = json.loads(files(cassette_dir)[0].read_text(encoding="utf-8"))
    assert isinstance(stored, dict)
    assert "kept" in json.dumps(stored)


async def test_replayed_result_carries_no_rate_limit_headers(cassette_dir: Path) -> None:
    # Scrubbing and replay pull in opposite directions; scrubbing wins, so a
    # replayed result reports no throttling headers at all.
    fake = FakeApi(always(lambda: ok_response(content="kept", headers=THROTTLE_HEADERS)))
    await record(cassette_dir, fake)
    replayed = await replay(cassette_dir)
    assert replayed.rate_limit_headers == {}
    assert replayed.content == "kept"
