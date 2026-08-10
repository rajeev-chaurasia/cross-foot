"""The review database schema, applied idempotently.

Every statement in schema.sql is IF NOT EXISTS, so opening an existing database
is a no-op. `ADDED_COLUMNS` covers the one case that is not: a database
materialized before a column existed still has to serve, so the column is added
rather than the file rebuilt.

A column that goes away needs nothing here. Every write names its columns, so an
older file keeps the dead one and no statement mentions it again.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

# (table, column, declaration) for columns added after the first materialized
# database shipped. Constants, never caller input: they are interpolated into
# DDL, which takes no bind parameters.
ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("exceptions", "resolution", "TEXT"),
    ("exceptions", "resolved_at", "TEXT"),
    ("documents", "dealer_id", "TEXT"),
    ("documents", "oem", "TEXT"),
    ("documents", "period_start", "TEXT"),
    ("documents", "period_end", "TEXT"),
)


def ensure_schema(connection: sqlite3.Connection) -> None:
    """Create every table and index the API reads, and add any missing column."""
    connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    for table, column, declaration in ADDED_COLUMNS:
        if column not in _columns(connection, table):
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")


def _columns(connection: sqlite3.Connection, table: str) -> frozenset[str]:
    rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    return frozenset(str(row["name"]) for row in rows)
