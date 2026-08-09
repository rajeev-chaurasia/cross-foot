"""The published figures: what they may claim, and what they must refuse to claim.

Two rules carry the weight here. A cell nothing was extracted from has no
accuracy, which is not the same as an accuracy of zero. And the sweep layout the
scorecard write path emits has to be the one the renderer reads, or a figure
would mark a curve point as an operating point nothing ever applied.
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from crossfoot.confidence.calibration import ConfidenceSample, sweep_point
from crossfoot.constants import (
    ExceptionType,
    FieldFamily,
    QualityTier,
    ReconMode,
    SplitName,
)
from crossfoot.evals import plots
from crossfoot.evals.runner import sweep_curve
from crossfoot.models.scorecard import (
    CalibrationBin,
    FieldAccuracyCell,
    ReconCell,
    Scorecard,
    ThresholdPoint,
)

RUN_ID = "20260809T120000-abc1234"
APPLIED = 0.83
CURVE_THRESHOLDS = (0.0, 0.5, APPLIED, 0.95)


def _sweep(family: FieldFamily) -> tuple[ThresholdPoint, ...]:
    """A family's run in the published layout: curve first, achieved point last."""
    curve = tuple(
        ThresholdPoint(
            field_family=family,
            threshold=threshold,
            auto_accept_precision=0.90 + threshold / 10,
            review_rate=threshold / 2,
        )
        for threshold in CURVE_THRESHOLDS
    )
    achieved = ThresholdPoint(
        field_family=family,
        threshold=APPLIED,
        auto_accept_precision=0.9738,
        review_rate=0.32,
    )
    return (*curve, achieved)


def _scorecard(
    *,
    field_accuracy: tuple[FieldAccuracyCell, ...] = (),
    calibration: tuple[CalibrationBin, ...] = (),
    threshold_sweep: tuple[ThresholdPoint, ...] = (),
    reconciliation: tuple[ReconCell, ...] = (),
    run_id: str = RUN_ID,
    created_at: datetime = datetime(2026, 8, 9, 12, 0, tzinfo=UTC),
) -> Scorecard:
    return Scorecard(
        run_id=run_id,
        created_at=created_at,
        git_sha="abc1234",
        dataset_config_hash="e" * 64,
        master_seed=42,
        split=SplitName.TEST,
        models_used=(),
        documents_total=4,
        documents_processed=3,
        documents_unprocessable=1,
        field_accuracy=field_accuracy,
        calibration=calibration,
        threshold_sweep=threshold_sweep,
        reconciliation=reconciliation,
    )


def _cell(tier: QualityTier, *, extracted: int, correct: int) -> FieldAccuracyCell:
    return FieldAccuracyCell(
        field_family=FieldFamily.AMOUNT,
        quality_tier=tier,
        fields_in_truth=10,
        fields_expected=10,
        fields_extracted=extracted,
        correct_canonical=correct,
        correct_raw=correct,
    )


def _full_scorecard() -> Scorecard:
    return _scorecard(
        field_accuracy=(
            _cell(QualityTier.CLEAN_DIGITAL, extracted=10, correct=9),
            _cell(QualityTier.XLSX, extracted=0, correct=0),
        ),
        calibration=(
            CalibrationBin(
                field_family=FieldFamily.AMOUNT,
                mean_confidence=0.62,
                empirical_accuracy=0.55,
                count=40,
            ),
            CalibrationBin(
                field_family=FieldFamily.AMOUNT,
                mean_confidence=0.94,
                empirical_accuracy=0.98,
                count=40,
            ),
        ),
        threshold_sweep=_sweep(FieldFamily.AMOUNT),
        reconciliation=(
            ReconCell(
                mode=ReconMode.END_TO_END,
                exception_type=ExceptionType.SHORT_PAY,
                injected=7,
                detected_true=3,
                detected_false=4,
                injected_dollar_cents=192_950,
                caught_dollar_cents=89_269,
            ),
            ReconCell(
                mode=ReconMode.END_TO_END,
                exception_type=ExceptionType.TIMING_DIFFERENCE,
                injected=13,
                detected_true=9,
                detected_false=0,
                injected_dollar_cents=0,
                caught_dollar_cents=0,
            ),
        ),
    )


