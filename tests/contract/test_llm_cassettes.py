"""Contract tests for LLM cassettes: record, replay, keying, and scrubbing.

Written against docs/contracts-phase2.md before the implementation exists.
RECORD is fed by an httpx.MockTransport and REPLAY is given a transport that
fails on contact, so a replay that quietly hits the network fails loudly.

Scrubbing is asserted as a property of the writer rather than as the absence of
a string: nothing hands a credential to the writer, so the tests pin the
allowlist it emits and hand it a result that does carry throttling headers. The
bearer token is assembled from parts at import time so no literal in this file
resembles a credential.
"""

from __future__ import annotations

import hashlib
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
from crossfoot.llm import cassettes
from crossfoot.llm import client as client_module
from crossfoot.llm.client import ChatResult, ChatUsage

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

# Distinctive values so a leak into the cassette is unmistakable. Six digits
# each: a two digit value would match a token count by coincidence.
THROTTLE_HEADERS = {
    "x-ratelimit-limit-requests": "918273",
    "x-ratelimit-remaining-tokens": "645321",
    "retry-after": "573914",
}
# Latency the writer is handed and must not store, distinctive for the same reason.
LEAK_LATENCY_MS = 872_341

# The writer's allowlist. A cassette carries these keys and nothing else, which
# is why no credential and no throttling header has anywhere to travel.
CASSETTE_KEYS = frozenset({"version", "model", "content", "usage"})
CASSETTE_USAGE_KEYS = frozenset({"prompt_tokens", "completion_tokens", "total_tokens"})

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


def compared(result: ChatResult) -> tuple[str, str, ChatUsage]:
    """The fields ChatResult equality looks at, named so a test says what it means.

    latency_ms and rate_limit_headers are compare=False, so a bare == between a
    live result and a replayed one silently skips the two fields that differ.
    """
    return (result.content, result.model, result.usage)


def transport_metadata(result: ChatResult) -> tuple[int, dict[str, str]]:
    """The fields ChatResult equality skips."""
    return (result.latency_ms, result.rate_limit_headers)


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
    assert compared(replayed) == compared(recorded)
    assert replayed.content == "recorded body"
    assert replayed.usage.total_tokens == 68
    # The two fields equality skips: scrubbing dropped the headers and a replay
    # measured no request.
    assert transport_metadata(replayed) == (cassettes.REPLAY_LATENCY_MS, {})


async def test_replay_serves_a_vision_call_without_the_network(cassette_dir: Path) -> None:
    fake = FakeApi(always(lambda: ok_response(content="vision body")))
    recorded = await record(cassette_dir, fake, images=page_images([PAGE_A, PAGE_B]))
    replayed = await replay(cassette_dir, images=page_images([PAGE_A, PAGE_B]))
    assert compared(replayed) == compared(recorded)
    assert transport_metadata(replayed) == (cassettes.REPLAY_LATENCY_MS, {})
    assert len(fake.requests) == 1


async def test_replay_on_an_unknown_key_raises_cassette_miss_error(cassette_dir: Path) -> None:
    with pytest.raises(cassettes.CassetteMissError):
        await replay(cassette_dir)


# Keying.


def test_the_image_digest_hashes_the_bytes_not_the_base64_rendering() -> None:
    # Recording the same bytes twice cannot tell the two apart: both are
    # deterministic functions of the same input, so the claim is about digest().
    image = page_images([PAGE_A])[0]
    assert image.digest() == hashlib.sha256(PAGE_A).hexdigest()
    assert image.digest() != hashlib.sha256(image.data_uri().encode("utf-8")).hexdigest()


async def test_the_cassette_key_is_built_from_the_digest_of_the_page_bytes(
    cassette_dir: Path,
) -> None:
    # The key is the file name, so this pins the whole keying path: a digest
    # taken over the base64 rendering would name a different file.
    fake = FakeApi(always(ok_response))
    await record(cassette_dir, fake, images=page_images([PAGE_A]))
    expected = cassettes.request_key(
        model=MODEL,
        messages=MESSAGES,
        image_digests=[hashlib.sha256(PAGE_A).hexdigest()],
    )
    assert [path.stem for path in files(cassette_dir)] == [expected]


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
    first = await replay(cassette_dir)
    second = await replay(cassette_dir)
    assert compared(first) == compared(second)
    # Equality skips these, so repeating a read has to be checked on them too.
    assert transport_metadata(first) == transport_metadata(second)


# Scrubbing.


async def test_a_recorded_cassette_carries_only_the_allowlisted_fields(
    cassette_dir: Path,
) -> None:
    # Scrubbing is structural: the token and the throttling headers are absent
    # because these are the only keys the writer emits. Asserting the allowlist
    # is what makes widening the writer fail rather than pass unnoticed.
    fake = FakeApi(always(lambda: ok_response(headers=THROTTLE_HEADERS)))
    await record(cassette_dir, fake, api_key=FAKE_BEARER)
    stored = json.loads(files(cassette_dir)[0].read_text(encoding="utf-8"))
    assert set(stored) == CASSETTE_KEYS
    assert set(stored["usage"]) == CASSETTE_USAGE_KEYS


def test_the_writer_drops_the_transport_metadata_it_is_handed(cassette_dir: Path) -> None:
    # save takes a ChatResult and nothing else, so a result carrying the
    # throttling headers is the one poison that can reach the file at all.
    poisoned = ChatResult(
        content="kept",
        model=MODEL,
        usage=ChatUsage(prompt_tokens=8, completion_tokens=1, total_tokens=68),
        latency_ms=LEAK_LATENCY_MS,
        rate_limit_headers=dict(THROTTLE_HEADERS),
    )
    cassettes.save(cassette_dir, "poisoned", poisoned)
    text = files(cassette_dir)[0].read_text(encoding="utf-8")
    assert set(json.loads(text)) == CASSETTE_KEYS
    lowered = text.lower()
    for marker in RATE_LIMIT_HEADER_MARKERS:
        assert marker not in lowered
    for value in THROTTLE_HEADERS.values():
        assert value not in text
    assert str(LEAK_LATENCY_MS) not in text


async def test_cassette_still_carries_what_replay_needs(cassette_dir: Path) -> None:
    # Exactly the fields cassettes.load reads back; a blob missing any of them
    # replays nothing.
    fake = FakeApi(always(lambda: ok_response(content="kept", headers=THROTTLE_HEADERS)))
    await record(cassette_dir, fake)
    stored = json.loads(files(cassette_dir)[0].read_text(encoding="utf-8"))
    assert stored["version"] == cassettes.CASSETTE_VERSION
    assert stored["model"] == MODEL
    assert stored["content"] == "kept"
    assert stored["usage"] == {"prompt_tokens": 8, "completion_tokens": 1, "total_tokens": 68}


async def test_replayed_result_carries_no_rate_limit_headers(cassette_dir: Path) -> None:
    # Scrubbing and replay pull in opposite directions; scrubbing wins, so a
    # replayed result reports no throttling headers at all.
    fake = FakeApi(always(lambda: ok_response(content="kept", headers=THROTTLE_HEADERS)))
    await record(cassette_dir, fake)
    replayed = await replay(cassette_dir)
    assert replayed.rate_limit_headers == {}
    assert replayed.content == "kept"
