"""Contract tests for the exceptions dashboard routes in crossfoot.api.

Written against docs/contracts-phase3.md before the implementation exists, so
the module-level importorskip keeps collection clean today. Rows are seeded with
sqlite3 directly; see api_seed for the schema.

Pinned surface, binding in the phase 2 sense:

    GET /api/exceptions -> {"items": [exception, ...], "total": int}
        query: type, status, min_impact_cents, limit, offset
        default rank: absolute dollar impact descending, so a large negative
        impact outranks a small positive one
        min_impact_cents compares the same absolute impact the ranking uses
        total is the count the filter matched, never the size of the page
    POST /api/exceptions/{exception_id}/resolve {"resolution": str}

The seeded impacts are deliberately distinct in absolute value, so the ranking
is total without the contract having to name a tie break.
"""

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from api_seed import DB_NAME, connection, create_schema, insert_document, insert_exception
from fastapi.testclient import TestClient

from crossfoot.constants import (
    DocType,
    ExceptionStatus,
    ExceptionType,
    ExtractionRoute,
    QualityTier,
    SplitName,
)

api = pytest.importorskip("crossfoot.api")

DOC = "doc-a"
MISSING_EXCEPTION = "exc-nope-9999"

# Hand ranked by absolute dollar impact, descending:
#   exc-3 short_pay          +250_000 -> 250_000
#   exc-5 duplicate           -60_000 ->  60_000  (resolved)
#   exc-1 amount_mismatch     -45_000 ->  45_000
#   exc-2 missing_from_ledger +12_000 ->  12_000
#   exc-4 timing_difference         0 ->       0  (memo 88_000)
# exc-1 outranking exc-2 is the point: signed ranking would invert them.
IMPACT_ORDER = ("exc-3", "exc-5", "exc-1", "exc-2", "exc-4")


def seed(conn: sqlite3.Connection) -> None:
    insert_document(
        conn,
        doc_id=DOC,
        doc_type=DocType.PARTS_STATEMENT,
        quality_tier=QualityTier.CLEAN_DIGITAL,
        route=ExtractionRoute.DIGITAL_PDF,
        split=SplitName.TEST,
    )
    insert_exception(
        conn,
        exception_id="exc-1",
        exception_type=ExceptionType.AMOUNT_MISMATCH,
        doc_id=DOC,
        statement_line_no=1,
        statement_amount_cents=105_000,
        ledger_amount_cents=150_000,
        dollar_impact_cents=-45_000,  # statement minus ledger
        status=ExceptionStatus.OPEN,
        explanation="statement is 450.00 under the ledger",
    )
    insert_exception(
        conn,
        exception_id="exc-2",
        exception_type=ExceptionType.MISSING_FROM_LEDGER,
        doc_id=DOC,
        statement_line_no=2,
        statement_amount_cents=12_000,
        dollar_impact_cents=12_000,
        status=ExceptionStatus.OPEN,
        explanation="statement line never reached the ledger",
    )
    insert_exception(
        conn,
        exception_id="exc-3",
        exception_type=ExceptionType.SHORT_PAY,
        doc_id=DOC,
        statement_line_no=3,
        statement_amount_cents=50_000,
        ledger_amount_cents=300_000,
        dollar_impact_cents=250_000,  # short pay impact is the withheld amount
        status=ExceptionStatus.OPEN,
        explanation="factory withheld 2500.00",
    )
    insert_exception(
        conn,
        exception_id="exc-4",
        exception_type=ExceptionType.TIMING_DIFFERENCE,
        doc_id=DOC,
        statement_line_no=4,
        statement_amount_cents=88_000,
        ledger_amount_cents=88_000,
        dollar_impact_cents=0,
        memo_amount_cents=88_000,
        status=ExceptionStatus.OPEN,
        explanation="posted outside the statement period",
    )
    insert_exception(
        conn,
        exception_id="exc-5",
        exception_type=ExceptionType.DUPLICATE,
        doc_id=DOC,
        statement_line_no=5,
        statement_amount_cents=60_000,
        dollar_impact_cents=-60_000,
        status=ExceptionStatus.RESOLVED,
        explanation="line 5 repeats line 4",
    )


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / DB_NAME
    with connection(path) as conn:
        create_schema(conn)
        seed(conn)
    return path


