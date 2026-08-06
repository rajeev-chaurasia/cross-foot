"""Contract tests for the SQLite cost ledger.

Written against docs/contracts-phase2.md before the implementation exists.
Every expected number is hand computed. The aggregation tests run against an
injected price table so the arithmetic is checkable on paper; the free tier
test runs against the real table in constants so a published cost per document
can never come out as a smug zero.
"""

from __future__ import annotations

import sqlite3
from enum import StrEnum
from pathlib import Path
from typing import Any

import pytest

from crossfoot.constants import PROVIDER_DEFAULT_MODELS, Provider

costs = pytest.importorskip("crossfoot.costs")

RUN_ID = "run-0001"
TEST_MODEL = "test-model"

# The columns the contract freezes, plus the two cost columns the same
# document requires the ledger to store alongside them.
FROZEN_COLUMNS = frozenset(
    {
        "call_id",
        "run_id",
        "doc_id",
        "purpose",
        "provider",
        "model",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "cached",
        "latency_ms",
        "http_status",
        "attempt",
        "created_at",
        "actual_cost_microusd",
        "list_price_microusd",
    }
)

# 1_000_000 microusd per million tokens is 1 microusd per prompt token and
# 2 microusd per completion token, which keeps every total hand checkable.
TEST_PRICES = (
    costs.ModelPrice(
        pattern=TEST_MODEL,
        prompt_microusd_per_mtok=1_000_000,
        completion_microusd_per_mtok=2_000_000,
    ),
)


def ledger(tmp_path: Path, *, real_prices: bool = False) -> Any:
    db_path = tmp_path / "costs.db"
    if real_prices:
        return costs.CostLedger(db_path)
    return costs.CostLedger(db_path, prices=TEST_PRICES)


def add(
    book: Any,
    *,
    doc_id: str = "doc-01",
    provider: Provider = Provider.GEMINI,
    model: str = TEST_MODEL,
    prompt_tokens: int = 100,
    completion_tokens: int = 10,
    total_tokens: int = 130,
    cached: bool = False,
    actual_cost_microusd: int = 0,
    attempt: int = 1,
    http_status: int = 200,
    latency_ms: int = 412,
    run_id: str = RUN_ID,
    purpose: Any = costs.Purpose.EXTRACT,
) -> Any:
    return book.record(
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
        actual_cost_microusd=actual_cost_microusd,
    )


def table_columns(db_path: Path, table: str) -> set[str]:
    connection = sqlite3.connect(db_path)
    try:
        rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    finally:
        connection.close()
    return {str(row[1]) for row in rows}


def hand_built(tmp_path: Path) -> Any:
    """Five rows across two documents and three providers."""
    book = ledger(tmp_path)
    add(
        book,
        doc_id="doc-A",
        provider=Provider.GEMINI,
        prompt_tokens=100,
        completion_tokens=10,
        total_tokens=130,
    )
    add(
        book,
        doc_id="doc-A",
        provider=Provider.GEMINI,
        prompt_tokens=200,
        completion_tokens=20,
        total_tokens=260,
    )
    add(
        book,
        doc_id="doc-A",
        provider=Provider.GROQ,
        prompt_tokens=0,
        completion_tokens=0,
        total_tokens=0,
        cached=True,
    )
    add(
        book,
        doc_id="doc-B",
        provider=Provider.GROQ,
        prompt_tokens=50,
        completion_tokens=5,
        total_tokens=55,
    )
    add(
        book,
        doc_id="doc-B",
        provider=Provider.OPENROUTER,
        prompt_tokens=7,
        completion_tokens=3,
        total_tokens=10,
        actual_cost_microusd=1234,
    )
    return book


# Schema and vocabulary.


def test_llm_calls_table_has_exactly_the_frozen_columns(tmp_path: Path) -> None:
    book = ledger(tmp_path)
    add(book)
    assert table_columns(tmp_path / "costs.db", "llm_calls") == FROZEN_COLUMNS


def test_purpose_is_a_str_enum_with_the_four_frozen_members() -> None:
    assert issubclass(costs.Purpose, StrEnum)
    assert {member.name for member in costs.Purpose} == {
        "CLASSIFY",
        "EXTRACT",
        "CONSISTENCY",
        "REPAIR",
    }


# One row per attempt.


def test_every_attempt_gets_its_own_row(tmp_path: Path) -> None:
    book = ledger(tmp_path)
    for attempt in (1, 2, 3):
        add(book, attempt=attempt, http_status=429 if attempt < 3 else 200)
    rows = book.rows(RUN_ID)
    assert len(rows) == 3
    assert [row.attempt for row in rows] == [1, 2, 3]
    assert [row.http_status for row in rows] == [429, 429, 200]
    assert len({row.call_id for row in rows}) == 3


def test_a_row_round_trips_its_frozen_fields(tmp_path: Path) -> None:
    book = ledger(tmp_path)
    add(
        book,
        doc_id="doc-77",
        provider=Provider.GROQ,
        purpose=costs.Purpose.CLASSIFY,
        latency_ms=931,
        attempt=2,
        http_status=200,
    )
    (row,) = book.rows(RUN_ID)
    assert row.run_id == RUN_ID
    assert row.doc_id == "doc-77"
    assert row.purpose == costs.Purpose.CLASSIFY
    assert row.provider == Provider.GROQ
    assert row.model == TEST_MODEL
    assert row.latency_ms == 931
    assert row.attempt == 2
    assert row.cached is False


