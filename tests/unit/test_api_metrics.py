"""The metrics route publishes the latest committed scorecard, and nothing when there is none.

The sweep it publishes is the scorecard's own, in the scorecard's own order.
That order is the only thing that says which entry is the held out result, so
reordering it or serving a different sweep in its place loses the one number the
section exists to show.
"""

from datetime import UTC, datetime
from pathlib import Path

from crossfoot.api.dto import MetricsPayload
from crossfoot.api.routes.metrics import SCORECARD_FILENAME, latest_scorecard
from crossfoot.constants import ExceptionType, FieldFamily, ReconMode, SplitName
from crossfoot.models.scorecard import ReconCell, Scorecard, ThresholdPoint

OLDER_RUN_ID = "20260801T120000-aaaaaaa"
NEWER_RUN_ID = "20260807T090000-bbbbbbb"
# Later in time but earlier by name, so ordering by run id alone would pick wrong.
LATEST_RUN_ID = "20260731T000000-ccccccc"

# The committed layout: the calibration curve in ascending threshold order, then
# one final entry holding what the test split reached at the applied threshold.
# The 0.9 curve point outscores the applied one, and the final entry's threshold
# is not the largest, so both "best precision wins" and "sort by threshold" lose
# the held out row.
SWEEP: tuple[ThresholdPoint, ...] = (
    ThresholdPoint(
        field_family=FieldFamily.AMOUNT,
        threshold=0.5,
        auto_accept_precision=0.9802,
        review_rate=0.0198,
    ),
    ThresholdPoint(
        field_family=FieldFamily.AMOUNT,
        threshold=0.7,
        auto_accept_precision=0.9969,
        review_rate=0.0504,
    ),
    ThresholdPoint(
        field_family=FieldFamily.AMOUNT,
        threshold=0.9,
        auto_accept_precision=1.0,
        review_rate=0.42,
    ),
    ThresholdPoint(
        field_family=FieldFamily.AMOUNT,
        threshold=0.7,
        auto_accept_precision=0.9597,
        review_rate=0.0534,
    ),
)


# All a reconciliation run has to publish, and none of it is drawn by this route.
RECONCILIATION: tuple[ReconCell, ...] = (
    ReconCell(
        mode=ReconMode.ORACLE,
        exception_type=ExceptionType.AMOUNT_MISMATCH,
        injected=4,
        detected_true=4,
        detected_false=0,
        injected_dollar_cents=150_000,
        caught_dollar_cents=150_000,
    ),
)


def scorecard(run_id: str, created_at: datetime) -> Scorecard:
    return Scorecard(
        run_id=run_id,
        created_at=created_at,
        git_sha=run_id.split("-")[1],
        dataset_config_hash="b" * 64,
        master_seed=42,
        split=SplitName.TEST,
        models_used=(),
        documents_total=1,
        documents_processed=1,
        documents_unprocessable=0,
        field_accuracy=(),
    )


def test_the_payload_hands_over_the_scorecard_sweep_untouched() -> None:
    card = scorecard(NEWER_RUN_ID, datetime(2026, 8, 7, 9, 0, tzinfo=UTC)).model_copy(
        update={"threshold_sweep": SWEEP}
    )
    payload = MetricsPayload.of(card)
    assert payload.threshold_sweep == SWEEP
    # The held out result is the last entry and has to stay the last entry.
    assert payload.threshold_sweep[-1].auto_accept_precision == 0.9597


def commit(root: Path, card: Scorecard) -> None:
    run_dir = root / card.run_id
    run_dir.mkdir(parents=True)
    (run_dir / SCORECARD_FILENAME).write_text(card.model_dump_json(indent=2), encoding="utf-8")


def test_no_committed_scorecard_is_not_an_error(tmp_path: Path) -> None:
    assert latest_scorecard(tmp_path) is None


def test_the_newest_scorecard_wins_even_when_its_name_sorts_first(tmp_path: Path) -> None:
    commit(tmp_path, scorecard(OLDER_RUN_ID, datetime(2026, 8, 1, 12, 0, tzinfo=UTC)))
    commit(tmp_path, scorecard(NEWER_RUN_ID, datetime(2026, 8, 7, 9, 0, tzinfo=UTC)))
    commit(tmp_path, scorecard(LATEST_RUN_ID, datetime(2026, 8, 9, 6, 0, tzinfo=UTC)))
    latest = latest_scorecard(tmp_path)
    assert latest is not None
    assert latest.run_id == LATEST_RUN_ID


def test_a_reconciliation_scorecard_does_not_displace_the_evaluation_it_followed(
    tmp_path: Path,
) -> None:
    """A full run ends with reconciliation, whose scorecard has nothing to draw.

    It carries recon cells and no field accuracy, calibration or sweep, so
    publishing it because it is newest leaves the metrics page reading "this
    scorecard published none" three times over and the held out result nowhere.
    """
    evaluation = scorecard(NEWER_RUN_ID, datetime(2026, 8, 7, 9, 0, tzinfo=UTC)).model_copy(
        update={"threshold_sweep": SWEEP}
    )
    commit(tmp_path, evaluation)
    commit(
        tmp_path,
        scorecard(
            "recon-oracle-20260808T000000", datetime(2026, 8, 8, 0, 0, tzinfo=UTC)
        ).model_copy(update={"reconciliation": RECONCILIATION}),
    )

    latest = latest_scorecard(tmp_path)
    assert latest is not None
    assert latest.run_id == NEWER_RUN_ID
    assert latest.threshold_sweep == SWEEP


def test_a_scorecard_that_no_longer_validates_is_skipped_rather_than_fatal(tmp_path: Path) -> None:
    commit(tmp_path, scorecard(OLDER_RUN_ID, datetime(2026, 8, 1, 12, 0, tzinfo=UTC)))
    broken = tmp_path / "20260808T000000-ddddddd"
    broken.mkdir()
    (broken / SCORECARD_FILENAME).write_text('{"run_id": "half a scorecard"}', encoding="utf-8")
    latest = latest_scorecard(tmp_path)
    assert latest is not None
    assert latest.run_id == OLDER_RUN_ID
