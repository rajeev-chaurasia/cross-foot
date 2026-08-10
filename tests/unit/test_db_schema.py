"""Opening a review database is idempotent, and an older one is brought forward."""

import sqlite3
from pathlib import Path

from crossfoot.db import connect
from crossfoot.db.schema import ADDED_COLUMNS, ensure_schema
from crossfoot.db.stats import summary

DB_NAME = "crossfoot.db"

# The exceptions table as it shipped before resolving one was recorded.
OLDER_EXCEPTIONS_DDL = """
CREATE TABLE exceptions (
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


def columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row["name"]) for row in connection.execute(f"PRAGMA table_info({table})")}


def test_an_older_database_gains_the_columns_resolving_needs(tmp_path: Path) -> None:
    connection = connect(tmp_path / DB_NAME)
    try:
        connection.execute(OLDER_EXCEPTIONS_DDL)
        assert "resolution" not in columns(connection, "exceptions")
        ensure_schema(connection)
        # Each entry names its own table: an entry added for another table has to
        # be checked against that table, not against exceptions.
        assert ADDED_COLUMNS
        for table, column, _declaration in ADDED_COLUMNS:
            assert column in columns(connection, table), f"{table}.{column}"
    finally:
        connection.close()


def test_ensuring_the_schema_twice_changes_nothing(tmp_path: Path) -> None:
    connection = connect(tmp_path / DB_NAME)
    try:
        ensure_schema(connection)
        before = columns(connection, "exceptions")
        ensure_schema(connection)
        assert columns(connection, "exceptions") == before
    finally:
        connection.close()


def test_the_summary_of_an_empty_database_is_zeros_rather_than_a_division_by_zero(
    tmp_path: Path,
) -> None:
    connection = connect(tmp_path / DB_NAME)
    try:
        ensure_schema(connection)
        row = summary(connection)
    finally:
        connection.close()
    assert dict(row) == {
        "documents_processed": 0,
        "fields_extracted": 0,
        "auto_accepted": 0,
        "review_queue_depth": 0,
        "open_exception_count": 0,
        "gross_dollars_at_risk_cents": 0,
        "list_price_microusd": 0,
    }
