"""Per-document checkpoints so a killed run resumes instead of restarting.

One row per (run_id, doc_id). Only DONE is skipped on resume: a run killed
mid-document leaves an IN_PROGRESS row whose work never finished, and a FAILED
document is worth another pass. The provider was already paid for the attempt
that died, so the ledger keeps that charge and the resumed attempt adds its own.

DONE therefore means finished for good, whether the document extracted or failed
on its own bytes. A failure the run caused rather than the document stays FAILED,
so the next pass owes it; crossfoot.extraction.failures decides which is which.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from crossfoot.db import connect


class DocStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    FAILED = "failed"


_SCHEMA = """
CREATE TABLE IF NOT EXISTS run_state (
    run_id TEXT NOT NULL,
    doc_id TEXT NOT NULL,
    doc_order INTEGER NOT NULL,
    status TEXT NOT NULL,
    result_json TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (run_id, doc_id)
)
"""

# Starting a run again must not undo what the previous pass finished.
_INSERT_PENDING = """
INSERT INTO run_state (
    run_id, doc_id, doc_order, status, result_json, error, created_at, updated_at
) VALUES (?, ?, ?, ?, NULL, NULL, ?, ?)
ON CONFLICT (run_id, doc_id) DO NOTHING
"""

_SELECT_PENDING = """
SELECT doc_id FROM run_state
WHERE run_id = ? AND status != ?
ORDER BY doc_order
"""

_SELECT_STATUS = "SELECT status FROM run_state WHERE run_id = ? AND doc_id = ?"
_SELECT_RUN_START = "SELECT MIN(created_at) AS started_at FROM run_state WHERE run_id = ?"
_SELECT_RESULT = "SELECT result_json FROM run_state WHERE run_id = ? AND doc_id = ?"

_UPDATE_STATUS = """
UPDATE run_state
SET status = ?, result_json = ?, error = ?, updated_at = ?
WHERE run_id = ? AND doc_id = ?
"""


class UnknownDocumentError(KeyError):
    """Raised when a checkpoint names a document the run never started."""


class RunState:
    def __init__(self, db_path: Path) -> None:
        self._connection = connect(db_path)
        with self._connection:
            self._connection.execute(_SCHEMA)

    def start_run(self, run_id: str, doc_ids: Sequence[str]) -> None:
        now = _now()
        with self._connection:
            self._connection.executemany(
                _INSERT_PENDING,
                [
                    (run_id, doc_id, order, DocStatus.PENDING.value, now, now)
                    for order, doc_id in enumerate(doc_ids)
                ],
            )

    def pending_docs(self, run_id: str) -> tuple[str, ...]:
        """Snapshot of what is left to do, in the order the run was started with."""
        cursor = self._connection.execute(_SELECT_PENDING, (run_id, DocStatus.DONE.value))
        return tuple(str(record["doc_id"]) for record in cursor.fetchall())

    def status(self, run_id: str, doc_id: str) -> DocStatus:
        record = self._connection.execute(_SELECT_STATUS, (run_id, doc_id)).fetchone()
        if record is None:
            raise UnknownDocumentError(f"{doc_id} is not part of run {run_id}")
        return DocStatus(record["status"])

    def run_started_at(self, run_id: str) -> str | None:
        """When this run first checkpointed a document, or None when it never has.

        `start_run` rewrites the run's rows, so this is the surviving attempt's
        start rather than the first time the id was ever used. That is what makes
        it usable for scoping a ledger read to the attempt whose output was kept.
        """
        record = self._connection.execute(_SELECT_RUN_START, (run_id,)).fetchone()
        started = None if record is None else record["started_at"]
        return None if started is None else str(started)

    def result(self, run_id: str, doc_id: str) -> str | None:
        record = self._connection.execute(_SELECT_RESULT, (run_id, doc_id)).fetchone()
        if record is None:
            raise UnknownDocumentError(f"{doc_id} is not part of run {run_id}")
        value = record["result_json"]
        return None if value is None else str(value)

    def mark_in_progress(self, run_id: str, doc_id: str) -> None:
        self._set(run_id, doc_id, DocStatus.IN_PROGRESS, result_json=None, error=None)

    def mark_done(self, run_id: str, doc_id: str, result_json: str) -> None:
        self._set(run_id, doc_id, DocStatus.DONE, result_json=result_json, error=None)

    def mark_failed(self, run_id: str, doc_id: str, error: str) -> None:
        self._set(run_id, doc_id, DocStatus.FAILED, result_json=None, error=error)

    def reset_to_pending(self, run_id: str, doc_id: str) -> None:
        """Put a document back in the queue, dropping the result it carried.

        The result goes because a row that is not DONE has nothing to report:
        keeping it would put a superseded answer in the extractions file.
        """
        self._set(run_id, doc_id, DocStatus.PENDING, result_json=None, error=None)

    def close(self) -> None:
        self._connection.close()

    def _set(
        self,
        run_id: str,
        doc_id: str,
        status: DocStatus,
        *,
        result_json: str | None,
        error: str | None,
    ) -> None:
        with self._connection:
            cursor = self._connection.execute(
                _UPDATE_STATUS,
                (status.value, result_json, error, _now(), run_id, doc_id),
            )
        if cursor.rowcount == 0:
            raise UnknownDocumentError(f"{doc_id} is not part of run {run_id}")


def _now() -> str:
    return datetime.now(UTC).isoformat()
