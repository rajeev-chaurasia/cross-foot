"""Contract tests for the summary tile and the published metrics in crossfoot.api.

Written against docs/contracts-phase3.md before the implementation exists, so
the module-level importorskip keeps collection clean today. Every expected
number is hand computed from the rows `seed` writes, and the arithmetic is in
the comment beside it.

Pinned surface, binding in the phase 2 sense:

    GET /api/stats/summary -> documents_processed, fields_extracted,
        auto_accept_rate, review_queue_depth, open_exception_count,
        gross_dollars_at_risk_cents, cost_per_document_microusd
    GET /api/metrics -> {"scorecard": ..., "calibration": [...],
        "threshold_sweep": [...]}

Money stays in int cents and LLM cost in microusd, matching
`CostCell.list_price_microusd`. `documents_processed` counts documents whose
route is not UNPROCESSABLE, `gross_dollars_at_risk_cents` sums the absolute
impact of OPEN exceptions only, and `cost_per_document_microusd` divides the
ledger's list price column by the documents processed, never by the actual cost
column, so a free tier run still reports what the work would cost.
"""

import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from api_seed import (
    DB_NAME,
    connection,
    create_schema,
    insert_document,
    insert_exception,
    insert_field,
    insert_llm_call,
    signals_json,
)
from fastapi.testclient import TestClient

from crossfoot.constants import (
    DocType,
    ExceptionStatus,
    ExceptionType,
    ExtractionRoute,
    FieldFamily,
    FieldName,
    IngestErrorKind,
    QualityTier,
    ReviewStatus,
    SplitName,
)
from crossfoot.models.scorecard import (
    CalibrationBin,
    FieldAccuracyCell,
    Scorecard,
    ThresholdPoint,
)

api = pytest.importorskip("crossfoot.api")

# The route a document of each tier actually takes. Field signals carry the
# route, because that is what a router reads off a file; the tier stays a
# documents-table column, which is a thing this dataset knows and a production
# pipeline does not.
ROUTES: dict[QualityTier, ExtractionRoute] = {
    QualityTier.CLEAN_DIGITAL: ExtractionRoute.DIGITAL_PDF,
    QualityTier.SCAN_LIGHT: ExtractionRoute.SCANNED_PDF,
    QualityTier.SCAN_HEAVY: ExtractionRoute.SCANNED_PDF,
    QualityTier.CSV: ExtractionRoute.CSV,
    QualityTier.XLSX: ExtractionRoute.XLSX,
    QualityTier.CORRUPTED: ExtractionRoute.UNPROCESSABLE,
}


SUMMARY_FIELDS = frozenset(
    {
        "documents_processed",
        "fields_extracted",
        "auto_accept_rate",
        "review_queue_depth",
        "open_exception_count",
        "gross_dollars_at_risk_cents",
        "cost_per_document_microusd",
    }
)

OLD_RUN_ID = "20260801T120000-aaaaaaa"
NEW_RUN_ID = "20260807T090000-bbbbbbb"


def build_scorecard(run_id: str, created_at: datetime, *, review_rate: float) -> Scorecard:
    return Scorecard(
        run_id=run_id,
        created_at=created_at,
        git_sha=run_id.split("-")[1],
        dataset_config_hash="b" * 64,
        master_seed=42,
        split=SplitName.TEST,
        models_used=("gemini-3.5-flash",),
        documents_total=3,
        documents_processed=2,
        documents_unprocessable=1,
        field_accuracy=(
            FieldAccuracyCell(
                field_family=FieldFamily.AMOUNT,
                quality_tier=QualityTier.CSV,
                fields_in_truth=12,
                fields_expected=10,
                fields_extracted=9,
                correct_canonical=9,
                correct_raw=8,
            ),
        ),
        calibration=(
            CalibrationBin(
                field_family=FieldFamily.AMOUNT,
                mean_confidence=0.91,
                empirical_accuracy=0.88,
                count=120,
            ),
            CalibrationBin(
                field_family=FieldFamily.REFERENCE,
                mean_confidence=0.55,
                empirical_accuracy=0.51,
                count=80,
            ),
        ),
        threshold_sweep=(
            ThresholdPoint(
                field_family=FieldFamily.AMOUNT,
                threshold=0.9,
                auto_accept_precision=0.9964,
                review_rate=review_rate,
            ),
        ),
        notes=f"contract fixture for {run_id}",
    )


OLDER = build_scorecard(OLD_RUN_ID, datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC), review_rate=0.31)
NEWER = build_scorecard(NEW_RUN_ID, datetime(2026, 8, 7, 9, 0, 0, tzinfo=UTC), review_rate=0.181)


