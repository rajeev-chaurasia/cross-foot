"""Direct sqlite3 seeding for the phase 3 API contract tests.

`crossfoot serve` materializes the database from a dataset directory plus its
saved extractions, and neither that command nor the ingest path behind it exists
yet, so these tests write rows straight into sqlite instead. The schema is the
one docs/contracts-phase3.md names, column for column, plus two additions the
same document forces and that the module docstrings of the test files record:

- `fields.signals`, holding `FieldSignals` JSON. The frozen column list carries
  no signals, but `GET /api/review/items/{field_id}` has to return "the signal
  breakdown that produced it", and a breakdown cannot be recomputed at read time
  from a row that never stored it.
- `llm_calls`, the phase 2 cost ledger table, unchanged from
  `crossfoot.costs.ledger`. The summary tile reports cost per document from the
  ledger's list price column, so the API has to be able to read it.

Values are the frozen enums from `crossfoot.constants` wherever the column holds
one, so a renamed member breaks these tests rather than silently drifting.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from crossfoot.constants import ExtractionRoute
from crossfoot.models.extraction import FieldSignals

DOCUMENTS_DDL = """
CREATE TABLE IF NOT EXISTS documents (
    doc_id TEXT PRIMARY KEY,
    file_path TEXT NOT NULL,
    doc_type TEXT,
    quality_tier TEXT NOT NULL,
    route TEXT NOT NULL,
    split TEXT,
    error_kind TEXT
)
"""

FIELDS_DDL = """
CREATE TABLE IF NOT EXISTS fields (
    field_id TEXT PRIMARY KEY,
    doc_id TEXT NOT NULL REFERENCES documents(doc_id),
    line_no INTEGER,
    name TEXT NOT NULL,
    family TEXT NOT NULL,
    raw_text TEXT,
    value TEXT,
    value_cents INTEGER,
    value_date TEXT,
    source TEXT NOT NULL,
    crop_kind TEXT NOT NULL,
    page INTEGER,
    x0 REAL,
    y0 REAL,
    x1 REAL,
    y1 REAL,
    confidence REAL NOT NULL,
    status TEXT NOT NULL,
    signals TEXT NOT NULL
)
"""

# Mirrors ExceptionRecord field for field, which is what "mirroring
# ExceptionRecord" in the contract has to mean for a table.
EXCEPTIONS_DDL = """
CREATE TABLE IF NOT EXISTS exceptions (
    exception_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    exception_type TEXT NOT NULL,
    doc_id TEXT,
    statement_line_no INTEGER,
    ledger_entry_id TEXT,
    match_key TEXT,
    statement_amount_cents INTEGER,
    ledger_amount_cents INTEGER,
    dollar_impact_cents INTEGER NOT NULL,
    memo_amount_cents INTEGER NOT NULL DEFAULT 0,
    explanation TEXT NOT NULL,
    status TEXT NOT NULL,
    detected_at TEXT NOT NULL
)
"""

CORRECTIONS_DDL = """
CREATE TABLE IF NOT EXISTS corrections (
    correction_id TEXT PRIMARY KEY,
    field_id TEXT NOT NULL REFERENCES fields(field_id),
    old_value TEXT,
    new_value TEXT NOT NULL,
    reviewer TEXT NOT NULL,
    created_at TEXT NOT NULL
)
"""

# Copied from crossfoot.costs.ledger so seeding never depends on that module's
# private schema constant; a drift between the two is a real contract break.
LLM_CALLS_DDL = """
CREATE TABLE IF NOT EXISTS llm_calls (
    call_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    doc_id TEXT NOT NULL,
    purpose TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_tokens INTEGER NOT NULL,
    completion_tokens INTEGER NOT NULL,
    total_tokens INTEGER NOT NULL,
    cached INTEGER NOT NULL,
    latency_ms INTEGER NOT NULL,
    http_status INTEGER NOT NULL,
    attempt INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    actual_cost_microusd INTEGER NOT NULL,
    list_price_microusd INTEGER NOT NULL
)
"""

SCHEMA_STATEMENTS: tuple[str, ...] = (
    DOCUMENTS_DDL,
    FIELDS_DDL,
    EXCEPTIONS_DDL,
    CORRECTIONS_DDL,
    LLM_CALLS_DDL,
)

DB_NAME = "crossfoot.db"


@contextmanager
def connection(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Open db_path in WAL mode, commit on a clean exit, always close."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        yield conn
        conn.commit()
    finally:
        conn.close()


def create_schema(conn: sqlite3.Connection) -> None:
    for statement in SCHEMA_STATEMENTS:
        conn.execute(statement)


def signals_json(
    route: ExtractionRoute,
    *,
    self_consistency: float | None = None,
    det_llm_agreement: float | None = None,
    validator_pass: float | None = None,
    grammar_match: float | None = None,
    crossfoot_ok: float | None = None,
    crossfoot_residual_suspect: bool = False,
    char_ambiguity: float = 0.0,
) -> str:
    """A FieldSignals payload as the fields table stores it."""
    return FieldSignals(
        self_consistency=self_consistency,
        det_llm_agreement=det_llm_agreement,
        validator_pass=validator_pass,
        grammar_match=grammar_match,
        crossfoot_ok=crossfoot_ok,
        crossfoot_residual_suspect=crossfoot_residual_suspect,
        char_ambiguity=char_ambiguity,
        route=route,
    ).model_dump_json()


def insert_document(
    conn: sqlite3.Connection,
    *,
    doc_id: str,
    quality_tier: str,
    route: str,
    file_path: str | None = None,
    doc_type: str | None = None,
    split: str | None = None,
    error_kind: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO documents (
            doc_id, file_path, doc_type, quality_tier, route, split, error_kind
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            doc_id,
            file_path if file_path is not None else f"files/{doc_id}.pdf",
            doc_type,
            quality_tier,
            route,
            split,
            error_kind,
        ),
    )


def insert_field(
    conn: sqlite3.Connection,
    *,
    field_id: str,
    doc_id: str,
    name: str,
    family: str,
    confidence: float,
    status: str,
    signals: str,
    line_no: int | None = None,
    raw_text: str | None = None,
    value: str | None = None,
    value_cents: int | None = None,
    value_date: str | None = None,
    source: str = "llm_vision",
    crop_kind: str = "row_band",
    page: int | None = 0,
    bbox: tuple[float, float, float, float] = (0.1, 0.2, 0.9, 0.3),
) -> None:
    x0, y0, x1, y1 = bbox
    conn.execute(
        """
        INSERT INTO fields (
            field_id, doc_id, line_no, name, family, raw_text, value,
            value_cents, value_date, source, crop_kind, page,
            x0, y0, x1, y1, confidence, status, signals
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            field_id,
            doc_id,
            line_no,
            name,
            family,
            raw_text,
            value,
            value_cents,
            value_date,
            source,
            crop_kind,
            page,
            x0,
            y0,
            x1,
            y1,
            confidence,
            status,
            signals,
        ),
    )


