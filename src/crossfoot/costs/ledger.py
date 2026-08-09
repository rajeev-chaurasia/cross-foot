"""SQLite cost ledger: one row per LLM call attempt, failures included.

Usage is stored exactly as the provider reported it. The phase 0 probe saw
Gemini report 8 prompt plus 1 completion but 68 total, because reasoning tokens
are billed without being itemized, so a total recomputed from the parts would
understate cost.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from ulid import ULID

from crossfoot.constants import MODEL_LIST_PRICES_MICROUSD_PER_MTOK, Provider
from crossfoot.db import connect

TOKENS_PER_PRICE_UNIT = 1_000_000
# Free tiers bill nothing; a paid lane fills this in from provider billing.
FREE_TIER_ACTUAL_COST_MICROUSD = 0


class Purpose(StrEnum):
    CLASSIFY = "classify"
    EXTRACT = "extract"
    CONSISTENCY = "consistency"
    REPAIR = "repair"


@dataclass(frozen=True)
class ModelPrice:
    """List price per million tokens for every model whose name carries pattern."""

    pattern: str
    prompt_microusd_per_mtok: int
    completion_microusd_per_mtok: int


DEFAULT_PRICES: tuple[ModelPrice, ...] = tuple(
    ModelPrice(
        pattern=pattern,
        prompt_microusd_per_mtok=prompt,
        completion_microusd_per_mtok=completion,
    )
    for pattern, (prompt, completion) in MODEL_LIST_PRICES_MICROUSD_PER_MTOK.items()
)


@dataclass(frozen=True)
class CallContext:
    """Who a call belongs to, so the client can write its own ledger row."""

    run_id: str
    doc_id: str
    purpose: Purpose
    attempt: int = 1


@dataclass(frozen=True)
class CallRow:
    call_id: str
    run_id: str
    doc_id: str
    purpose: Purpose
    provider: Provider
    model: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cached: bool
    latency_ms: int
    http_status: int
    attempt: int
    created_at: str
    actual_cost_microusd: int
    list_price_microusd: int


@dataclass(frozen=True)
class CostTotals:
    calls: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    actual_cost_microusd: int
    list_price_microusd: int


def list_price_microusd(
    model: str,
    *,
    prompt_tokens: int,
    completion_tokens: int,
    prices: Sequence[ModelPrice] = DEFAULT_PRICES,
) -> int:
    """List-price equivalent in microusd; zero when no pattern matches the model."""
    price = _price_for(model, prices)
    if price is None:
        return 0
    microusd = (
        prompt_tokens * price.prompt_microusd_per_mtok
        + completion_tokens * price.completion_microusd_per_mtok
    )
    return (microusd + TOKENS_PER_PRICE_UNIT // 2) // TOKENS_PER_PRICE_UNIT


def effective_list_price_microusd(
    model: str,
    *,
    prompt_tokens: int,
    completion_tokens: int,
    stored_microusd: int,
    prices: Sequence[ModelPrice] = DEFAULT_PRICES,
) -> int:
    """What a stored row is worth once the price table has moved on.

    The stored column is the record of what was believed when the call was made,
    and it stands whenever it says anything at all. A stored zero says only that
    no pattern matched the model back then, which is a gap in the table rather
    than a free call, so it is repriced from the table as it reads now. That is
    what stops a model priced after its run from publishing zero forever.
    """
    if stored_microusd:
        return stored_microusd
    return list_price_microusd(
        model, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens, prices=prices
    )


def _price_for(model: str, prices: Sequence[ModelPrice]) -> ModelPrice | None:
    lowered = model.casefold()
    matches = [price for price in prices if price.pattern.casefold() in lowered]
    return max(matches, key=lambda price: len(price.pattern), default=None)


_SCHEMA = """
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

_RUN_INDEX = "CREATE INDEX IF NOT EXISTS llm_calls_run_id ON llm_calls (run_id)"

_INSERT = """
INSERT INTO llm_calls (
    call_id, run_id, doc_id, purpose, provider, model,
    prompt_tokens, completion_tokens, total_tokens, cached,
    latency_ms, http_status, attempt, created_at,
    actual_cost_microusd, list_price_microusd
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

# Insertion order is the attempt order the ledger reports, so rows are read back
# by rowid rather than by a timestamp two calls can share.
_SELECT_RUN = "SELECT * FROM llm_calls WHERE run_id = ? ORDER BY rowid"

_TOTALS = """
SELECT
    {column} AS bucket,
    COUNT(*) AS calls,
    SUM(prompt_tokens) AS prompt_tokens,
    SUM(completion_tokens) AS completion_tokens,
    SUM(total_tokens) AS total_tokens,
    SUM(actual_cost_microusd) AS actual_cost_microusd,
    SUM(list_price_microusd) AS list_price_microusd