def seed(conn: sqlite3.Connection) -> None:
    """Three documents, ten fields, four exceptions, and a free tier ledger."""
    insert_document(
        conn,
        doc_id="doc-s1",
        doc_type=DocType.PARTS_STATEMENT,
        quality_tier=QualityTier.CSV,
        route=ExtractionRoute.CSV,
        split=SplitName.TRAIN,
    )
    insert_document(
        conn,
        doc_id="doc-s2",
        doc_type=DocType.WARRANTY_CREDIT_MEMO,
        quality_tier=QualityTier.CLEAN_DIGITAL,
        route=ExtractionRoute.DIGITAL_PDF,
        split=SplitName.TEST,
    )
    # Unprocessable, so it never counts as processed and never costs anything.
    insert_document(
        conn,
        doc_id="doc-s3",
        doc_type=DocType.FLOORPLAN_STATEMENT,
        quality_tier=QualityTier.CORRUPTED,
        route=ExtractionRoute.UNPROCESSABLE,
        error_kind=IngestErrorKind.UNRECOGNIZED,
    )

    # doc-s1: four auto accepted plus one in the queue.
    for index, confidence in enumerate((0.99, 0.98, 0.97, 0.96), start=1):
        _amount_field(conn, f"fld-s1-{index:04d}", "doc-s1", QualityTier.CSV, confidence)
    _amount_field(
        conn,
        "fld-s1-0005",
        "doc-s1",
        QualityTier.CSV,
        0.10,
        status=ReviewStatus.NEEDS_REVIEW,
    )
    # doc-s2: three auto accepted, one in the queue, one accepted by a human.
    for index, confidence in enumerate((0.95, 0.94, 0.93), start=1):
        _amount_field(conn, f"fld-s2-{index:04d}", "doc-s2", QualityTier.CLEAN_DIGITAL, confidence)
    _amount_field(
        conn,
        "fld-s2-0004",
        "doc-s2",
        QualityTier.CLEAN_DIGITAL,
        0.20,
        status=ReviewStatus.NEEDS_REVIEW,
    )
    _amount_field(
        conn,
        "fld-s2-0005",
        "doc-s2",
        QualityTier.CLEAN_DIGITAL,
        0.30,
        status=ReviewStatus.HUMAN_ACCEPTED,
    )

    insert_exception(
        conn,
        exception_id="exc-s1",
        exception_type=ExceptionType.AMOUNT_MISMATCH,
        doc_id="doc-s1",
        statement_line_no=1,
        dollar_impact_cents=150_000,
        status=ExceptionStatus.OPEN,
        explanation="statement is 1500.00 over the ledger",
    )
    insert_exception(
        conn,
        exception_id="exc-s2",
        exception_type=ExceptionType.MISSING_FROM_STATEMENT,
        doc_id="doc-s2",
        ledger_entry_id="led-parts_payable-00007",
        dollar_impact_cents=-25_000,  # the negative ledger amount
        status=ExceptionStatus.OPEN,
        explanation="ledger entry never appeared on the statement",
    )
    insert_exception(
        conn,
        exception_id="exc-s3",
        exception_type=ExceptionType.TIMING_DIFFERENCE,
        doc_id="doc-s2",
        statement_line_no=2,
        dollar_impact_cents=0,
        memo_amount_cents=50_000,
        status=ExceptionStatus.OPEN,
        explanation="posted outside the statement period",
    )
    insert_exception(
        conn,
        exception_id="exc-s4",
        exception_type=ExceptionType.DUPLICATE,
        doc_id="doc-s1",
        statement_line_no=3,
        dollar_impact_cents=-900_000,
        status=ExceptionStatus.RESOLVED,
        explanation="already credited last month",
    )

    # A free tier run: every actual cost is zero and every list price is not.
    insert_llm_call(conn, call_id="call-1", doc_id="doc-s1", list_price_microusd=36_000)
    insert_llm_call(conn, call_id="call-2", doc_id="doc-s1", list_price_microusd=24_000)
    insert_llm_call(conn, call_id="call-3", doc_id="doc-s2", list_price_microusd=30_000)


def _amount_field(
    conn: sqlite3.Connection,
    field_id: str,
    doc_id: str,
    tier: QualityTier,
    confidence: float,
    *,
    status: str = ReviewStatus.AUTO_ACCEPTED,
) -> None:
    insert_field(
        conn,
        field_id=field_id,
        doc_id=doc_id,
        line_no=1,
        name=FieldName.LINE_AMOUNT,
        family=FieldFamily.AMOUNT,
        raw_text="$10.00",
        value="10.00",
        value_cents=1_000,
        confidence=confidence,
        status=status,
        signals=signals_json(ROUTES[tier], validator_pass=1.0),
    )


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / DB_NAME
    with connection(path) as conn:
        create_schema(conn)
        seed(conn)
    return path


@pytest.fixture
def scorecards_dir(tmp_path: Path) -> Path:
    """Two committed scorecards, the newer one both later and later named."""
    root = tmp_path / "scorecards"
    for card in (OLDER, NEWER):
        run_dir = root / card.run_id
        run_dir.mkdir(parents=True)
        (run_dir / "scorecard.json").write_text(card.model_dump_json(indent=2), encoding="utf-8")
    return root


@pytest.fixture
def client(tmp_path: Path, db_path: Path, scorecards_dir: Path) -> Iterator[TestClient]:
    crops_root = tmp_path / "crops"
    crops_root.mkdir()
    app = api.create_app(db_path=db_path, crops_root=crops_root, scorecards_dir=scorecards_dir)
    with TestClient(app) as test_client:
        yield test_client