def insert_exception(
    conn: sqlite3.Connection,
    *,
    exception_id: str,
    exception_type: str,
    dollar_impact_cents: int,
    status: str,
    explanation: str,
    run_id: str = "run-contract-0001",
    doc_id: str | None = None,
    statement_line_no: int | None = None,
    ledger_entry_id: str | None = None,
    match_key: str | None = None,
    statement_amount_cents: int | None = None,
    ledger_amount_cents: int | None = None,
    memo_amount_cents: int = 0,
    detected_at: str = "2026-08-01T12:00:00Z",
) -> None:
    conn.execute(
        """
        INSERT INTO exceptions (
            exception_id, run_id, exception_type, doc_id, statement_line_no,
            ledger_entry_id, match_key, statement_amount_cents,
            ledger_amount_cents, dollar_impact_cents, memo_amount_cents,
            explanation, status, detected_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            exception_id,
            run_id,
            exception_type,
            doc_id,
            statement_line_no,
            ledger_entry_id,
            match_key,
            statement_amount_cents,
            ledger_amount_cents,
            dollar_impact_cents,
            memo_amount_cents,
            explanation,
            status,
            detected_at,
        ),
    )


def insert_llm_call(
    conn: sqlite3.Connection,
    *,
    call_id: str,
    doc_id: str,
    list_price_microusd: int,
    run_id: str = "run-contract-0001",
    purpose: str = "extract",
    provider: str = "gemini",
    model: str = "gemini-3.5-flash",
    prompt_tokens: int = 100,
    completion_tokens: int = 10,
    total_tokens: int = 130,
    cached: bool = False,
    latency_ms: int = 412,
    http_status: int = 200,
    attempt: int = 1,
    created_at: str = "2026-08-01T12:00:00Z",
    actual_cost_microusd: int = 0,
) -> None:
    conn.execute(
        """
        INSERT INTO llm_calls (
            call_id, run_id, doc_id, purpose, provider, model,
            prompt_tokens, completion_tokens, total_tokens, cached,
            latency_ms, http_status, attempt, created_at,
            actual_cost_microusd, list_price_microusd
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            call_id,
            run_id,
            doc_id,
            purpose,
            provider,
            model,
            prompt_tokens,
            completion_tokens,
            total_tokens,
            int(cached),
            latency_ms,
            http_status,
            attempt,
            created_at,
            actual_cost_microusd,
            list_price_microusd,
        ),
    )


def rows(db_path: Path, sql: str, params: tuple[object, ...] = ()) -> list[sqlite3.Row]:
    """Read straight from the database, bypassing the API under test."""
    with connection(db_path) as conn:
        return list(conn.execute(sql, params).fetchall())
