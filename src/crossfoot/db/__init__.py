"""Shared sqlite3 helpers for the local stores.

Stdlib sqlite3 with explicit SQL and no ORM. WAL is on so a reader (the
scorecard, a second process, a test opening its own connection) never blocks
the writer.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

WAL_PRAGMA = "PRAGMA journal_mode=WAL"


def connect(db_path: Path, *, check_same_thread: bool = True) -> sqlite3.Connection:
    """Open db_path in WAL mode with rows addressable by column name.

    `check_same_thread=False` is for a connection one request owns end to end:
    the API's sync handlers run in a threadpool and the dependency that opens the
    connection may land on a different worker than the handler that uses it.
    Nothing shares the connection, so no serialization is lost.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path, check_same_thread=check_same_thread)
    connection.row_factory = sqlite3.Row
    connection.execute(WAL_PRAGMA)
    return connection