def summary(client: TestClient) -> dict[str, Any]:
    response = client.get("/api/stats/summary")
    assert response.status_code == 200
    payload: dict[str, Any] = response.json()
    return payload


def status_counts(db_path: Path) -> dict[str, int]:
    with connection(db_path) as conn:
        rows = conn.execute("SELECT status, COUNT(*) AS n FROM fields GROUP BY status").fetchall()
    return {str(row["status"]): int(row["n"]) for row in rows}


# The summary tile.


def test_summary_returns_every_documented_field(client: TestClient) -> None:
    assert set(summary(client)) >= SUMMARY_FIELDS


def test_documents_processed_excludes_the_unprocessable_document(client: TestClient) -> None:
    # Three documents seeded, one of them route=unprocessable.
    assert summary(client)["documents_processed"] == 2


def test_fields_extracted_counts_every_field_row(client: TestClient) -> None:
    # 5 fields on doc-s1 plus 5 on doc-s2.
    assert summary(client)["fields_extracted"] == 10


def test_auto_accept_rate_is_the_auto_accepted_share(client: TestClient) -> None:
    # 7 of 10 fields are auto accepted: 4 on doc-s1 and 3 on doc-s2.
    assert summary(client)["auto_accept_rate"] == pytest.approx(0.7)


def test_review_queue_depth_is_the_needs_review_count(client: TestClient) -> None:
    # fld-s1-0005 and fld-s2-0004.
    assert summary(client)["review_queue_depth"] == 2


def test_auto_accept_rate_and_queue_depth_agree_with_the_seeded_statuses(
    client: TestClient, db_path: Path
) -> None:
    counts = status_counts(db_path)
    assert counts == {
        ReviewStatus.AUTO_ACCEPTED.value: 7,
        ReviewStatus.NEEDS_REVIEW.value: 2,
        ReviewStatus.HUMAN_ACCEPTED.value: 1,
    }
    payload = summary(client)
    assert payload["fields_extracted"] == sum(counts.values())
    assert payload["auto_accept_rate"] == pytest.approx(
        counts[ReviewStatus.AUTO_ACCEPTED.value] / sum(counts.values())
    )
    assert payload["review_queue_depth"] == counts[ReviewStatus.NEEDS_REVIEW.value]
    # 0.7 * 10 auto accepted plus 2 still queued is 9, the one field left over
    # being the one a human already accepted.
    auto_accepted = payload["auto_accept_rate"] * payload["fields_extracted"]
    assert auto_accepted + payload["review_queue_depth"] == pytest.approx(9)


def test_open_exception_count_ignores_the_resolved_one(client: TestClient) -> None:
    # exc-s1, exc-s2 and exc-s3 are open; exc-s4 is resolved.
    assert summary(client)["open_exception_count"] == 3


def test_gross_dollars_at_risk_sums_absolute_open_impact(client: TestClient) -> None:
    # 150_000 + abs(-25_000) + 0 = 175_000. The resolved -900_000 is not at risk
    # and the timing difference brings no dollars with it.
    assert summary(client)["gross_dollars_at_risk_cents"] == 175_000


# Cost per document comes from the list price column, never the actual cost.


def test_cost_per_document_uses_the_ledger_list_price(client: TestClient) -> None:
    # (36_000 + 24_000 + 30_000) microusd over 2 processed documents = 45_000.
    assert summary(client)["cost_per_document_microusd"] == 45_000


def test_a_free_tier_run_still_reports_a_nonzero_cost_per_document(
    client: TestClient, db_path: Path
) -> None:
    with connection(db_path) as conn:
        (actual,) = conn.execute("SELECT SUM(actual_cost_microusd) FROM llm_calls").fetchone()
        (listed,) = conn.execute("SELECT SUM(list_price_microusd) FROM llm_calls").fetchone()
    assert actual == 0  # every seeded call was served by a free tier
    assert listed == 90_000
    assert summary(client)["cost_per_document_microusd"] == 45_000


# The published metrics.


def test_metrics_returns_the_latest_committed_scorecard(client: TestClient) -> None:
    response = client.get("/api/metrics")
    assert response.status_code == 200
    payload = response.json()
    assert payload["scorecard"]["run_id"] == NEW_RUN_ID
    assert payload["scorecard"] == json.loads(NEWER.model_dump_json())


def test_metrics_returns_the_calibration_points(client: TestClient) -> None:
    payload = client.get("/api/metrics").json()
    expected = json.loads(NEWER.model_dump_json())["calibration"]
    assert payload["calibration"] == expected
    assert [point["field_family"] for point in payload["calibration"]] == [
        FieldFamily.AMOUNT.value,
        FieldFamily.REFERENCE.value,
    ]


def test_metrics_returns_the_threshold_sweep(client: TestClient) -> None:
    payload = client.get("/api/metrics").json()
    expected = json.loads(NEWER.model_dump_json())["threshold_sweep"]
    assert payload["threshold_sweep"] == expected
    # The newer run's operating point, not the older run's 0.31.
    assert payload["threshold_sweep"][0]["review_rate"] == pytest.approx(0.181)
