"""Contract tests for run checkpointing and --resume.

Written against docs/contracts-phase2.md before the implementation exists. The
central test kills a five document run partway through the third document,
resumes it, and checks that the finished documents are neither re-extracted nor
charged twice.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from crossfoot.constants import PROVIDER_DEFAULT_MODELS, Provider

runstate = pytest.importorskip("crossfoot.llm.runstate")
costs = pytest.importorskip("crossfoot.costs")

RUN_ID = "run-resume-01"
OTHER_RUN_ID = "run-resume-02"
DOC_IDS = ("doc-01", "doc-02", "doc-03", "doc-04", "doc-05")
MODEL = PROVIDER_DEFAULT_MODELS[Provider.GEMINI]


class KilledRunError(RuntimeError):
    """Stands in for the process dying partway through a document."""


class FakeExtractor:
    """Charges the ledger the way a real call does, then maybe dies."""

    def __init__(self, book: Any, fail_on: str | None) -> None:
        self._book = book
        self._fail_on = fail_on
        self.calls: list[str] = []

    def extract(self, doc_id: str) -> str:
        self.calls.append(doc_id)
        # The provider is paid when the call goes out, before anything can fail.
        self._book.record(
            run_id=RUN_ID,
            doc_id=doc_id,
            purpose=costs.Purpose.EXTRACT,
            provider=Provider.GEMINI,
            model=MODEL,
            prompt_tokens=100,
            completion_tokens=10,
            total_tokens=130,
            cached=False,
            latency_ms=250,
            http_status=200,
            attempt=1,
            actual_cost_microusd=0,
        )
        if doc_id == self._fail_on:
            raise KilledRunError(doc_id)
        return json.dumps({"doc_id": doc_id})


def run_pass(state: Any, extractor: FakeExtractor) -> None:
    state.start_run(RUN_ID, DOC_IDS)
    for doc_id in state.pending_docs(RUN_ID):
        state.mark_in_progress(RUN_ID, doc_id)
        payload = extractor.extract(doc_id)
        state.mark_done(RUN_ID, doc_id, payload)


@dataclass(frozen=True)
class ResumedRun:
    state: Any
    book: Any
    first: FakeExtractor
    second: FakeExtractor


def new_state(tmp_path: Path) -> Any:
    return runstate.RunState(tmp_path / "runstate.db")


def new_ledger(tmp_path: Path) -> Any:
    return costs.CostLedger(tmp_path / "costs.db")


def charges_per_doc(book: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in book.rows(RUN_ID):
        counts[row.doc_id] = counts.get(row.doc_id, 0) + 1
    return counts


@pytest.fixture
def resumed(tmp_path: Path) -> ResumedRun:
    """One run killed inside document 3, then resumed to completion."""
    state = new_state(tmp_path)
    book = new_ledger(tmp_path)
    first = FakeExtractor(book, fail_on="doc-03")
    with pytest.raises(KilledRunError):
        run_pass(state, first)
    second = FakeExtractor(book, fail_on=None)
    run_pass(state, second)
    return ResumedRun(state=state, book=book, first=first, second=second)


# Row identity and statuses.


def test_status_enum_has_the_four_frozen_values() -> None:
    assert {member.name for member in runstate.DocStatus} == {
        "PENDING",
        "IN_PROGRESS",
        "DONE",
        "FAILED",
    }


def test_start_run_creates_one_pending_row_per_document(tmp_path: Path) -> None:
    state = new_state(tmp_path)
    state.start_run(RUN_ID, DOC_IDS)
    assert state.pending_docs(RUN_ID) == DOC_IDS
    assert all(state.status(RUN_ID, doc_id) is runstate.DocStatus.PENDING for doc_id in DOC_IDS)


def test_pending_docs_is_a_snapshot_tuple(tmp_path: Path) -> None:
    state = new_state(tmp_path)
    state.start_run(RUN_ID, DOC_IDS)
    assert isinstance(state.pending_docs(RUN_ID), tuple)


def test_starting_the_same_run_twice_keeps_one_row_per_document(tmp_path: Path) -> None:
    state = new_state(tmp_path)
    state.start_run(RUN_ID, DOC_IDS)
    state.start_run(RUN_ID, DOC_IDS)
    connection = sqlite3.connect(tmp_path / "runstate.db")
    try:
        (count,) = connection.execute(
            "SELECT COUNT(*) FROM run_state WHERE run_id = ?", (RUN_ID,)
        ).fetchone()
    finally:
        connection.close()
    assert count == len(DOC_IDS)


def test_restarting_a_run_does_not_reset_finished_documents(tmp_path: Path) -> None:
    state = new_state(tmp_path)
    state.start_run(RUN_ID, DOC_IDS)
    state.mark_done(RUN_ID, "doc-01", "{}")
    state.start_run(RUN_ID, DOC_IDS)
    assert state.status(RUN_ID, "doc-01") is runstate.DocStatus.DONE


def test_two_runs_track_the_same_document_independently(tmp_path: Path) -> None:
    state = new_state(tmp_path)
    state.start_run(RUN_ID, DOC_IDS)
    state.start_run(OTHER_RUN_ID, DOC_IDS)
    state.mark_done(RUN_ID, "doc-01", "{}")
    assert state.status(RUN_ID, "doc-01") is runstate.DocStatus.DONE
    assert state.status(OTHER_RUN_ID, "doc-01") is runstate.DocStatus.PENDING


def test_the_result_blob_round_trips(tmp_path: Path) -> None:
    state = new_state(tmp_path)
    state.start_run(RUN_ID, DOC_IDS)
    state.mark_done(RUN_ID, "doc-02", json.dumps({"doc_id": "doc-02", "lines": 3}))
    assert json.loads(state.result(RUN_ID, "doc-02")) == {"doc_id": "doc-02", "lines": 3}


# Resume selection.


def test_resume_skips_done_and_reprocesses_in_progress(tmp_path: Path) -> None:
    # A killed run leaves IN_PROGRESS rows dangling, so they get redone.
    state = new_state(tmp_path)
    state.start_run(RUN_ID, DOC_IDS)
    state.mark_done(RUN_ID, "doc-01", "{}")
    state.mark_done(RUN_ID, "doc-02", "{}")
    state.mark_in_progress(RUN_ID, "doc-03")
    assert state.pending_docs(RUN_ID) == ("doc-03", "doc-04", "doc-05")


def test_resume_reprocesses_failed_documents(tmp_path: Path) -> None:
    state = new_state(tmp_path)
    state.start_run(RUN_ID, DOC_IDS)
    state.mark_failed(RUN_ID, "doc-01", "schema validation failed twice")
    assert state.status(RUN_ID, "doc-01") is runstate.DocStatus.FAILED
    assert "doc-01" in state.pending_docs(RUN_ID)


def test_a_finished_run_has_nothing_left_to_do(tmp_path: Path) -> None:
    state = new_state(tmp_path)
    state.start_run(RUN_ID, DOC_IDS)
    for doc_id in DOC_IDS:
        state.mark_done(RUN_ID, doc_id, "{}")
    assert state.pending_docs(RUN_ID) == ()


def test_checkpoints_survive_reopening_the_database(tmp_path: Path) -> None:
    state = new_state(tmp_path)
    state.start_run(RUN_ID, DOC_IDS)
    state.mark_done(RUN_ID, "doc-01", "{}")
    reopened = new_state(tmp_path)
    assert reopened.status(RUN_ID, "doc-01") is runstate.DocStatus.DONE
    assert reopened.pending_docs(RUN_ID) == ("doc-02", "doc-03", "doc-04", "doc-05")


# The killed run.


def test_the_killed_pass_stops_inside_document_three(resumed: ResumedRun) -> None:
    assert resumed.first.calls == ["doc-01", "doc-02", "doc-03"]


def test_resume_does_not_re_extract_finished_documents(resumed: ResumedRun) -> None:
    assert resumed.second.calls == ["doc-03", "doc-04", "doc-05"]


def test_resume_does_not_double_charge_finished_documents(resumed: ResumedRun) -> None:
    # doc-03 was charged before the kill and again on resume, which is honest.
    # doc-01 and doc-02 must never be charged twice.
    assert charges_per_doc(resumed.book) == {
        "doc-01": 1,
        "doc-02": 1,
        "doc-03": 2,
        "doc-04": 1,
        "doc-05": 1,
    }


def test_every_document_ends_done(resumed: ResumedRun) -> None:
    assert all(
        resumed.state.status(RUN_ID, doc_id) is runstate.DocStatus.DONE for doc_id in DOC_IDS
    )
    assert resumed.state.pending_docs(RUN_ID) == ()


def test_the_resumed_run_keeps_the_results_from_the_first_pass(resumed: ResumedRun) -> None:
    assert json.loads(resumed.state.result(RUN_ID, "doc-01")) == {"doc_id": "doc-01"}
    assert json.loads(resumed.state.result(RUN_ID, "doc-05")) == {"doc_id": "doc-05"}
