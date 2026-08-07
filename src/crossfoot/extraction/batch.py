"""Bounded-concurrency extraction over independent documents.

One live vision call measured 17 to 41 seconds, so roughly 180 calls run
sequentially is hours. Documents do not depend on each other, so a semaphore
bounds how many are in flight while the rate limiter keeps deciding when a
request may go out.

Concurrency changes nothing a run can be read by. One lock serializes the
checkpoint writes, because a sqlite connection tolerates one writer; a document
reaches DONE only after its result is persisted, so a killed run resumes without
paying for it twice; and the documents are sorted by doc_id before they are
returned, so completion order can never move the output.

DONE is final, so only a failure the document owns may earn it. A failure the
run owns stays FAILED and the next pass takes it again.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass

from crossfoot.extraction.failures import FailureClass, failure_class_of, is_provider_failure
from crossfoot.llm.ratelimit import Clock, MonotonicClock
from crossfoot.llm.runstate import DocStatus, RunState
from crossfoot.models.extraction import ExtractedDocument

_LOGGER = logging.getLogger(__name__)

# Four in flight keeps a free tier busy without turning every provider limit
# into the bottleneck; the rate limiter still paces what actually goes out.
DEFAULT_EXTRACT_CONCURRENCY = 4

UNKNOWN_FAILURE_DETAIL = "extraction failed without a detail"
# Printed in place of a route for a document that never produced one.
FAILED_ROUTE_LABEL = "failed"


@dataclass(frozen=True, slots=True)
class DocumentOutcome:
    """What one document produced: a document to persist, or a failure to record."""

    document: ExtractedDocument | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class BatchResult:
    """One pass over a batch. Documents are sorted by doc_id, never by finish time."""

    documents: tuple[ExtractedDocument, ...]
    # Disjoint counts, because a run with unfinished work must not read as
    # complete: unprocessable is finished, pending_retry is still owed.
    unprocessable: int
    pending_retry: int
    skipped: int


ExtractOne = Callable[[str], Awaitable[DocumentOutcome]]
Report = Callable[[str], None]


def report_to_stderr(line: str) -> None:
    """Progress goes to stderr so stdout stays parseable."""
    print(line, file=sys.stderr, flush=True)


def reset_provider_failures(state: RunState, run_id: str, doc_ids: Iterable[str]) -> int:
    """Re-open DONE rows whose only result is a provider failure; returns how many.

    Rows written before transient and permanent were told apart are DONE and
    carry a provider failure as their result, so `--resume` skips them forever
    and the only cure was deleting the run state along with everything that
    worked. Idempotent: a re-opened row keeps no result, so a second call over
    the same run finds nothing left to move.
    """
    reopened = 0
    for doc_id in doc_ids:
        if state.status(run_id, doc_id) is not DocStatus.DONE:
            continue
        stored = state.result(run_id, doc_id)
        if stored is None or not is_provider_failure(ExtractedDocument.model_validate_json(stored)):
            continue
        state.reset_to_pending(run_id, doc_id)
        reopened += 1
    return reopened


def _failure_detail(outcome: DocumentOutcome) -> str:
    """What a not-done row records: the raised error, or the document's own."""
    if outcome.error is not None:
        return outcome.error
    if outcome.document is not None and outcome.document.error is not None:
        return outcome.document.error.detail
    return UNKNOWN_FAILURE_DETAIL


def _no_degradations() -> int:
    return 0


