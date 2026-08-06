"""Shared sqlite3 helpers for the local stores.

Stdlib sqlite3 with explicit SQL and no ORM. WAL is on so a reader (the
scorecard, a second process, a test opening its own connection) never blocks
the writer.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

WAL_PRAGMA = "PRAGMA journal_mode=WAL"


def connect(db_path: Path) -> sqlite3.Connection:
    """Open db_path in WAL mode with rows addressable by column name."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    connection.execute(WAL_PRAGMA)
    return connection