FROM llm_calls
WHERE run_id = ?
GROUP BY {column}
"""

_DOC_COLUMN = "doc_id"
_PROVIDER_COLUMN = "provider"


class CostLedger:
    """Append-only record of every call attempt, priced as it is written."""

    def __init__(self, db_path: Path, *, prices: Sequence[ModelPrice] = DEFAULT_PRICES) -> None:
        self._prices = tuple(prices)
        self._connection = connect(db_path)
        with self._connection:
            self._connection.execute(_SCHEMA)
            self._connection.execute(_RUN_INDEX)

    def record(
        self,
        *,
        run_id: str,
        doc_id: str,
        purpose: Purpose,
        provider: Provider,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: int,
        cached: bool,
        latency_ms: int,
        http_status: int,
        attempt: int,
        actual_cost_microusd: int = FREE_TIER_ACTUAL_COST_MICROUSD,
    ) -> CallRow:
        row = CallRow(
            call_id=str(ULID()),
            run_id=run_id,
            doc_id=doc_id,
            purpose=purpose,
            provider=provider,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            cached=cached,
            latency_ms=latency_ms,
            http_status=http_status,
            attempt=attempt,
            created_at=datetime.now(UTC).isoformat(),
            actual_cost_microusd=actual_cost_microusd,
            list_price_microusd=list_price_microusd(
                model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                prices=self._prices,
            ),
        )
        with self._connection:
            self._connection.execute(
                _INSERT,
                (
                    row.call_id,
                    row.run_id,
                    row.doc_id,
                    row.purpose.value,
                    row.provider.value,
                    row.model,
                    row.prompt_tokens,
                    row.completion_tokens,
                    row.total_tokens,
                    int(row.cached),
                    row.latency_ms,
                    row.http_status,
                    row.attempt,
                    row.created_at,
                    row.actual_cost_microusd,
                    row.list_price_microusd,
                ),
            )
        return row

    def rows(self, run_id: str) -> tuple[CallRow, ...]:
        cursor = self._connection.execute(_SELECT_RUN, (run_id,))
        return tuple(_row_from(record) for record in cursor.fetchall())

    def totals_by_document(self, run_id: str) -> dict[str, CostTotals]:
        return {
            str(record["bucket"]): _totals_from(record)
            for record in self._grouped(run_id, _DOC_COLUMN)
        }

    def totals_by_provider(self, run_id: str) -> dict[Provider, CostTotals]:
        return {
            Provider(record["bucket"]): _totals_from(record)
            for record in self._grouped(run_id, _PROVIDER_COLUMN)
        }

    def close(self) -> None:
        self._connection.close()

    def _grouped(self, run_id: str, column: str) -> list[sqlite3.Row]:
        # column is one of the module constants above, never caller input.
        return self._connection.execute(_TOTALS.format(column=column), (run_id,)).fetchall()


def _row_from(record: sqlite3.Row) -> CallRow:
    return CallRow(
        call_id=str(record["call_id"]),
        run_id=str(record["run_id"]),
        doc_id=str(record["doc_id"]),
        purpose=Purpose(record["purpose"]),
        provider=Provider(record["provider"]),
        model=str(record["model"]),
        prompt_tokens=int(record["prompt_tokens"]),
        completion_tokens=int(record["completion_tokens"]),
        total_tokens=int(record["total_tokens"]),
        cached=bool(record["cached"]),
        latency_ms=int(record["latency_ms"]),
        http_status=int(record["http_status"]),
        attempt=int(record["attempt"]),
        created_at=str(record["created_at"]),
        actual_cost_microusd=int(record["actual_cost_microusd"]),
        list_price_microusd=int(record["list_price_microusd"]),
    )


def _totals_from(record: sqlite3.Row) -> CostTotals:
    return CostTotals(
        calls=int(record["calls"]),
        prompt_tokens=int(record["prompt_tokens"]),
        completion_tokens=int(record["completion_tokens"]),
        total_tokens=int(record["total_tokens"]),
        actual_cost_microusd=int(record["actual_cost_microusd"]),
        list_price_microusd=int(record["list_price_microusd"]),
    )
