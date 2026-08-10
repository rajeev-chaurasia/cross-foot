"""Concurrent extraction: overlap, pacing, determinism, and checkpoints.

A single live vision call measured 17 to 41 seconds, so the roughly 180 calls a
full run needs are hours when they go out one at a time. Everything here is
offline: the extractor is a fake and the clock is virtual, so the waits are
simulated and the elapsed time the tests assert on is the simulated one.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path

import pytest
from virtual_clock import VirtualClock

from crossfoot.constants import PROVIDER_DEFAULT_MODELS, ExtractionRoute, Provider
from crossfoot.costs import CostLedger, Purpose
from crossfoot.extraction.batch import BatchExtractor, DocumentOutcome
from crossfoot.llm.ratelimit import RateLimiter
from crossfoot.llm.runstate import DocStatus, RunState
from crossfoot.models.extraction import ExtractedDocument

RUN_ID = "run-batch-01"
DOCUMENT_SECONDS = 1.0
CONCURRENCY = 4
DOCUMENT_COUNT = 8
KILLED_DOC_ID = "doc-04"

MODEL = PROVIDER_DEFAULT_MODELS[Provider.GEMINI]
OK = 200

# One minute of allowance, small enough that twelve documents must wait it out.
REQUESTS_PER_MINUTE = 6
TOKENS_PER_MINUTE = 1_000_000
LIMITED_DOCUMENTS = 12
SECONDS_PER_MINUTE = 60.0
FLOAT_SLACK = 1e-9


class KilledRunError(RuntimeError):
    """Stands in for the process dying partway through a document."""


def doc_ids(count: int = DOCUMENT_COUNT) -> list[str]:
    return [f"doc-{index:02d}" for index in range(count)]


def _document(doc_id: str) -> ExtractedDocument:
    return ExtractedDocument(
        doc_id=doc_id, file_path=f"files/{doc_id}.pdf", route=ExtractionRoute.SCANNED_PDF
    )


def _state(db_path: Path, ids: Sequence[str]) -> RunState:
    state = RunState(db_path)
    state.start_run(RUN_ID, ids)
    return state


def _discard(line: str) -> None:
    """Progress has its own tests; everywhere else it stays out of the way."""


def _no_degradations() -> int:
    return 0


def _batch(
    state: RunState,
    extract: Callable[[str], Awaitable[DocumentOutcome]],
    clock: VirtualClock,
    *,
    concurrency: int = CONCURRENCY,
    report: Callable[[str], None] = _discard,
    degradations: Callable[[], int] = _no_degradations,
    fatal: tuple[type[Exception], ...] = (),
) -> BatchExtractor:
    return BatchExtractor(
        state=state,
        run_id=RUN_ID,
        extract=extract,
        concurrency=concurrency,
        clock=clock,
        degradations=degradations,
        report=report,
        fatal=fatal,
    )


class FakeExtractor:
    """Charges the ledger the way a real call does, then maybe dies."""

    def __init__(
        self, book: CostLedger, clock: VirtualClock, *, fail_on: str | None = None
    ) -> None:
        self._book = book
        self._clock = clock
        self._fail_on = fail_on
        self.calls: list[str] = []

    async def __call__(self, doc_id: str) -> DocumentOutcome:
        self.calls.append(doc_id)
        # The provider is paid when the call goes out, before anything can fail.
        self._book.record(
            run_id=RUN_ID,
            doc_id=doc_id,
            purpose=Purpose.EXTRACT,
            provider=Provider.GEMINI,
            model=MODEL,
            prompt_tokens=10,
            completion_tokens=5,
            total_tokens=15,
            cached=False,
            latency_ms=1,
            http_status=OK,
            attempt=1,
        )
        if doc_id == self._fail_on:
            raise KilledRunError(doc_id)
        await self._clock.sleep(DOCUMENT_SECONDS)
        return DocumentOutcome(document=_document(doc_id))


# Overlap.


async def test_four_workers_overlap_eight_one_second_documents(tmp_path: Path) -> None:
    clock = VirtualClock()
    ids = doc_ids()
    state = _state(tmp_path / "runstate.db", ids)

    async def extract(doc_id: str) -> DocumentOutcome:
        await clock.sleep(DOCUMENT_SECONDS)
        return DocumentOutcome(document=_document(doc_id))

    result = await clock.run(_batch(state, extract, clock).run(ids))

    assert len(result.documents) == DOCUMENT_COUNT
    # Sequentially this run is eight seconds; four in flight makes it two waves.
    assert clock.now() == pytest.approx(2 * DOCUMENT_SECONDS)


# The limiter is still the single place a request is admitted.


async def test_concurrent_workers_do_not_outrun_the_rate_limiter(tmp_path: Path) -> None:
    clock = VirtualClock()
    limiter = RateLimiter(
        requests_per_minute=REQUESTS_PER_MINUTE,
        tokens_per_minute=TOKENS_PER_MINUTE,
        clock=clock,
    )
    ids = doc_ids(LIMITED_DOCUMENTS)
    state = _state(tmp_path / "runstate.db", ids)
    admitted: list[float] = []

    async def extract(doc_id: str) -> DocumentOutcome:
        await limiter.acquire(tokens=1)
        admitted.append(clock.now())
        return DocumentOutcome(document=_document(doc_id))

    await clock.run(_batch(state, extract, clock).run(ids))

    assert len(admitted) == LIMITED_DOCUMENTS
    refill_per_second = REQUESTS_PER_MINUTE / SECONDS_PER_MINUTE
    for count, moment in enumerate(admitted, start=1):
        # A bucket holding one minute of allowance can never have issued more
        # than its capacity plus what refilled by then, whatever admitted it.
        assert count <= REQUESTS_PER_MINUTE + moment * refill_per_second + FLOAT_SLACK
    # Six go free, then one every ten seconds: six per minute, not four at once.
    assert clock.now() == pytest.approx(SECONDS_PER_MINUTE)


# Deterministic order.


async def _run_ordered(db_path: Path, ids: Sequence[str], *, reverse: bool) -> list[str]:
    """One pass whose documents finish in doc_id order, or exactly against it."""
    clock = VirtualClock()
    state = _state(db_path, ids)
    finished: list[str] = []

    async def extract(doc_id: str) -> DocumentOutcome:
        position = ids.index(doc_id)
        rank = len(ids) - position if reverse else position + 1
        await clock.sleep(rank * DOCUMENT_SECONDS)
        finished.append(doc_id)
        return DocumentOutcome(document=_document(doc_id))

    result = await clock.run(_batch(state, extract, clock, concurrency=len(ids)).run(ids))
    assert finished == (list(reversed(ids)) if reverse else list(ids))
    return [document.model_dump_json() for document in result.documents]


async def test_documents_come_back_sorted_by_doc_id_whatever_finishes_first(
    tmp_path: Path,
) -> None:
    ids = doc_ids()
    payloads = await _run_ordered(tmp_path / "runstate.db", ids, reverse=True)
    assert [ExtractedDocument.model_validate_json(row).doc_id for row in payloads] == sorted(ids)


async def test_opposite_completion_orders_produce_identical_output(tmp_path: Path) -> None:
    ids = doc_ids()
    first = await _run_ordered(tmp_path / "first" / "runstate.db", ids, reverse=True)
    second = await _run_ordered(tmp_path / "second" / "runstate.db", ids, reverse=False)
    assert first == second


# Checkpoints under concurrency.


async def test_a_killed_run_resumes_without_extracting_or_charging_a_document_twice(
    tmp_path: Path,
) -> None:
    clock = VirtualClock()
    ids = doc_ids()
    db_path = tmp_path / "runstate.db"
    book = CostLedger(tmp_path / "costs.db")
    state = _state(db_path, ids)

    killed = FakeExtractor(book, clock, fail_on=KILLED_DOC_ID)
    with pytest.raises(KilledRunError):
        await clock.run(_batch(state, killed, clock, fatal=(KilledRunError,)).run(ids))
    assert killed.calls == ids
    # The document that died never reached DONE, so it is still owed.
    assert state.status(RUN_ID, KILLED_DOC_ID) is not DocStatus.DONE
    state.close()

    # A resumed run is a new process reading the checkpoints back off disk.
    resumed_state = RunState(db_path)
    resumed = FakeExtractor(book, clock)
    result = await clock.run(_batch(resumed_state, resumed, clock).run(ids, resume=True))

    assert resumed.calls == [KILLED_DOC_ID]
    assert result.skipped == DOCUMENT_COUNT - 1
    assert [document.doc_id for document in result.documents] == [KILLED_DOC_ID]

    charged = Counter(row.doc_id for row in book.rows(RUN_ID))
    # Only the document that died is charged twice: the provider was already
    # paid for the attempt that died, and the resumed attempt adds its own.
    assert charged[KILLED_DOC_ID] == 2
    assert all(charged[doc_id] == 1 for doc_id in ids if doc_id != KILLED_DOC_ID)
    assert sum(charged.values()) == DOCUMENT_COUNT + 1


async def test_a_failed_document_is_checkpointed_and_the_batch_carries_on(
    tmp_path: Path,
) -> None:
    clock = VirtualClock()
    ids = doc_ids()
    state = _state(tmp_path / "runstate.db", ids)

    async def extract(doc_id: str) -> DocumentOutcome:
        await clock.sleep(DOCUMENT_SECONDS)
        if doc_id == KILLED_DOC_ID:
            raise RuntimeError("this document only breaks itself")
        return DocumentOutcome(document=_document(doc_id))

    result = await clock.run(_batch(state, extract, clock).run(ids))

    assert len(result.documents) == DOCUMENT_COUNT - 1
    # A raised exception carries no verdict on the document, so it counts as
    # work still owed rather than as a result: FAILED, and pending on a resume.
    assert result.pending_retry == 1
    assert result.unprocessable == 0
    assert state.status(RUN_ID, KILLED_DOC_ID) is DocStatus.FAILED
    assert all(
        state.status(RUN_ID, doc_id) is DocStatus.DONE for doc_id in ids if doc_id != KILLED_DOC_ID
    )


# Progress.


async def test_one_progress_line_lands_per_completed_document(tmp_path: Path) -> None:
    clock = VirtualClock()
    ids = doc_ids()
    state = _state(tmp_path / "runstate.db", ids)
    lines: list[str] = []

    async def extract(doc_id: str) -> DocumentOutcome:
        await clock.sleep(DOCUMENT_SECONDS)
        return DocumentOutcome(document=_document(doc_id))

    # A counter that grows with the run, so the reported value cannot be the
    # default zero: each line is written before it is appended, so line n
    # reports n whatever order the documents finish in.
    await clock.run(
        _batch(state, extract, clock, report=lines.append, degradations=lambda: len(lines)).run(ids)
    )

    assert len(lines) == DOCUMENT_COUNT
    assert {line.split()[1] for line in lines} == set(ids)
    for index in range(1, DOCUMENT_COUNT + 1):
        assert sum(line.startswith(f"[{index}/{DOCUMENT_COUNT}] ") for line in lines) == 1
    assert all(ExtractionRoute.SCANNED_PDF.value in line for line in lines)
    assert all(f"{DOCUMENT_SECONDS:.1f}s" in line for line in lines)
    assert [line.rpartition("degraded=")[2] for line in lines] == [
        str(index) for index in range(DOCUMENT_COUNT)
    ]


async def test_progress_goes_to_stderr_so_stdout_stays_parseable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    clock = VirtualClock()
    ids = doc_ids(1)
    state = _state(tmp_path / "runstate.db", ids)

    async def extract(doc_id: str) -> DocumentOutcome:
        return DocumentOutcome(document=_document(doc_id))

    await clock.run(
        BatchExtractor(state=state, run_id=RUN_ID, extract=extract, clock=clock).run(ids)
    )

    captured = capsys.readouterr()
    assert captured.out == ""
    assert ids[0] in captured.err
