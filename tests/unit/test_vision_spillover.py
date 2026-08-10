"""The vision path under provider failure: retry, spillover, degradation.

A live run died on a transient 503 from the k=2 consistency sample because the
vision call bypassed the spillover pool entirely. Everything here is offline: an
httpx.MockTransport answers per provider host and the clock is injected, so
every retry wait is simulated rather than slept.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest

from crossfoot.config import ProviderProfile
from crossfoot.constants import (
    PROVIDER_BASE_URLS,
    PROVIDER_DEFAULT_MODELS,
    DocType,
    ExtractionRoute,
    FieldName,
    IngestErrorKind,
    Provider,
)
from crossfoot.costs import CostLedger, Purpose
from crossfoot.evals.runner import ExtractionRun, VisionDegradations, run_notes
from crossfoot.extraction.llm_vision import PageImage, VisionExtractor
from crossfoot.llm.ratelimit import RetryPolicy
from crossfoot.llm.spillover import FailureAction, SpilloverClient, action_for
from crossfoot.models.extraction import ExtractedDocument, ExtractedField

RUN_ID = "run-vision-0001"
DOC_ID = "doc-parts_statement-dlr-meridian-202607-01"
NEXT_DOC_ID = "doc-parts_statement-dlr-meridian-202607-02"
FILE_PATH = "files/statement.pdf"
# The transport never decodes these bytes; they only prove images reach the wire.
PNG_BYTES = b"\x89PNG\r\n\x1a\n"
# The same eight bytes in base64, by hand: 89 50 4E 47 0D 0A 1A 0A.
PAGE_DATA_URI = "data:image/png;base64,iVBORw0KGgo="

CHAIN = (Provider.GEMINI, Provider.GROQ)
COOLDOWN_SECONDS = 300.0
OK = int(httpx.codes.OK)
TOTAL_CENTS = 145_000

HOSTS: dict[Provider, str] = {
    provider: str(httpx.URL(PROVIDER_BASE_URLS[provider]).host) for provider in CHAIN
}

PAYLOAD: dict[str, Any] = {
    "statement_number": {"raw": "PS-2026-07-001", "normalized": "PS-2026-07-001"},
    "total": {"raw": "$1,450.00", "normalized": "1450.00"},
    "lines": [
        {
            "row_position": 1,
            "invoice_number": {"raw": "M1234567", "normalized": "M1234567"},
            "line_date": {"raw": "07/10/2026", "normalized": "2026-07-10"},
            "line_amount": {"raw": "$1,000.00", "normalized": "1000.00"},
        }
    ],
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
        "choices": [{"message": {"role": "assistant", "content": json.dumps(PAYLOAD)}}],
        "usage": {"prompt_tokens": 8, "completion_tokens": 1, "total_tokens": 68},
    }


def _profile(provider: Provider) -> ProviderProfile:
    return ProviderProfile(
        name=provider,
        base_url=PROVIDER_BASE_URLS[provider],
        api_key=f"key-for-{provider.value}",
        model=PROVIDER_DEFAULT_MODELS[provider],
    )


def build_extractor(
    api: ScriptedApi,
    book: CostLedger,
    clock: FakeClock,
    *,
    max_attempts: int = 3,
    cooldown_seconds: float = COOLDOWN_SECONDS,
) -> VisionExtractor:
    """The extractor the CLI builds: a spillover pool behind the client protocol."""
    pool = SpilloverClient(
        profiles=[_profile(provider) for provider in CHAIN],
        ledger=book,
        clock=clock,
        retry_policy=RetryPolicy(
            max_attempts=max_attempts,
            base_delay_seconds=0.5,
            max_delay_seconds=4.0,
            jitter_fraction=0.25,
        ),
        cooldown_seconds=cooldown_seconds,
        transport=api.transport,
        timeout_seconds=5.0,
    )
    return VisionExtractor(pool, run_id=RUN_ID)


async def extract(extractor: VisionExtractor, doc_id: str = DOC_ID) -> ExtractedDocument:
    return await extractor.extract_document(
        doc_id=doc_id,
        file_path=FILE_PATH,
        doc_type=DocType.PARTS_STATEMENT,
        images=(PageImage(page=0, png_bytes=PNG_BYTES),),
    )


def ledger(tmp_path: Path) -> CostLedger:
    return CostLedger(tmp_path / "costs.db")


def _fields(doc: ExtractedDocument) -> tuple[ExtractedField, ...]:
    return (*doc.header_fields, *doc.line_fields)


def _total_cents(doc: ExtractedDocument) -> int | None:
    return next(field for field in doc.header_fields if field.name is FieldName.TOTAL).value_cents


def _rows(book: CostLedger, purpose: Purpose) -> list[Any]:
    return [row for row in book.rows(RUN_ID) if row.purpose == purpose]


def _image_urls(request: httpx.Request) -> list[str]:
    """Every image part on the last message of one outbound request, in order."""
    parts = json.loads(request.content)["messages"][-1]["content"]
    return [part["image_url"]["url"] for part in parts if part["type"] == "image_url"]


# Retryable status table.


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (None, FailureAction.RETRY),  # transport failure, treated like a 5xx
        (429, FailureAction.RETRY),
        (500, FailureAction.RETRY),
        (502, FailureAction.RETRY),
        (503, FailureAction.RETRY),
        (504, FailureAction.RETRY),
        (402, FailureAction.SPILL),
        (400, FailureAction.RAISE),
        (401, FailureAction.RAISE),
        (403, FailureAction.RAISE),
        (404, FailureAction.RAISE),
        (422, FailureAction.RAISE),
    ],
)
def test_every_status_is_classified_by_one_named_table(
    status: int | None, expected: FailureAction
) -> None:
    assert action_for(status) is expected


# Retry and spillover on the vision call.


async def test_a_503_is_retried_and_the_document_still_extracts(tmp_path: Path) -> None:
    api = ScriptedApi({Provider.GEMINI: (503, OK)})
    book = ledger(tmp_path)
    doc = await extract(build_extractor(api, book, FakeClock()))
    assert doc.route is ExtractionRoute.SCANNED_PDF
    assert _total_cents(doc) == TOTAL_CENTS
    # Both attempts reach the ledger, the failure included.
    assert [(row.http_status, row.attempt) for row in _rows(book, Purpose.EXTRACT)] == [
        (503, 1),
        (OK, 2),
    ]


async def test_the_retried_vision_call_still_carries_the_page_images(tmp_path: Path) -> None:
    api = ScriptedApi({Provider.GEMINI: (503, OK)})
    await extract(build_extractor(api, ledger(tmp_path), FakeClock()))
    # The page bytes themselves, not merely a part typed image_url: a retry that
    # dropped or duplicated the image would still carry one of those.
    assert _image_urls(api.requests[1]) == [PAGE_DATA_URI]
    assert _image_urls(api.requests[0]) == _image_urls(api.requests[1])


async def test_a_dead_primary_spills_over_and_the_document_still_extracts(
    tmp_path: Path,
) -> None:
    api = ScriptedApi({Provider.GEMINI: (503,), Provider.GROQ: (OK,)})
    book = ledger(tmp_path)
    doc = await extract(build_extractor(api, book, FakeClock()))
    assert doc.route is ExtractionRoute.SCANNED_PDF
    assert _total_cents(doc) == TOTAL_CENTS
    assert api.calls(Provider.GEMINI) == 3
    rows = _rows(book, Purpose.EXTRACT)
    assert [row.provider for row in rows] == [
        Provider.GEMINI,
        Provider.GEMINI,
        Provider.GEMINI,
        Provider.GROQ,
    ]
    # The attempt counter restarts on each profile.
    assert [row.attempt for row in rows] == [1, 2, 3, 1]


async def test_a_400_neither_retries_nor_spills_over(tmp_path: Path) -> None:
    # A malformed request is not a provider problem; three more free tiers would
    # only burn three more allowances on the same broken call.
    api = ScriptedApi({Provider.GEMINI: (400,), Provider.GROQ: (OK,)})
    doc = await extract(build_extractor(api, ledger(tmp_path), FakeClock()))
    assert api.calls(Provider.GEMINI) == 1
    assert api.calls(Provider.GROQ) == 0
    assert doc.route is ExtractionRoute.UNPROCESSABLE


# Degradation rather than loss.


async def test_a_lost_consistency_sample_degrades_instead_of_losing_the_document(
    tmp_path: Path,
) -> None:
    # The authoritative sample lands, then every provider refuses the warm one.
    api = ScriptedApi({Provider.GEMINI: (OK, 503), Provider.GROQ: (503,)})
    extractor = build_extractor(api, ledger(tmp_path), FakeClock())
    doc = await extract(extractor)
    assert doc.route is ExtractionRoute.SCANNED_PDF
    assert _total_cents(doc) == TOTAL_CENTS
    assert _fields(doc)
    assert all(field.signals.self_consistency is None for field in _fields(doc))
    assert extractor.consistency_degradations == 1
    assert api.calls(Provider.GEMINI) == 4  # one authoritative, three exhausted retries
    assert api.calls(Provider.GROQ) == 3


async def test_one_lost_document_does_not_stop_the_batch(tmp_path: Path) -> None:
    # Cooldown is zero so the second document reaches the same profiles; the
    # frozen contract suite is what covers cooling down.
    api = ScriptedApi({Provider.GEMINI: (503, 503, 503, OK), Provider.GROQ: (503,)})
    extractor = build_extractor(api, ledger(tmp_path), FakeClock(), cooldown_seconds=0.0)
    lost, served = [await extract(extractor, doc_id) for doc_id in (DOC_ID, NEXT_DOC_ID)]

    assert lost.route is ExtractionRoute.UNPROCESSABLE
    assert lost.error is not None
    # The kind says the run failed, not the document, so a resume owes it again.
    assert lost.error.kind is IngestErrorKind.PROVIDER_UNAVAILABLE
    assert extractor.provider_failures == 1

    assert served.route is ExtractionRoute.SCANNED_PDF
    assert _total_cents(served) == TOTAL_CENTS
    assert extractor.consistency_degradations == 0


# Every degraded path is recorded rather than hidden.


def _run(degradations: VisionDegradations) -> ExtractionRun:
    return ExtractionRun(
        documents=(), unprocessable=(), unserved=Counter(), degradations=degradations
    )


def test_the_degradation_counters_reach_the_run_notes() -> None:
    notes = run_notes(_run(VisionDegradations(consistency_degradations=2, provider_failures=1)))
    assert "2 lost the consistency sample" in notes
    assert "1 failed on every provider" in notes


def test_a_run_that_degraded_nothing_says_nothing() -> None:
    assert "Vision degradations" not in run_notes(_run(VisionDegradations()))
