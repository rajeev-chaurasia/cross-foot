"""Cost per document survives a price table that learned a model after the run.

The ledger rows for the first local runs stored `list_price_microusd = 0`,
because `qwen2.5vl` had no price table entry when they were written. The stored
column is the record of what was believed at the time and stays as it is; the
published number is repriced from the table as it reads now, so a stale zero
never becomes the headline figure.
"""

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from crossfoot.api.app import create_app
from crossfoot.constants import ExtractionRoute, QualityTier
from crossfoot.costs.ledger import list_price_microusd
from crossfoot.db import connect
from crossfoot.db.schema import ensure_schema
from crossfoot.db.stats import summary

DB_NAME = "crossfoot.db"

# The model the local vision runs used, priced by explicit equivalence in
# constants.py after those runs had already been written.
SELF_HOSTED_MODEL = "qwen2.5vl:7b"
UNPRICED_MODEL = "some-model-nobody-priced"
PRICED_MODEL = "gemini-3.5-flash"

PROMPT_TOKENS = 375_311
COMPLETION_TOKENS = 125_363
DOCUMENTS = 2

_INSERT_DOCUMENT = """
INSERT INTO documents (doc_id, file_path, quality_tier, route)
VALUES (?, ?, ?, ?)
"""

_INSERT_CALL = """
INSERT INTO llm_calls (
    call_id, run_id, doc_id, purpose, provider, model,
    prompt_tokens, completion_tokens, total_tokens, cached,
    latency_ms, http_status, attempt, created_at,
    actual_cost_microusd, list_price_microusd
) VALUES (?, 'run-1', 'doc-1', 'extract', 'custom', ?, ?, ?, ?, 0, 100, 200, 1,
          '2026-08-08T12:00:00Z', 0, ?)
"""


def _seed(db_path: Path, *, model: str, stored_microusd: int) -> None:
    connection = connect(db_path)
    try:
        with connection:
            ensure_schema(connection)
            for index in range(DOCUMENTS):
                connection.execute(
                    _INSERT_DOCUMENT,
                    (
                        f"doc-{index}",
                        f"files/doc-{index}.pdf",
                        QualityTier.SCAN_HEAVY,
                        "scanned_pdf",
                    ),
                )
            connection.execute(
                _INSERT_CALL,
                (
                    "call-1",
                    model,
                    PROMPT_TOKENS,
                    COMPLETION_TOKENS,
                    PROMPT_TOKENS + COMPLETION_TOKENS,
                    stored_microusd,
                ),
            )
    finally:
        connection.close()


def _summary_row(db_path: Path) -> sqlite3.Row:
    connection = connect(db_path)
    try:
        return summary(connection)
    finally:
        connection.close()


def _stored_total(db_path: Path) -> int:
    connection = connect(db_path)
    try:
        (total,) = connection.execute("SELECT SUM(list_price_microusd) FROM llm_calls").fetchone()
    finally:
        connection.close()
    return int(total)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / DB_NAME


def test_cost_per_document_is_nonzero_when_the_stored_price_is_a_stale_zero(
    db_path: Path, tmp_path: Path
) -> None:
    _seed(db_path, model=SELF_HOSTED_MODEL, stored_microusd=0)
    expected = list_price_microusd(
        SELF_HOSTED_MODEL, prompt_tokens=PROMPT_TOKENS, completion_tokens=COMPLETION_TOKENS
    )
    assert expected > 0

    crops = tmp_path / "crops"
    crops.mkdir()
    scorecards = tmp_path / "scorecards"
    scorecards.mkdir()
    app = create_app(db_path=db_path, crops_root=crops, scorecards_dir=scorecards)
    with TestClient(app) as client:
        payload = client.get("/api/stats/summary").json()

    assert payload["cost_per_document_microusd"] == expected // DOCUMENTS
    assert payload["cost_per_document_microusd"] > 0
    # The column itself is untouched: it records what the run believed it spent.
    assert _stored_total(db_path) == 0


def test_a_stored_price_the_table_knew_still_stands(db_path: Path) -> None:
    # Repricing is a repair for a gap in the table, not a recomputation of every
    # row, so a run that priced itself keeps the number it published.
    _seed(db_path, model=PRICED_MODEL, stored_microusd=36_000)
    assert int(_summary_row(db_path)["list_price_microusd"]) == 36_000


def test_a_model_the_table_still_does_not_know_reports_zero(db_path: Path) -> None:
    _seed(db_path, model=UNPRICED_MODEL, stored_microusd=0)
    assert int(_summary_row(db_path)["list_price_microusd"]) == 0


def test_an_unprocessable_document_stays_out_of_the_denominator(
    db_path: Path, tmp_path: Path
) -> None:
    _seed(db_path, model=SELF_HOSTED_MODEL, stored_microusd=0)
    connection = connect(db_path)
    try:
        with connection:
            connection.execute(
                _INSERT_DOCUMENT,
                (
                    "doc-bad",
                    "files/doc-bad.pdf",
                    QualityTier.CORRUPTED,
                    ExtractionRoute.UNPROCESSABLE.value,
                ),
            )
        row = summary(connection)
    finally:
        connection.close()
    assert int(row["documents_processed"]) == DOCUMENTS