@pytest.fixture
def client(tmp_path: Path, db_path: Path) -> Iterator[TestClient]:
    crops_root = tmp_path / "crops"
    crops_root.mkdir()
    scorecards_dir = tmp_path / "scorecards"
    scorecards_dir.mkdir()
    app = api.create_app(db_path=db_path, crops_root=crops_root, scorecards_dir=scorecards_dir)
    with TestClient(app) as test_client:
        yield test_client


def listing(client: TestClient, **params: str | int) -> dict[str, Any]:
    response = client.get("/api/exceptions", params=params)
    assert response.status_code == 200
    payload: dict[str, Any] = response.json()
    return payload


def listing_ids(client: TestClient, **params: str | int) -> list[str]:
    return [str(item["exception_id"]) for item in listing(client, **params)["items"]]


def exception_row(db_path: Path, exception_id: str) -> sqlite3.Row:
    with connection(db_path) as conn:
        row: sqlite3.Row | None = conn.execute(
            "SELECT * FROM exceptions WHERE exception_id = ?", (exception_id,)
        ).fetchone()
    assert row is not None
    return row


# Ranking.


def test_default_rank_is_absolute_dollar_impact_descending(client: TestClient) -> None:
    assert listing_ids(client) == list(IMPACT_ORDER)


def test_a_large_negative_impact_outranks_a_small_positive_one(client: TestClient) -> None:
    ids = listing_ids(client)
    # 45_000 against the ledger beats 12_000 in its favour.
    assert ids.index("exc-1") < ids.index("exc-2")


def test_listing_reports_the_full_count(client: TestClient) -> None:
    assert listing(client)["total"] == 5


def test_listing_items_carry_the_frozen_exception_record_fields(client: TestClient) -> None:
    (first, *_) = listing(client)["items"]
    assert first["exception_id"] == "exc-3"
    assert first["exception_type"] == ExceptionType.SHORT_PAY.value
    assert first["doc_id"] == DOC
    assert first["statement_line_no"] == 3
    assert first["dollar_impact_cents"] == 250_000
    assert first["memo_amount_cents"] == 0
    assert first["status"] == ExceptionStatus.OPEN.value
    assert first["explanation"] == "factory withheld 2500.00"


# Paging. The dashboard cannot render 751 rows at once, so the listing pages the
# same way the review queue does.


def test_limit_and_offset_cut_the_ranked_listing_into_pages(client: TestClient) -> None:
    assert listing_ids(client, limit=2, offset=0) == ["exc-3", "exc-5"]
    assert listing_ids(client, limit=2, offset=2) == ["exc-1", "exc-2"]
    assert listing_ids(client, limit=2, offset=4) == ["exc-4"]


def test_walking_the_pages_visits_every_exception_exactly_once(client: TestClient) -> None:
    walked: list[str] = []
    offset = 0
    while True:
        page = listing_ids(client, limit=2, offset=offset)
        if not page:
            break
        walked.extend(page)
        offset += 2
    assert walked == list(IMPACT_ORDER)


def test_total_counts_the_filter_not_the_page(client: TestClient) -> None:
    payload = listing(client, limit=1)
    assert len(payload["items"]) == 1
    assert payload["total"] == 5


def test_an_offset_past_the_end_is_an_empty_page_and_not_an_error(client: TestClient) -> None:
    payload = listing(client, limit=2, offset=99)
    assert payload["items"] == []
    assert payload["total"] == 5


def test_a_limit_below_one_is_rejected(client: TestClient) -> None:
    assert client.get("/api/exceptions", params={"limit": 0}).status_code == 422


def test_a_negative_offset_is_rejected(client: TestClient) -> None:
    assert client.get("/api/exceptions", params={"offset": -1}).status_code == 422


# Filters.


