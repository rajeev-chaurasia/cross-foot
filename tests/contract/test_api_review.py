"""Contract tests for the review queue routes in crossfoot.api.

Written against docs/contracts-phase3.md before the implementation exists, so
the module-level importorskip keeps collection clean today. The database is
seeded with sqlite3 directly because `crossfoot serve` does not exist yet
either; see api_seed for the schema and the two additions it has to make.

Phase 3 names routes and behaviour but no DTO field names, so these tests pin
the smallest surface that expresses the frozen behaviour, and that pin is
binding in the phase 2 sense:

    GET /api/review/queue -> {"items": [item, ...], "total": int}
        item: field_id, doc_id, line_no, name, family, raw_text, value,
              confidence, status, crop_url
        query: status, family, tier, limit, offset
        unset filters mean no filter, so the unfiltered queue is every field
        ordered least trusted first, and `status=needs_review` narrows it
    GET /api/review/items/{field_id} -> item plus signals, document, neighbors
    POST /api/review/items/{field_id}/accept -> the updated item
    POST /api/review/items/{field_id}/correct {"value", "reviewer"} -> the item

Ordering is the contract's total order: ascending confidence, then field_id.
Two fields are seeded at confidence 0.20 so the tie break is exercised rather
than assumed. Every expected page and count below is worked out by hand from
the seeded rows listed in `seed`.
"""

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from api_seed import DB_NAME, connection, create_schema, insert_document, insert_field, signals_json
from fastapi.testclient import TestClient

from crossfoot.constants import (
    CropKind,
    DocType,
    ExtractionRoute,
    FieldFamily,
    FieldName,
    FieldSource,
    QualityTier,
    ReviewStatus,
    SplitName,
)

api = pytest.importorskip("crossfoot.api")

DOC_A = "doc-a"
DOC_B = "doc-b"

AMOUNT_FIELD = "fld-a-0002"  # line 1 of doc-a, value 1234.56, confidence 0.20
DATE_FIELD = "fld-a-0003"  # line 1 of doc-a, value 2026-07-15, confidence 0.55
ACCEPT_FIELD = "fld-b-0001"  # line 1 of doc-b, confidence 0.31
MISSING_FIELD = "fld-nope-9999"

REVIEWER = "rc"

AMOUNT_SIGNALS = signals_json(
    QualityTier.CLEAN_DIGITAL,
    self_consistency=0.5,
    validator_pass=1.0,
    crossfoot_ok=0.0,
    char_ambiguity=0.25,
)
PLAIN_SIGNALS = signals_json(QualityTier.CLEAN_DIGITAL, validator_pass=1.0)
SCAN_SIGNALS = signals_json(QualityTier.SCAN_HEAVY, self_consistency=0.0, char_ambiguity=0.4)

# Ascending confidence, then field_id. The two fields at 0.20 are the tie:
# fld-a-0001 sorts before fld-a-0002 on field_id alone.
#   0.20 fld-a-0001, 0.20 fld-a-0002, 0.31 fld-b-0001, 0.44 fld-b-0002,
#   0.55 fld-a-0003, 0.72 fld-a-0004, 0.90 fld-b-0003, 0.99 fld-a-0005
QUEUE_ORDER = (
    "fld-a-0001",
    "fld-a-0002",
    "fld-b-0001",
    "fld-b-0002",
    "fld-a-0003",
    "fld-a-0004",
    "fld-b-0003",
    "fld-a-0005",
)
# The six seeded NEEDS_REVIEW fields, same order minus the two auto accepted.
NEEDS_REVIEW_ORDER = (
    "fld-a-0001",
    "fld-a-0002",
    "fld-b-0001",
    "fld-b-0002",
    "fld-a-0003",
    "fld-a-0004",
)

DOCUMENT_CONTEXT = {
    "doc_id": DOC_A,
    "doc_type": DocType.PARTS_STATEMENT.value,
    "quality_tier": QualityTier.CLEAN_DIGITAL.value,
    "route": ExtractionRoute.DIGITAL_PDF.value,
    "split": SplitName.TEST.value,
}

ITEM_KEYS = frozenset(
    {
        "field_id",
        "doc_id",
        "line_no",
        "name",
        "family",
        "raw_text",
        "value",
        "confidence",
        "status",
        "crop_url",
    }
)