def _write(scorecard: Scorecard, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / plots.SCORECARD_FILENAME
    path.write_text(scorecard.model_dump_json(indent=2), encoding="utf-8")
    return path


def test_a_cell_nothing_was_extracted_from_has_no_accuracy() -> None:
    assert plots.cell_accuracy(_cell(QualityTier.XLSX, extracted=0, correct=0)) is None


def test_a_cell_read_wrongly_every_time_has_an_accuracy_of_zero() -> None:
    """Absent and nought are different claims, and only one of them blames the model."""
    assert plots.cell_accuracy(_cell(QualityTier.SCAN_HEAVY, extracted=10, correct=0)) == 0.0


def test_family_sweeps_recovers_the_applied_point_and_the_achieved_point() -> None:
    (sweep,) = plots.family_sweeps(_sweep(FieldFamily.REFERENCE))
    assert sweep.field_family is FieldFamily.REFERENCE
    assert sweep.applied.threshold == APPLIED
    assert sweep.achieved.review_rate == 0.32
    assert sweep.applied is not sweep.achieved
    assert len(sweep.curve) == len(CURVE_THRESHOLDS)


def test_family_sweeps_rejects_a_result_at_a_threshold_off_its_curve() -> None:
    points = (
        *_sweep(FieldFamily.AMOUNT)[:-1],
        ThresholdPoint(
            field_family=FieldFamily.AMOUNT,
            threshold=0.111,
            auto_accept_precision=0.9,
            review_rate=0.2,
        ),
    )
    with pytest.raises(plots.MalformedSweepError):
        plots.family_sweeps(points)


def test_family_sweeps_rejects_a_family_with_no_curve_at_all() -> None:
    with pytest.raises(plots.MalformedSweepError):
        plots.family_sweeps(_sweep(FieldFamily.AMOUNT)[-1:])


def test_the_write_path_lays_the_sweep_out_the_way_the_renderer_reads_it() -> None:
    """The two modules only agree if the applied threshold lands on the curve."""
    samples = [
        ConfidenceSample(FieldFamily.AMOUNT, index / 20, index % 4 > 0, SplitName.CALIBRATION)
        for index in range(20)
    ]
    applied = 0.4321
    curve = sweep_curve(FieldFamily.AMOUNT, samples, applied)
    achieved = sweep_point(FieldFamily.AMOUNT, samples, applied)
    (sweep,) = plots.family_sweeps([*curve, achieved])
    assert sweep.applied.threshold == applied


def test_render_writes_every_figure_the_scorecard_supports(tmp_path: Path) -> None:
    path = _write(_full_scorecard(), tmp_path / "scorecards" / RUN_ID)
    rendered = plots.render_figures(path)
    assert not rendered.skipped
    assert {figure.name for figure in rendered.written} == set(
        plots.figure_names(_full_scorecard())
    )
    assert all(figure.stat().st_size > 0 for figure in rendered.written)


def test_render_skips_a_figure_whose_numbers_the_scorecard_never_published(
    tmp_path: Path,
) -> None:
    path = _write(
        _scorecard(field_accuracy=(_cell(QualityTier.CSV, extracted=10, correct=10),)),
        tmp_path / "scorecards" / RUN_ID,
    )
    rendered = plots.render_figures(path)
    assert [figure.name for figure in rendered.written] == [plots.FIELD_ACCURACY_PNG]
    assert len(rendered.skipped) == 3


def test_one_scorecard_renders_the_same_bytes_twice(tmp_path: Path) -> None:
    """No timestamp inside a figure and no jitter, so a rerun is a no-op in git."""
    first = plots.render_figures(_write(_full_scorecard(), tmp_path / "first" / RUN_ID))
    second = plots.render_figures(_write(_full_scorecard(), tmp_path / "second" / RUN_ID))
    for left, right in zip(first.written, second.written, strict=True):
        assert left.name == right.name
        assert left.read_bytes() == right.read_bytes()


def test_the_exception_figure_pairs_the_counterpart_reconciliation_mode(tmp_path: Path) -> None:
    """Oracle against end to end is the point, and one reconcile run writes one mode."""
    root = tmp_path / "scorecards"
    oracle = _scorecard(
        run_id="recon-oracle-20260809T100000",
        created_at=datetime(2026, 8, 9, 10, 0, tzinfo=UTC),
        reconciliation=(
            ReconCell(
                mode=ReconMode.ORACLE,
                exception_type=ExceptionType.SHORT_PAY,
                injected=7,
                detected_true=7,
                detected_false=0,
                injected_dollar_cents=192_950,
                caught_dollar_cents=192_950,
            ),
        ),
    )
    _write(oracle, root / oracle.run_id)
    end_to_end = _full_scorecard()
    path = _write(end_to_end, root / end_to_end.run_id)
    sources = plots._recon_sources(end_to_end, root)
    assert set(sources) == {ReconMode.ORACLE, ReconMode.END_TO_END}
    assert sources[ReconMode.ORACLE].run_id == oracle.run_id
    assert plots.render_figures(path, scorecards_root=root).written


def test_a_caption_names_the_run_id_that_produced_it() -> None:
    assert RUN_ID in plots.scorecard_stamp(_full_scorecard())


def test_latest_scorecard_path_picks_the_newest_committed_run(tmp_path: Path) -> None:
    root = tmp_path / "scorecards"
    _write(_scorecard(run_id="older", created_at=datetime(2026, 8, 1, tzinfo=UTC)), root / "older")
    _write(_scorecard(run_id="newer", created_at=datetime(2026, 8, 8, tzinfo=UTC)), root / "newer")
    (root / "junk").mkdir()
    (root / "junk" / plots.SCORECARD_FILENAME).write_text("not a scorecard", encoding="utf-8")
    found = plots.latest_scorecard_path(root)
    assert found is not None and found.parent.name == "newer"


def test_latest_scorecard_path_is_none_when_nothing_is_committed(tmp_path: Path) -> None:
    assert plots.latest_scorecard_path(tmp_path) is None
