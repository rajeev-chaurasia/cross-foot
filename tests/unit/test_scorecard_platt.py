"""A published figure has to be reproducible from the committed scorecard alone.

The correction applied to a run used to ride in the free text notes, where no
reader could rebuild it. It rides in the model now, so these pin the two things
that makes true: the cells survive the JSON the build commits, and an
uncalibrated run publishes an empty collection rather than a silence a boolean
somewhere else could contradict.
"""

from datetime import UTC, datetime

from crossfoot.constants import FieldFamily, QualityTier, SplitName
from crossfoot.models.scorecard import FieldAccuracyCell, PlattCell, Scorecard

CELLS = (
    PlattCell(field_family=FieldFamily.AMOUNT, slope=0.7213, intercept=-0.3114),
    PlattCell(field_family=FieldFamily.TEXT, slope=1.1408, intercept=0.0521),
)

ACCURACY = FieldAccuracyCell(
    field_family=FieldFamily.AMOUNT,
    quality_tier=QualityTier.CLEAN_DIGITAL,
    fields_in_truth=4,
    fields_expected=4,
    fields_extracted=4,
    correct_canonical=4,
    correct_raw=4,
)


def _scorecard(platt: tuple[PlattCell, ...]) -> Scorecard:
    return Scorecard(
        run_id="20260809T101500-abc1234",
        created_at=datetime(2026, 8, 9, 10, 15, tzinfo=UTC),
        git_sha="abc1234",
        dataset_config_hash="0" * 64,
        master_seed=42,
        split=SplitName.TEST,
        models_used=("qwen2.5vl:7b",),
        documents_total=1,
        documents_processed=1,
        documents_unprocessable=0,
        field_accuracy=(ACCURACY,),
        platt_scaling=platt,
    )


def _round_trip(platt: tuple[PlattCell, ...]) -> Scorecard:
    return Scorecard.model_validate_json(_scorecard(platt).model_dump_json())


def test_platt_cells_survive_the_json_the_build_commits() -> None:
    assert _round_trip(CELLS).platt_scaling == CELLS


def test_an_uncalibrated_scorecard_publishes_an_empty_collection() -> None:
    assert _round_trip(()).platt_scaling == ()


def test_a_scorecard_written_before_the_field_existed_reads_as_uncalibrated() -> None:
    # Committed scorecards predate the field, and dropping the key is what they
    # look like on disk. Empty is the right reading of them: they were not.
    payload = _scorecard(CELLS).model_dump(mode="json")
    del payload["platt_scaling"]
    assert Scorecard.model_validate(payload).platt_scaling == ()
