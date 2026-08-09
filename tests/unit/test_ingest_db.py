"""Rebuilding the review database: what it recomputes and what it must leave alone.

The dataset here is the hand built corpus from `test_scoring`, written to disk in
the shape `build_database` reads: a manifest, a ledger, and one saved extraction
file per split. No document file is needed, because a record with a saved
extraction never has its bytes routed again.
"""

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_scoring import _corpus

from crossfoot.api.app import create_app
from crossfoot.constants import FieldFamily, ReviewStatus, SplitName
from crossfoot.db import connect, review, thresholds
from crossfoot.generator.ledger_gen import generate_ledger
from crossfoot.ingest_db import IngestCounts, build_database, extraction_run_id
from crossfoot.models.manifest import DatasetManifest
from crossfoot.models.scorecard import Scorecard

CONFIG_HASH = "e" * 64
DB_NAME = "crossfoot.db"
REVIEWER = "rc"


@pytest.fixture
def dataset(tmp_path: Path) -> Path:
    """A dataset directory plus the extraction files a live run would have saved."""
    documents, records = _corpus()
    dataset_dir = tmp_path / "dataset"
    dataset_dir.mkdir()
    manifest = DatasetManifest(
        master_seed=42,
        generator_version="0.1.0",
        config_hash=CONFIG_HASH,
        records=tuple(records.values()),
    )
    (dataset_dir / "manifest.json").write_text(manifest.model_dump_json(), encoding="utf-8")
    (dataset_dir / "ledger.json").write_text(
        generate_ledger(42).model_dump_json(), encoding="utf-8"
    )

    extractions = tmp_path / "extractions"
    extractions.mkdir()
    for split in SplitName:
        of_split = [document for document in documents if records[document.doc_id].split is split]
        path = extractions / f"{extraction_run_id(split, CONFIG_HASH)}.json"
        payload = [json.loads(document.model_dump_json()) for document in of_split]
        path.write_text(json.dumps(payload), encoding="utf-8")
    return dataset_dir


def _build(dataset_dir: Path, tmp_path: Path) -> IngestCounts:
    return build_database(
        dataset_dir=dataset_dir,
        db_path=tmp_path / DB_NAME,
        extractions_dir=dataset_dir.parent / "extractions",
        cost_db=tmp_path / "costs.db",
    )


def _field_rows(db_path: Path) -> dict[str, sqlite3.Row]:
    connection = connect(db_path)
    try:
        rows = connection.execute("SELECT * FROM fields").fetchall()
    finally:
        connection.close()
    return {str(row["field_id"]): row for row in rows}


def test_a_build_writes_graded_confidences_and_an_operating_point(
    dataset: Path, tmp_path: Path
) -> None:
    counts = _build(dataset, tmp_path)
    rows = _field_rows(tmp_path / DB_NAME)
    confidences = {round(float(row["confidence"]), 6) for row in rows.values()}
    assert confidences - {0.0, 1.0}
    assert 0 < counts.auto_accepted < counts.fields
    assert [point.field_family for point in counts.thresholds] == [FieldFamily.AMOUNT]


def test_the_applied_thresholds_are_readable_back(dataset: Path, tmp_path: Path) -> None:
    counts = _build(dataset, tmp_path)
    connection = connect(tmp_path / DB_NAME)
    try:
        stored = thresholds.applied(connection)
        row = connection.execute("SELECT * FROM applied_thresholds").fetchone()
    finally:
        connection.close()
    assert stored == counts.thresholds
    # The splits the point was earned on are recorded, not left to be assumed.
    assert str(row["fit_split"]) == SplitName.TRAIN.value
    assert str(row["threshold_split"]) == SplitName.CALIBRATION.value


def test_the_metrics_route_publishes_the_operating_point_that_was_applied(
    dataset: Path, tmp_path: Path
) -> None:
    counts = _build(dataset, tmp_path)
    scorecards = tmp_path / "scorecards"
    card = Scorecard(
        run_id="20260801T120000-aaaaaaa",
        created_at=datetime(2026, 8, 1, 12, 0, tzinfo=UTC),
        git_sha="aaaaaaa",
        dataset_config_hash=CONFIG_HASH,
        master_seed=42,
        split=SplitName.TEST,
        models_used=(),
        documents_total=1,
        documents_processed=1,
        documents_unprocessable=0,
        field_accuracy=(),
    )
    run_dir = scorecards / card.run_id
    run_dir.mkdir(parents=True)
    (run_dir / "scorecard.json").write_text(card.model_dump_json(), encoding="utf-8")
    crops = tmp_path / "crops"
    crops.mkdir()

    app = create_app(db_path=tmp_path / DB_NAME, crops_root=crops, scorecards_dir=scorecards)
    with TestClient(app) as client:
        payload = client.get("/api/metrics").json()
    # The scorecard carries no sweep at all, so anything here came from the build.
    assert card.threshold_sweep == ()
    assert payload["threshold_sweep"] == [
        json.loads(point.model_dump_json()) for point in counts.thresholds
    ]


def test_a_human_corrected_field_is_left_alone_by_a_rebuild(dataset: Path, tmp_path: Path) -> None:
    db_path = tmp_path / DB_NAME
    _build(dataset, tmp_path)
    before = _field_rows(db_path)
    corrected, accepted, untouched = sorted(before)[:3]

    connection = connect(db_path)
    try:
        review.correct(connection, field_id=corrected, new_value="1.00", reviewer=REVIEWER)
        review.accept(connection, accepted)
    finally:
        connection.close()

    _build(dataset, tmp_path)
    after = _field_rows(db_path)
    assert after[corrected]["status"] == ReviewStatus.HUMAN_CORRECTED.value
    assert after[accepted]["status"] == ReviewStatus.HUMAN_ACCEPTED.value
    # Not re-scored either: a decided field keeps the confidence it was decided at.
    assert after[corrected]["confidence"] == before[corrected]["confidence"]
    assert after[accepted]["confidence"] == before[accepted]["confidence"]
    # The correction itself survives, and an undecided neighbour is still rebuilt.
    assert after[corrected]["value"] == before[corrected]["value"]
    assert after[untouched]["status"] in {
        ReviewStatus.AUTO_ACCEPTED.value,
        ReviewStatus.NEEDS_REVIEW.value,
    }
