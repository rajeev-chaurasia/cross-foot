"""What a resume owes, and what it must never do twice.

A live 105 document run spilled 33 scans over to a text only model that answered
400 for every one of them. Each became an UNPROCESSABLE result and each was
checkpointed DONE, so `--resume` skipped them forever and the only cure was
deleting the run state along with the 64 documents that had succeeded.

Everything here is offline: the extractors are fakes and the vision client is
canned, so no provider is ever called.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from pdf_fixtures import TRUTH_DOC
from typer.testing import CliRunner

from crossfoot import cli
from crossfoot.constants import (
    CorruptionKind,
    DocType,
    ExtractionRoute,
    IngestErrorKind,
)
from crossfoot.extraction.batch import (
    BatchExtractor,
    DocumentOutcome,
    reset_provider_failures,
    reset_stale_unrecognized,
)
from crossfoot.extraction.failures import (
    FAILURE_CLASSES,
    PROVIDER_FAILURE_DETAIL,
    FailureClass,
    failure_class_of,
)
from crossfoot.extraction.llm_vision import SCHEMA_FAILURE_DETAIL, PageImage, VisionExtractor
from crossfoot.generator.corrupt import build_minimal_pdf, write_corrupted
from crossfoot.generator.renderers.tabular import render_xlsx
from crossfoot.llm.client import ChatResult, ChatUsage
from crossfoot.llm.results import LlmError
from crossfoot.llm.runstate import DocStatus, RunState
from crossfoot.models.extraction import ExtractedDocument, IngestError

RUN_ID = "run-resume-classification"
DOC_IDS = ("doc-01", "doc-02", "doc-03")
SERVED, LOST, CORRUPTED = DOC_IDS

# The fake client never decodes these bytes; they only prove a call was shaped.
PNG_BYTES = b"\x89PNG\r\n\x1a\n"
# Schema invalid twice over: row_position is not an integer, so the sample and
# its one repair both fail validation and the document is the reason.
BAD_SAMPLE = json.dumps({"lines": [{"row_position": "ROW-TWO"}]})

NO_EXTRACTOR_DETAIL = cli.NO_EXTRACTOR_DETAIL.format(route=ExtractionRoute.XLSX.value)
# The template TRUTH_DOC is written with when a test needs real workbook bytes.
XLSX_TEMPLATE = "meridian-parts_statement-xlsx-v1"


# ---------------------------------------------------------------------------
# Documents, states, and batches
# ---------------------------------------------------------------------------


def _extracted(doc_id: str) -> ExtractedDocument:
    return ExtractedDocument(
        doc_id=doc_id, file_path=f"files/{doc_id}.pdf", route=ExtractionRoute.SCANNED_PDF
    )


def _unprocessable(doc_id: str, kind: IngestErrorKind, detail: str) -> ExtractedDocument:
    return ExtractedDocument(
        doc_id=doc_id,
        file_path=f"files/{doc_id}.pdf",
        route=ExtractionRoute.UNPROCESSABLE,
        error=IngestError(kind=kind, detail=detail),
    )


def _provider_failure(doc_id: str) -> ExtractedDocument:
    """What the vision extractor returns once the whole provider chain is spent."""
    return _unprocessable(
        doc_id,
        IngestErrorKind.PROVIDER_UNAVAILABLE,
        f"{PROVIDER_FAILURE_DETAIL}: groq: 400 from a text only model",
    )


def _legacy_provider_failure(doc_id: str) -> ExtractedDocument:
    """The shape the live run stored, before the failure had a kind of its own."""
    return _unprocessable(
        doc_id,
        IngestErrorKind.UNRECOGNIZED,
        f"{PROVIDER_FAILURE_DETAIL}: groq: 400 from a text only model",
    )


def _corrupted(doc_id: str) -> ExtractedDocument:
    return _unprocessable(doc_id, IngestErrorKind.TRUNCATED, "unreadable pdf: EOF marker missing")


def _state(tmp_path: Path, ids: Sequence[str] = DOC_IDS) -> RunState:
    state = RunState(tmp_path / "runstate.db")
    state.start_run(RUN_ID, ids)
    return state


def _discard(line: str) -> None:
    """Progress has its own tests; everywhere else it stays out of the way."""


def _batch(state: RunState, extract: Any) -> BatchExtractor:
    return BatchExtractor(state=state, run_id=RUN_ID, extract=extract, report=_discard)


def _serving(doc_id: str) -> DocumentOutcome:
    return DocumentOutcome(document=_extracted(doc_id))


async def _first_pass(doc_id: str) -> DocumentOutcome:
    """One document extracts, one loses every provider, one is a corrupted file."""
    if doc_id == LOST:
        return DocumentOutcome(document=_provider_failure(doc_id))
    if doc_id == CORRUPTED:
        return DocumentOutcome(document=_corrupted(doc_id))
    return _serving(doc_id)


# ---------------------------------------------------------------------------
# The classification itself
# ---------------------------------------------------------------------------


def test_every_ingest_error_kind_is_classified() -> None:
    # A kind with no entry would silently fall back, so the table stays complete.
    assert set(FAILURE_CLASSES) == set(IngestErrorKind)


def test_infrastructure_is_transient_and_the_document_itself_is_permanent() -> None:
    assert failure_class_of(_provider_failure(LOST)) is FailureClass.TRANSIENT
    assert failure_class_of(_corrupted(CORRUPTED)) is FailureClass.PERMANENT
    assert (
        failure_class_of(
            _unprocessable(CORRUPTED, IngestErrorKind.UNRECOGNIZED, NO_EXTRACTOR_DETAIL)
        )
        is FailureClass.PERMANENT
    )
    assert failure_class_of(_extracted(SERVED)) is None


# ---------------------------------------------------------------------------
# A transient failure is unfinished work
# ---------------------------------------------------------------------------


async def test_a_provider_failure_leaves_the_document_pending_and_a_resume_retries_it(
    tmp_path: Path,
) -> None:
    state = _state(tmp_path)
    result = await _batch(state, _first_pass).run(DOC_IDS)

    assert result.pending_retry == 1
    assert state.status(RUN_ID, LOST) is not DocStatus.DONE
    assert LOST in state.pending_docs(RUN_ID)

    retried: list[str] = []

    async def extract(doc_id: str) -> DocumentOutcome:
        retried.append(doc_id)
        return _serving(doc_id)

    await _batch(state, extract).run(DOC_IDS, resume=True)
    assert retried == [LOST]


async def test_a_permanent_failure_is_not_retried_on_resume(tmp_path: Path) -> None:
    ids = (SERVED, CORRUPTED, "doc-xlsx")
    state = _state(tmp_path, ids)

    async def extract(doc_id: str) -> DocumentOutcome:
        if doc_id == CORRUPTED:
            return DocumentOutcome(document=_corrupted(doc_id))
        if doc_id == "doc-xlsx":
            return DocumentOutcome(
                document=_unprocessable(doc_id, IngestErrorKind.UNRECOGNIZED, NO_EXTRACTOR_DETAIL)
            )
        return _serving(doc_id)

    result = await _batch(state, extract).run(ids)
    assert result.unprocessable == 2
    assert result.pending_retry == 0
    assert state.pending_docs(RUN_ID) == ()

    retried: list[str] = []

    async def never(doc_id: str) -> DocumentOutcome:
        retried.append(doc_id)
        return _serving(doc_id)

    resumed = await _batch(state, never).run(ids, resume=True)
    assert retried == []
    assert resumed.skipped == len(ids)


async def test_a_schema_failure_after_the_repair_is_the_document_own_fault() -> None:
    # The real extractor, so the classification is read off what it truly writes.
    client = _CannedVisionClient(responses=(BAD_SAMPLE, BAD_SAMPLE))
    doc = await _vision_document(client)

    assert doc.error is not None
    assert doc.error.detail == SCHEMA_FAILURE_DETAIL
    assert failure_class_of(doc) is FailureClass.PERMANENT


async def test_a_lost_provider_chain_is_the_run_fault() -> None:
    client = _CannedVisionClient(responses=(), error=LlmError("every profile is cooling down"))
    doc = await _vision_document(client)

    assert doc.error is not None
    assert doc.error.kind is IngestErrorKind.PROVIDER_UNAVAILABLE
    assert failure_class_of(doc) is FailureClass.TRANSIENT


# ---------------------------------------------------------------------------
# What a resumed run reports and writes
# ---------------------------------------------------------------------------


async def test_a_resumed_run_that_succeeds_takes_the_pending_count_to_zero(
    tmp_path: Path,
) -> None:
    state = _state(tmp_path)
    first = await _batch(state, _first_pass).run(DOC_IDS)
    assert first.pending_retry == 1

    async def extract(doc_id: str) -> DocumentOutcome:
        return _serving(doc_id)

    resumed = await _batch(state, extract).run(DOC_IDS, resume=True)
    assert resumed.pending_retry == 0
    assert state.pending_docs(RUN_ID) == ()


async def test_permanent_failures_from_an_earlier_pass_survive_a_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "EXTRACTIONS_DIR", tmp_path / "extractions")
    state = _state(tmp_path)
    await _batch(state, _first_pass).run(DOC_IDS)

    # The corrupted file is a real result; the lost one is not a result at all.
    assert [doc.doc_id for doc in cli._finished_documents(state, RUN_ID, DOC_IDS)] == [
        SERVED,
        CORRUPTED,
    ]

    async def extract(doc_id: str) -> DocumentOutcome:
        return _serving(doc_id)

    await _batch(state, extract).run(DOC_IDS, resume=True)
    finished = cli._finished_documents(state, RUN_ID, DOC_IDS)
    assert [doc.doc_id for doc in finished] == list(DOC_IDS)

    written = json.loads(cli._write_extractions(RUN_ID, finished).read_text(encoding="utf-8"))
    by_id = {doc["doc_id"]: doc for doc in written}
    assert by_id[CORRUPTED]["route"] == ExtractionRoute.UNPROCESSABLE.value
    assert by_id[CORRUPTED]["error"]["kind"] == IngestErrorKind.TRUNCATED.value
    assert by_id[LOST]["route"] == ExtractionRoute.SCANNED_PDF.value


# ---------------------------------------------------------------------------
# Recovering the rows the live run already checkpointed DONE
# ---------------------------------------------------------------------------


def _done_with(state: RunState, document: ExtractedDocument) -> None:
    state.mark_done(RUN_ID, document.doc_id, document.model_dump_json())


def test_reclassification_reopens_provider_failures_and_leaves_the_rest_alone(
    tmp_path: Path,
) -> None:
    state = _state(tmp_path)
    _done_with(state, _extracted(SERVED))
    _done_with(state, _legacy_provider_failure(LOST))
    _done_with(state, _corrupted(CORRUPTED))

    assert reset_provider_failures(state, RUN_ID, DOC_IDS) == 1

    assert state.status(RUN_ID, LOST) is DocStatus.PENDING
    assert state.result(RUN_ID, LOST) is None
    assert state.pending_docs(RUN_ID) == (LOST,)
    # A success and a document that failed on its own bytes are both finished.
    assert state.status(RUN_ID, SERVED) is DocStatus.DONE
    assert state.status(RUN_ID, CORRUPTED) is DocStatus.DONE
    assert state.result(RUN_ID, CORRUPTED) is not None


def test_reclassification_is_idempotent(tmp_path: Path) -> None:
    state = _state(tmp_path)
    _done_with(state, _extracted(SERVED))
    _done_with(state, _legacy_provider_failure(LOST))
    _done_with(state, _provider_failure(CORRUPTED))

    assert reset_provider_failures(state, RUN_ID, DOC_IDS) == 2
    before = [state.status(RUN_ID, doc_id) for doc_id in DOC_IDS]
    # Nothing left to move, and nothing already moved is touched again.
    assert reset_provider_failures(state, RUN_ID, DOC_IDS) == 0
    assert [state.status(RUN_ID, doc_id) for doc_id in DOC_IDS] == before


# ---------------------------------------------------------------------------
# Rows whose unrecognized verdict a later router disagrees with
# ---------------------------------------------------------------------------


def _unprocessable_at(
    doc_id: str, path: Path, kind: IngestErrorKind, detail: str
) -> ExtractedDocument:
    """The same verdict as `_unprocessable`, on a file the router can be re-asked about."""
    return ExtractedDocument(
        doc_id=doc_id,
        file_path=path.as_posix(),
        route=ExtractionRoute.UNPROCESSABLE,
        error=IngestError(kind=kind, detail=detail),
    )


def _reset_stale(state: RunState) -> int:
    return reset_stale_unrecognized(
        state, RUN_ID, DOC_IDS, routes_served=cli._routes_with_extractors()
    )


def test_a_workbook_the_router_learned_to_read_is_reopened(tmp_path: Path) -> None:
    workbook = tmp_path / "book.xlsx"
    render_xlsx(TRUTH_DOC, XLSX_TEMPLATE, 3, workbook)
    state = _state(tmp_path)
    _done_with(state, _extracted(SERVED))
    _done_with(
        state,
        _unprocessable_at(LOST, workbook, IngestErrorKind.UNRECOGNIZED, NO_EXTRACTOR_DETAIL),
    )

    assert _reset_stale(state) == 1
    assert state.status(RUN_ID, LOST) is DocStatus.PENDING
    # The superseded verdict goes with it, so it cannot reach the output file.
    assert state.result(RUN_ID, LOST) is None
    assert state.status(RUN_ID, SERVED) is DocStatus.DONE
    # Nothing is DONE and stale any more, so a second resume moves nothing.
    assert _reset_stale(state) == 0


def test_a_file_the_router_still_places_nowhere_keeps_its_verdict(tmp_path: Path) -> None:
    junk = tmp_path / "junk.pdf"
    write_corrupted(CorruptionKind.BINARY_JUNK, 11, junk)
    state = _state(tmp_path)
    _done_with(
        state,
        _unprocessable_at(
            CORRUPTED, junk, IngestErrorKind.UNRECOGNIZED, "no recognized file signature"
        ),
    )

    assert _reset_stale(state) == 0
    assert state.status(RUN_ID, CORRUPTED) is DocStatus.DONE
    assert state.result(RUN_ID, CORRUPTED) is not None


def test_a_provider_failure_is_left_to_the_provider_reset(tmp_path: Path) -> None:
    scan = tmp_path / "scan.pdf"
    scan.write_bytes(build_minimal_pdf("x"))
    state = _state(tmp_path)
    # The legacy shape sits on a file the vision path serves, so only the detail
    # separates the two recoveries and exactly one of them may take the row.
    _done_with(
        state,
        _unprocessable_at(
            LOST,
            scan,
            IngestErrorKind.UNRECOGNIZED,
            f"{PROVIDER_FAILURE_DETAIL}: groq: 400 from a text only model",
        ),
    )

    assert _reset_stale(state) == 0
    assert state.status(RUN_ID, LOST) is DocStatus.DONE
    assert reset_provider_failures(state, RUN_ID, DOC_IDS) == 1
    assert state.status(RUN_ID, LOST) is DocStatus.PENDING
    assert _reset_stale(state) == 0


def test_a_run_with_nothing_stale_reopens_nothing(tmp_path: Path) -> None:
    state = _state(tmp_path)
    for doc_id in DOC_IDS:
        _done_with(state, _extracted(doc_id))

    assert _reset_stale(state) == 0
    assert [state.status(RUN_ID, doc_id) for doc_id in DOC_IDS] == [DocStatus.DONE] * len(DOC_IDS)
    assert state.pending_docs(RUN_ID) == ()


# ---------------------------------------------------------------------------
# The summary line
# ---------------------------------------------------------------------------


def test_the_summary_separates_unprocessable_from_pending_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from crossfoot.evals.runner import VisionDegradations

    async def counts(*args: Any, **kwargs: Any) -> cli.ExtractCounts:
        return cli.ExtractCounts(
            extracted=64,
            unprocessable=8,
            pending_retry=33,
            skipped=0,
            degradations=VisionDegradations(),
        )

    monkeypatch.setattr(cli, "_extract_split", counts)
    result = CliRunner().invoke(cli.app, ["extract"])

    assert result.exit_code == 0
    # A run holding 33 documents it never extracted must not read as complete.
    assert "64 extracted, 8 unprocessable, 33 pending retry, 0 already done" in result.stdout


# ---------------------------------------------------------------------------
# A canned vision client, so the real extractor runs with no network
# ---------------------------------------------------------------------------


class _CannedVisionClient:
    """Answers from a script, or raises the way a spent provider chain does."""

    def __init__(self, *, responses: Sequence[str], error: LlmError | None = None) -> None:
        self._responses = list(responses)
        self._error = error

    async def chat_vision(self, *args: Any, **kwargs: Any) -> ChatResult:
        if self._error is not None:
            raise self._error
        assert self._responses, "extractor issued more calls than the test scripted"
        return ChatResult(
            content=self._responses.pop(0),
            model="fake-vision",
            usage=ChatUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            latency_ms=1,
            rate_limit_headers={},
        )


async def _vision_document(client: _CannedVisionClient) -> ExtractedDocument:
    return await VisionExtractor(client).extract_document(
        doc_id=LOST,
        file_path=f"files/{LOST}.pdf",
        doc_type=DocType.PARTS_STATEMENT,
        images=(PageImage(page=0, png_bytes=PNG_BYTES),),
    )