class BatchExtractor:
    """Runs one extraction pass over a batch of documents, bounded and checkpointed."""

    def __init__(
        self,
        *,
        state: RunState,
        run_id: str,
        extract: ExtractOne,
        concurrency: int = DEFAULT_EXTRACT_CONCURRENCY,
        clock: Clock | None = None,
        degradations: Callable[[], int] = _no_degradations,
        report: Report = report_to_stderr,
        # Configuration faults, unlike anything one document's bytes can do, are
        # worth ending the batch for; everything else stays a failed document.
        fatal: tuple[type[Exception], ...] = (),
    ) -> None:
        if concurrency < 1:
            raise ValueError("concurrency must be at least 1")
        self._state = state
        self._run_id = run_id
        self._extract = extract
        self._concurrency = concurrency
        self._clock = MonotonicClock() if clock is None else clock
        self._degradations = degradations
        self._report = report
        self._fatal = fatal
        # One writer at a time, and no checkpoint write interleaved with another.
        self._writes = asyncio.Lock()
        self._completed = 0
        self._total = 0

    async def run(self, doc_ids: Sequence[str], *, resume: bool = False) -> BatchResult:
        """Extract every pending document concurrently and report as each lands."""
        pending = [doc_id for doc_id in doc_ids if not (resume and self._is_done(doc_id))]
        self._completed = 0
        self._total = len(pending)
        semaphore = asyncio.Semaphore(self._concurrency)
        settled = await asyncio.gather(
            *(self._one(semaphore, doc_id) for doc_id in pending), return_exceptions=True
        )
        outcomes: list[DocumentOutcome] = []
        for outcome in settled:
            # Only a fatal error reaches here; every worker guards its own document.
            if isinstance(outcome, BaseException):
                raise outcome
            outcomes.append(outcome)
        # Classified once, then read three ways, so no count can drift from the
        # checkpoint that was already written for the same outcome.
        classified = [(outcome, failure_class_of(outcome.document)) for outcome in outcomes]
        extracted = [
            outcome.document
            for outcome, failure in classified
            if failure is None and outcome.document is not None
        ]
        return BatchResult(
            documents=tuple(sorted(extracted, key=lambda document: document.doc_id)),
            unprocessable=sum(1 for _, f in classified if f is FailureClass.PERMANENT),
            pending_retry=sum(1 for _, f in classified if f is FailureClass.TRANSIENT),
            skipped=len(doc_ids) - len(pending),
        )

    async def _one(self, semaphore: asyncio.Semaphore, doc_id: str) -> DocumentOutcome:
        """One document from IN_PROGRESS to persisted, holding a concurrency slot."""
        async with semaphore:
            async with self._writes:
                self._state.mark_in_progress(self._run_id, doc_id)
            started = self._clock.now()
            outcome = await self._guarded(doc_id)
            async with self._writes:
                self._persist(doc_id, outcome)
            self._progress(doc_id, outcome, self._clock.now() - started)
            return outcome

    async def _guarded(self, doc_id: str) -> DocumentOutcome:
        """No single document may end the batch, whatever its extractor does."""
        try:
            return await self._extract(doc_id)
        except self._fatal:
            raise
        except Exception as error:
            _LOGGER.warning("%s failed to extract: %s", doc_id, error)
            return DocumentOutcome(error=str(error))

    def _persist(self, doc_id: str, outcome: DocumentOutcome) -> None:
        """DONE carries the result, so resuming never re-extracts a finished document.

        A failure the run caused is not a finished document, so it is checkpointed
        FAILED and stays in pending_docs for the next pass to take.
        """
        document = outcome.document
        if document is None or failure_class_of(document) is FailureClass.TRANSIENT:
            self._state.mark_failed(self._run_id, doc_id, _failure_detail(outcome))
            return
        self._state.mark_done(self._run_id, doc_id, document.model_dump_json())

    def _progress(self, doc_id: str, outcome: DocumentOutcome, elapsed: float) -> None:
        """One line per completed document: a long run has to be supervisable."""
        self._completed += 1
        route = FAILED_ROUTE_LABEL if outcome.document is None else outcome.document.route.value
        self._report(
            f"[{self._completed}/{self._total}] {doc_id} {route}"
            f" {elapsed:.1f}s degraded={self._degradations()}"
        )

    def _is_done(self, doc_id: str) -> bool:
        return self._state.status(self._run_id, doc_id) is DocStatus.DONE
