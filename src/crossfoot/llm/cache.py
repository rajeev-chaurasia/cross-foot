"""SQLite response cache, keyed exactly like cassettes.

A hit costs no tokens. The client still writes a ledger row for it, flagged
cached with zero usage, so a cheap run reads as cheap rather than as a run that
never happened.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from crossfoot.db import connect
from crossfoot.llm.results import ChatResult, ChatUsage

CACHE_LATENCY_MS = 0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS llm_cache (
    request_key TEXT PRIMARY KEY,
    model TEXT NOT NULL,
    content TEXT NOT NULL,
    prompt_tokens INTEGER NOT NULL,
    completion_tokens INTEGER NOT NULL,
    total_tokens INTEGER NOT NULL,
    created_at TEXT NOT NULL
)
"""

_SELECT = "SELECT * FROM llm_cache WHERE request_key = ?"

_UPSERT = """
INSERT INTO llm_cache (
    request_key, model, content, prompt_tokens, completion_tokens, total_tokens, created_at
) VALUES (?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (request_key) DO UPDATE SET
    model = excluded.model,
    content = excluded.content,
    prompt_tokens = excluded.prompt_tokens,
    completion_tokens = excluded.completion_tokens,
    total_tokens = excluded.total_tokens,
    created_at = excluded.created_at
"""


class ResponseCache:
    def __init__(self, db_path: Path) -> None:
        self._connection = connect(db_path)
        with self._connection:
            self._connection.execute(_SCHEMA)

    def get(self, request_key: str) -> ChatResult | None:
        record = self._connection.execute(_SELECT, (request_key,)).fetchone()
        return None if record is None else _result_from(record)

    def put(self, request_key: str, result: ChatResult) -> None:
        with self._connection:
            self._connection.execute(
                _UPSERT,
                (
                    request_key,
                    result.model,
                    result.content,
                    result.usage.prompt_tokens,
                    result.usage.completion_tokens,
                    result.usage.total_tokens,
                    datetime.now(UTC).isoformat(),
                ),
            )

    def close(self) -> None:
        self._connection.close()


def _result_from(record: sqlite3.Row) -> ChatResult:
    return ChatResult(
        content=str(record["content"]),
        model=str(record["model"]),
        usage=ChatUsage(
            prompt_tokens=int(record["prompt_tokens"]),
            completion_tokens=int(record["completion_tokens"]),
            total_tokens=int(record["total_tokens"]),
        ),
        latency_ms=CACHE_LATENCY_MS,
        rate_limit_headers={},
    )