def seed(conn: sqlite3.Connection) -> None:
    """Two documents and eight fields, spanning both tiers and four families."""
    insert_document(
        conn,
        doc_id=DOC_A,
        doc_type=DocType.PARTS_STATEMENT,
        quality_tier=QualityTier.CLEAN_DIGITAL,
        route=ExtractionRoute.DIGITAL_PDF,
        split=SplitName.TEST,
    )
    insert_document(
        conn,
        doc_id=DOC_B,
        doc_type=DocType.WARRANTY_CREDIT_MEMO,
        quality_tier=QualityTier.SCAN_HEAVY,
        route=ExtractionRoute.SCANNED_PDF,
        split=SplitName.TRAIN,
    )
    insert_field(
        conn,
        field_id="fld-a-0001",
        doc_id=DOC_A,
        line_no=1,
        name=FieldName.VIN,
        family=FieldFamily.REFERENCE,
        raw_text="1G1ZT53826F1O9149",
        value="1G1ZT53826F1O9149",
        confidence=0.20,
        status=ReviewStatus.NEEDS_REVIEW,
        signals=PLAIN_SIGNALS,
    )
    insert_field(
        conn,
        field_id=AMOUNT_FIELD,
        doc_id=DOC_A,
        line_no=1,
        name=FieldName.LINE_AMOUNT,
        family=FieldFamily.AMOUNT,
        raw_text="$1,234.56",
        value="1234.56",
        value_cents=123_456,
        confidence=0.20,
        status=ReviewStatus.NEEDS_REVIEW,
        signals=AMOUNT_SIGNALS,
        crop_kind=CropKind.EXACT_BBOX,
        source=FieldSource.DETERMINISTIC,
    )
    insert_field(
        conn,
        field_id=DATE_FIELD,
        doc_id=DOC_A,
        line_no=1,
        name=FieldName.LINE_DATE,
        family=FieldFamily.DATE,
        raw_text="07/15/2026",
        value="2026-07-15",
        value_date="2026-07-15",
        confidence=0.55,
        status=ReviewStatus.NEEDS_REVIEW,
        signals=PLAIN_SIGNALS,
    )
    insert_field(
        conn,
        field_id="fld-a-0004",
        doc_id=DOC_A,
        line_no=1,
        name=FieldName.DESCRIPTION,
        family=FieldFamily.TEXT,
        raw_text="Brake pads, front",
        value="Brake pads, front",
        confidence=0.72,
        status=ReviewStatus.NEEDS_REVIEW,
        signals=PLAIN_SIGNALS,
    )
    insert_field(
        conn,
        field_id="fld-a-0005",
        doc_id=DOC_A,
        name=FieldName.TOTAL,
        family=FieldFamily.AMOUNT,
        raw_text="$9,876.54",
        value="9876.54",
        value_cents=987_654,
        confidence=0.99,
        status=ReviewStatus.AUTO_ACCEPTED,
        signals=PLAIN_SIGNALS,
    )
    insert_field(
        conn,
        field_id=ACCEPT_FIELD,
        doc_id=DOC_B,
        line_no=1,
        name=FieldName.CLAIM_NUMBER,
        family=FieldFamily.REFERENCE,
        raw_text="NS12345678",
        value="NS12345678",
        confidence=0.31,
        status=ReviewStatus.NEEDS_REVIEW,
        signals=SCAN_SIGNALS,
    )
    insert_field(
        conn,
        field_id="fld-b-0002",
        doc_id=DOC_B,
        line_no=2,
        name=FieldName.LINE_AMOUNT,
        family=FieldFamily.AMOUNT,
        raw_text="$44.00",
        value="44.00",
        value_cents=4_400,
        confidence=0.44,
        status=ReviewStatus.NEEDS_REVIEW,
        signals=SCAN_SIGNALS,
    )
    insert_field(
        conn,
        field_id="fld-b-0003",
        doc_id=DOC_B,
        name=FieldName.STATEMENT_DATE,
        family=FieldFamily.DATE,
        raw_text="2026-07-31",
        value="2026-07-31",
        value_date="2026-07-31",
        confidence=0.90,
        status=ReviewStatus.AUTO_ACCEPTED,
        signals=SCAN_SIGNALS,
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


def queue(client: TestClient, **params: str | int) -> dict[str, Any]:
    response = client.get("/api/review/queue", params=params)
    assert response.status_code == 200
    payload: dict[str, Any] = response.json()
    return payload


def queue_ids(client: TestClient, **params: str | int) -> list[str]:
    return [str(item["field_id"]) for item in queue(client, **params)["items"]]


def detail(client: TestClient, field_id: str) -> dict[str, Any]:
    response = client.get(f"/api/review/items/{field_id}")
    assert response.status_code == 200
    payload: dict[str, Any] = response.json()
    return payload


def field_row(db_path: Path, field_id: str) -> sqlite3.Row:
    with connection(db_path) as conn:
        row: sqlite3.Row | None = conn.execute(
            "SELECT * FROM fields WHERE field_id = ?", (field_id,)
        ).fetchone()
    assert row is not None
    return row


def corrections(db_path: Path, field_id: str) -> list[sqlite3.Row]:
    """Correction rows in insertion order, which is the order they were made."""
    with connection(db_path) as conn:
        return list(
            conn.execute(
                "SELECT * FROM corrections WHERE field_id = ? ORDER BY rowid", (field_id,)
            ).fetchall()
        )


# Queue order: the contract's total order, so two runs agree.


def test_queue_is_sorted_by_ascending_confidence_then_field_id(client: TestClient) -> None:
    assert queue_ids(client, limit=50) == list(QUEUE_ORDER)


def test_queue_ties_break_by_field_id(client: TestClient) -> None:
    # Both fields sit at confidence 0.20, so only field_id can order them.
    ids = queue_ids(client, limit=50)
    assert ids[:2] == ["fld-a-0001", "fld-a-0002"]
    items = queue(client, limit=50)["items"]
    assert items[0]["confidence"] == pytest.approx(0.20)
    assert items[1]["confidence"] == pytest.approx(0.20)


def test_queue_puts_the_least_trusted_field_first_by_default(client: TestClient) -> None:
    payload = queue(client)
    assert payload["items"][0]["field_id"] == "fld-a-0001"
    assert payload["total"] == 8


def test_queue_items_carry_the_frozen_keys(client: TestClient) -> None:
    (first, *_) = queue(client, limit=1)["items"]
    assert set(first) >= ITEM_KEYS
    assert first["doc_id"] == DOC_A
    assert first["family"] == FieldFamily.REFERENCE.value
    assert first["status"] == ReviewStatus.NEEDS_REVIEW.value
    assert first["crop_url"] == f"/api/crops/{DOC_A}/fld-a-0001.png"


# Filters and paging.


def test_status_filter_narrows_the_queue(client: TestClient) -> None:
    payload = queue(client, status=ReviewStatus.NEEDS_REVIEW, limit=50)
    assert [str(item["field_id"]) for item in payload["items"]] == list(NEEDS_REVIEW_ORDER)
    assert payload["total"] == 6  # 8 seeded fields minus the 2 auto accepted


def test_family_filter_narrows_the_queue(client: TestClient) -> None:
    # The three AMOUNT fields: 0.20 fld-a-0002, 0.44 fld-b-0002, 0.99 fld-a-0005.
    payload = queue(client, family=FieldFamily.AMOUNT, limit=50)
    assert [str(item["field_id"]) for item in payload["items"]] == [
        "fld-a-0002",
        "fld-b-0002",
        "fld-a-0005",
    ]
    assert payload["total"] == 3


def test_tier_filter_narrows_the_queue(client: TestClient) -> None:
    # Tier belongs to the document, so scan_heavy is exactly doc-b's 3 fields.
    payload = queue(client, tier=QualityTier.SCAN_HEAVY, limit=50)
    assert [str(item["field_id"]) for item in payload["items"]] == [
        "fld-b-0001",
        "fld-b-0002",
        "fld-b-0003",
    ]
    assert payload["total"] == 3


def test_filters_compose(client: TestClient) -> None:
    payload = queue(client, status=ReviewStatus.NEEDS_REVIEW, family=FieldFamily.AMOUNT, limit=50)
    assert [str(item["field_id"]) for item in payload["items"]] == ["fld-a-0002", "fld-b-0002"]
    assert payload["total"] == 2


def test_limit_and_offset_page_through_the_whole_order(client: TestClient) -> None:
    first = queue(client, limit=3, offset=0)
    second = queue(client, limit=3, offset=3)
    third = queue(client, limit=3, offset=6)
    assert [str(item["field_id"]) for item in first["items"]] == list(QUEUE_ORDER[0:3])
    assert [str(item["field_id"]) for item in second["items"]] == list(QUEUE_ORDER[3:6])
    assert [str(item["field_id"]) for item in third["items"]] == list(QUEUE_ORDER[6:8])
    assert len(third["items"]) == 2  # 8 rows, so the last page is short


def test_total_is_the_full_filtered_count_not_the_page_size(client: TestClient) -> None:
    payload = queue(client, limit=3, offset=3)
    assert len(payload["items"]) == 3
    assert payload["total"] == 8


def test_total_follows_the_filter_through_paging(client: TestClient) -> None:
    payload = queue(client, status=ReviewStatus.NEEDS_REVIEW, limit=2, offset=4)
    assert [str(item["field_id"]) for item in payload["items"]] == ["fld-a-0003", "fld-a-0004"]
    assert payload["total"] == 6


# Item detail.


def test_item_detail_returns_the_signal_breakdown(client: TestClient) -> None:
    payload = detail(client, AMOUNT_FIELD)
    assert payload["signals"] == json.loads(AMOUNT_SIGNALS)


def test_item_detail_returns_the_crop_url(client: TestClient) -> None:
    payload = detail(client, AMOUNT_FIELD)
    assert payload["crop_url"] == f"/api/crops/{DOC_A}/{AMOUNT_FIELD}.png"


def test_item_detail_returns_the_document_context(client: TestClient) -> None:
    payload = detail(client, AMOUNT_FIELD)
    assert DOCUMENT_CONTEXT.items() <= payload["document"].items()


def test_item_detail_returns_the_neighbouring_fields_on_the_same_line(
    client: TestClient,
) -> None:
    # doc-a line 1 holds four fields; the neighbours are the other three, and
    # fld-b-0001 is also line 1 but belongs to doc-b, so it is not a neighbour.
    payload = detail(client, AMOUNT_FIELD)
    assert [str(item["field_id"]) for item in payload["neighbors"]] == [
        "fld-a-0001",
        "fld-a-0003",
        "fld-a-0004",
    ]


def test_item_detail_carries_the_raw_text_and_canonical_value(client: TestClient) -> None:
    payload = detail(client, AMOUNT_FIELD)
    assert payload["raw_text"] == "$1,234.56"
    assert payload["value"] == "1234.56"
    assert payload["confidence"] == pytest.approx(0.20)


# Accept.


def test_accept_sets_human_accepted(client: TestClient, db_path: Path) -> None:
    response = client.post(f"/api/review/items/{ACCEPT_FIELD}/accept")
    assert response.status_code == 200
    assert response.json()["status"] == ReviewStatus.HUMAN_ACCEPTED.value
    assert field_row(db_path, ACCEPT_FIELD)["status"] == ReviewStatus.HUMAN_ACCEPTED.value


def test_accept_is_idempotent(client: TestClient, db_path: Path) -> None:
    first = client.post(f"/api/review/items/{ACCEPT_FIELD}/accept")
    second = client.post(f"/api/review/items/{ACCEPT_FIELD}/accept")
    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["status"] == ReviewStatus.HUMAN_ACCEPTED.value
    with connection(db_path) as conn:
        states = conn.execute(
            "SELECT status FROM fields WHERE field_id = ?", (ACCEPT_FIELD,)
        ).fetchall()
    assert [row["status"] for row in states] == [ReviewStatus.HUMAN_ACCEPTED.value]
    assert corrections(db_path, ACCEPT_FIELD) == []


def test_accept_leaves_the_extracted_value_alone(client: TestClient, db_path: Path) -> None:
    client.post(f"/api/review/items/{ACCEPT_FIELD}/accept")
    assert field_row(db_path, ACCEPT_FIELD)["value"] == "NS12345678"


# Correct.


def test_correct_appends_exactly_one_corrections_row(client: TestClient, db_path: Path) -> None:
    response = client.post(
        f"/api/review/items/{AMOUNT_FIELD}/correct",
        json={"value": "1999.99", "reviewer": REVIEWER},
    )
    assert response.status_code == 200
    rows = corrections(db_path, AMOUNT_FIELD)
    assert len(rows) == 1
    assert rows[0]["new_value"] == "1999.99"
    assert rows[0]["reviewer"] == REVIEWER


def test_correct_sets_human_corrected_and_returns_the_updated_item(
    client: TestClient, db_path: Path
) -> None:
    response = client.post(
        f"/api/review/items/{AMOUNT_FIELD}/correct",
        json={"value": "1999.99", "reviewer": REVIEWER},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["field_id"] == AMOUNT_FIELD
    assert payload["status"] == ReviewStatus.HUMAN_CORRECTED.value
    assert payload["value"] == "1999.99"
    assert field_row(db_path, AMOUNT_FIELD)["status"] == ReviewStatus.HUMAN_CORRECTED.value
    assert detail(client, AMOUNT_FIELD)["value"] == "1999.99"


def test_correct_accepts_a_date_the_family_can_parse(client: TestClient, db_path: Path) -> None:
    response = client.post(
        f"/api/review/items/{DATE_FIELD}/correct",
        json={"value": "2026-07-16", "reviewer": REVIEWER},
    )
    assert response.status_code == 200
    assert response.json()["status"] == ReviewStatus.HUMAN_CORRECTED.value
    assert [row["new_value"] for row in corrections(db_path, DATE_FIELD)] == ["2026-07-16"]


# Corrections are append only: the model's reading stays recoverable.


def test_the_original_extraction_survives_a_correction(client: TestClient, db_path: Path) -> None:
    client.post(
        f"/api/review/items/{AMOUNT_FIELD}/correct",
        json={"value": "1999.99", "reviewer": REVIEWER},
    )
    row = field_row(db_path, AMOUNT_FIELD)
    # A correction never mutates the original extraction, so the field row still
    # says what the model said and only its status moved.
    assert row["value"] == "1234.56"
    assert row["value_cents"] == 123_456
    assert row["raw_text"] == "$1,234.56"
    assert row["status"] == ReviewStatus.HUMAN_CORRECTED.value
    assert corrections(db_path, AMOUNT_FIELD)[0]["old_value"] == "1234.56"


def test_two_successive_corrections_leave_two_rows_in_order(
    client: TestClient, db_path: Path
) -> None:
    first = client.post(
        f"/api/review/items/{AMOUNT_FIELD}/correct",
        json={"value": "1999.99", "reviewer": REVIEWER},
    )
    second = client.post(
        f"/api/review/items/{AMOUNT_FIELD}/correct",
        json={"value": "2222.22", "reviewer": "second-reviewer"},
    )
    assert first.status_code == 200
    assert second.status_code == 200
    rows = corrections(db_path, AMOUNT_FIELD)
    assert [row["new_value"] for row in rows] == ["1999.99", "2222.22"]
    assert [row["reviewer"] for row in rows] == [REVIEWER, "second-reviewer"]
    assert len({str(row["correction_id"]) for row in rows}) == 2
    assert rows[0]["created_at"] <= rows[1]["created_at"]
    # Still recoverable after two rounds of human editing.
    assert field_row(db_path, AMOUNT_FIELD)["value"] == "1234.56"
    assert rows[0]["old_value"] == "1234.56"


# Rejections.


def test_correct_rejects_an_unparseable_amount_with_422_naming_the_family(
    client: TestClient, db_path: Path
) -> None:
    response = client.post(
        f"/api/review/items/{AMOUNT_FIELD}/correct",
        json={"value": "not a number", "reviewer": REVIEWER},
    )
    assert response.status_code == 422
    assert FieldFamily.AMOUNT.value in response.text
    assert corrections(db_path, AMOUNT_FIELD) == []
    assert field_row(db_path, AMOUNT_FIELD)["status"] == ReviewStatus.NEEDS_REVIEW.value


def test_correct_rejects_an_impossible_date_with_422_naming_the_family(
    client: TestClient, db_path: Path
) -> None:
    # Month 13, day 45: shaped like MM/DD/YYYY and still not a date.
    response = client.post(
        f"/api/review/items/{DATE_FIELD}/correct",
        json={"value": "13/45/2026", "reviewer": REVIEWER},
    )
    assert response.status_code == 422
    assert FieldFamily.DATE.value in response.text
    assert corrections(db_path, DATE_FIELD) == []
    assert field_row(db_path, DATE_FIELD)["status"] == ReviewStatus.NEEDS_REVIEW.value


def test_unknown_field_id_is_404_on_detail(client: TestClient) -> None:
    assert client.get(f"/api/review/items/{MISSING_FIELD}").status_code == 404


def test_unknown_field_id_is_404_on_accept(client: TestClient) -> None:
    assert client.post(f"/api/review/items/{MISSING_FIELD}/accept").status_code == 404


def test_unknown_field_id_is_404_on_correct(client: TestClient, db_path: Path) -> None:
    response = client.post(
        f"/api/review/items/{MISSING_FIELD}/correct",
        json={"value": "1999.99", "reviewer": REVIEWER},
    )
    assert response.status_code == 404
    with connection(db_path) as conn:
        (total,) = conn.execute("SELECT COUNT(*) FROM corrections").fetchone()
    assert total == 0