def test_rows_survive_reopening_the_database(tmp_path: Path) -> None:
    add(ledger(tmp_path), doc_id="doc-09")
    reopened = ledger(tmp_path)
    assert [row.doc_id for row in reopened.rows(RUN_ID)] == ["doc-09"]


# Usage is recorded, never recomputed.


def test_total_tokens_is_stored_as_reported(tmp_path: Path) -> None:
    # 8 prompt plus 1 completion but 68 total: Gemini bills hidden reasoning
    # tokens, so recomputing the total from the parts understates the cost.
    book = ledger(tmp_path)
    add(book, prompt_tokens=8, completion_tokens=1, total_tokens=68)
    (row,) = book.rows(RUN_ID)
    assert row.prompt_tokens == 8
    assert row.completion_tokens == 1
    assert row.total_tokens == 68


def test_a_cache_hit_records_zero_marginal_tokens(tmp_path: Path) -> None:
    book = ledger(tmp_path)
    add(book, cached=True, prompt_tokens=0, completion_tokens=0, total_tokens=0)
    (row,) = book.rows(RUN_ID)
    assert row.cached is True
    assert row.prompt_tokens == 0
    assert row.completion_tokens == 0
    assert row.total_tokens == 0
    assert row.list_price_microusd == 0
    assert row.actual_cost_microusd == 0


# Pricing.


def test_list_price_is_per_million_tokens() -> None:
    # 1_000_000 prompt tokens at 1_000_000 microusd per million is 1_000_000
    # microusd; 500_000 completion tokens at 2_000_000 per million is another
    # 1_000_000. Total 2_000_000 microusd.
    price = costs.list_price_microusd(
        TEST_MODEL, prompt_tokens=1_000_000, completion_tokens=500_000, prices=TEST_PRICES
    )
    assert price == 2_000_000


def test_the_ledger_stores_list_price_beside_actual_cost(tmp_path: Path) -> None:
    book = ledger(tmp_path)
    add(book, prompt_tokens=100, completion_tokens=10, total_tokens=130, actual_cost_microusd=0)
    (row,) = book.rows(RUN_ID)
    # 100 * 1 + 10 * 2
    assert row.list_price_microusd == 120
    assert row.actual_cost_microusd == 0


def test_a_free_tier_model_still_gets_a_nonzero_list_price(tmp_path: Path) -> None:
    book = ledger(tmp_path, real_prices=True)
    add(
        book,
        model=PROVIDER_DEFAULT_MODELS[Provider.GEMINI],
        prompt_tokens=1_000_000,
        completion_tokens=1_000_000,
        total_tokens=2_000_000,
        actual_cost_microusd=0,
    )
    (row,) = book.rows(RUN_ID)
    assert row.actual_cost_microusd == 0
    assert row.list_price_microusd > 0


# Aggregation.


def test_totals_per_document(tmp_path: Path) -> None:
    totals = hand_built(tmp_path).totals_by_document(RUN_ID)
    assert set(totals) == {"doc-A", "doc-B"}

    doc_a = totals["doc-A"]
    assert doc_a.calls == 3
    assert doc_a.prompt_tokens == 300  # 100 + 200 + 0
    assert doc_a.completion_tokens == 30  # 10 + 20 + 0
    assert doc_a.total_tokens == 390  # 130 + 260 + 0
    assert doc_a.actual_cost_microusd == 0
    assert doc_a.list_price_microusd == 360  # 120 + 240 + 0

    doc_b = totals["doc-B"]
    assert doc_b.calls == 2
    assert doc_b.prompt_tokens == 57  # 50 + 7
    assert doc_b.completion_tokens == 8  # 5 + 3
    assert doc_b.total_tokens == 65  # 55 + 10
    assert doc_b.actual_cost_microusd == 1234
    assert doc_b.list_price_microusd == 73  # 60 + 13


def test_totals_per_provider(tmp_path: Path) -> None:
    totals = hand_built(tmp_path).totals_by_provider(RUN_ID)
    assert set(totals) == {Provider.GEMINI, Provider.GROQ, Provider.OPENROUTER}

    gemini = totals[Provider.GEMINI]
    assert gemini.calls == 2
    assert gemini.prompt_tokens == 300
    assert gemini.completion_tokens == 30
    assert gemini.total_tokens == 390
    assert gemini.list_price_microusd == 360

    groq = totals[Provider.GROQ]
    assert groq.calls == 2
    assert groq.prompt_tokens == 50
    assert groq.completion_tokens == 5
    assert groq.total_tokens == 55
    assert groq.list_price_microusd == 60  # the cached row contributes nothing

    openrouter = totals[Provider.OPENROUTER]
    assert openrouter.calls == 1
    assert openrouter.total_tokens == 10
    assert openrouter.actual_cost_microusd == 1234
    assert openrouter.list_price_microusd == 13


def test_totals_are_scoped_to_one_run(tmp_path: Path) -> None:
    book = hand_built(tmp_path)
    add(
        book,
        run_id="run-0002",
        doc_id="doc-Z",
        prompt_tokens=999,
        completion_tokens=99,
        total_tokens=1098,
    )
    assert set(book.totals_by_document(RUN_ID)) == {"doc-A", "doc-B"}
    assert set(book.totals_by_document("run-0002")) == {"doc-Z"}