def test_type_filter_narrows_the_listing(client: TestClient) -> None:
    payload = listing(client, type=ExceptionType.AMOUNT_MISMATCH)
    assert [str(item["exception_id"]) for item in payload["items"]] == ["exc-1"]
    assert payload["total"] == 1


def test_status_filter_narrows_the_listing(client: TestClient) -> None:
    # Four of the five are open; exc-5 is seeded resolved.
    payload = listing(client, status=ExceptionStatus.OPEN)
    assert [str(item["exception_id"]) for item in payload["items"]] == [
        "exc-3",
        "exc-1",
        "exc-2",
        "exc-4",
    ]
    assert payload["total"] == 4
    assert listing_ids(client, status=ExceptionStatus.RESOLVED) == ["exc-5"]


def test_min_impact_cents_filter_compares_absolute_impact(client: TestClient) -> None:
    # 250_000 and 60_000 clear 50_000; 45_000, 12_000 and 0 do not.
    assert listing_ids(client, min_impact_cents=50_000) == ["exc-3", "exc-5"]


def test_min_impact_cents_keeps_every_exception_at_zero(client: TestClient) -> None:
    assert listing_ids(client, min_impact_cents=0) == list(IMPACT_ORDER)


# Timing differences carry no dollars and still belong on the dashboard.


def test_a_timing_difference_carries_zero_impact_and_a_memo_amount(client: TestClient) -> None:
    (timing,) = [item for item in listing(client)["items"] if item["exception_id"] == "exc-4"]
    assert timing["exception_type"] == ExceptionType.TIMING_DIFFERENCE.value
    assert timing["dollar_impact_cents"] == 0
    assert timing["memo_amount_cents"] == 88_000


def test_a_timing_difference_survives_a_min_impact_filter_of_zero(client: TestClient) -> None:
    assert "exc-4" in listing_ids(client, min_impact_cents=0)
    assert "exc-4" in listing_ids(client, status=ExceptionStatus.OPEN, min_impact_cents=0)


# Resolve.


def test_resolve_sets_the_status_to_resolved(client: TestClient, db_path: Path) -> None:
    response = client.post(
        "/api/exceptions/exc-1/resolve", json={"resolution": "credited on the next statement"}
    )
    assert response.status_code == 200
    assert response.json()["status"] == ExceptionStatus.RESOLVED.value
    assert exception_row(db_path, "exc-1")["status"] == ExceptionStatus.RESOLVED.value


def test_resolve_is_idempotent(client: TestClient, db_path: Path) -> None:
    first = client.post("/api/exceptions/exc-1/resolve", json={"resolution": "credited"})
    second = client.post("/api/exceptions/exc-1/resolve", json={"resolution": "credited again"})
    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["status"] == ExceptionStatus.RESOLVED.value
    with connection(db_path) as conn:
        (total,) = conn.execute("SELECT COUNT(*) FROM exceptions").fetchone()
        states = conn.execute(
            "SELECT status FROM exceptions WHERE exception_id = ?", ("exc-1",)
        ).fetchall()
    assert total == 5  # resolving appends nothing
    assert [row["status"] for row in states] == [ExceptionStatus.RESOLVED.value]


def test_resolve_leaves_the_dollar_impact_alone(client: TestClient, db_path: Path) -> None:
    client.post("/api/exceptions/exc-1/resolve", json={"resolution": "credited"})
    assert exception_row(db_path, "exc-1")["dollar_impact_cents"] == -45_000


def test_a_resolved_exception_leaves_the_open_listing(client: TestClient) -> None:
    client.post("/api/exceptions/exc-1/resolve", json={"resolution": "credited"})
    assert listing_ids(client, status=ExceptionStatus.OPEN) == ["exc-3", "exc-2", "exc-4"]


def test_unknown_exception_id_is_404_on_resolve(client: TestClient) -> None:
    response = client.post(
        f"/api/exceptions/{MISSING_EXCEPTION}/resolve", json={"resolution": "credited"}
    )
    assert response.status_code == 404
