"""Split guards, threshold fallbacks, and binning edges in the calibration module."""

import pytest

from crossfoot.confidence.calibration import (
    BIN_COUNT,
    ConfidenceSample,
    PlattScaler,
    SplitDisciplineError,
    TrainingSample,
    choose_thresholds,
    expected_calibration_error,
    fit_platt_scaling,
    fit_scorers,
    reliability_bins,
)
from crossfoot.constants import FieldFamily, QualityTier, SplitName
from crossfoot.models.extraction import FieldSignals

HIGH = FieldSignals(quality_tier=QualityTier.CLEAN_DIGITAL, validator_pass=1.0)
LOW = FieldSignals(quality_tier=QualityTier.CLEAN_DIGITAL, validator_pass=0.0)


def _confidences(
    rows: tuple[tuple[float, bool], ...],
    family: FieldFamily = FieldFamily.AMOUNT,
    split: SplitName = SplitName.CALIBRATION,
) -> list[ConfidenceSample]:
    return [
        ConfidenceSample(field_family=family, confidence=confidence, correct=correct, split=split)
        for confidence, correct in rows
    ]


def test_scorers_are_fit_per_family() -> None:
    samples = [
        TrainingSample(
            field_family=family,
            signals=HIGH if correct else LOW,
            correct=correct,
            split=SplitName.TRAIN,
        )
        for family in (FieldFamily.AMOUNT, FieldFamily.DATE)
        for correct in (True, False)
    ]
    models = fit_scorers(samples, split=SplitName.TRAIN)
    assert set(models) == {FieldFamily.AMOUNT, FieldFamily.DATE}
    assert models[FieldFamily.DATE].field_family is FieldFamily.DATE


def test_unreachable_precision_target_reviews_everything() -> None:
    # Nothing this family accepts is correct, so no threshold earns the target
    # and the sweep falls back to a review queue holding every field.
    points = choose_thresholds(
        _confidences(((0.9, False), (0.5, False))), split=SplitName.CALIBRATION
    )
    assert len(points) == 1
    assert points[0].review_rate == pytest.approx(1.0)
    assert points[0].threshold > 0.9


def test_thresholds_come_back_one_per_family() -> None:
    samples = [
        *_confidences(((0.9, True), (0.1, False)), FieldFamily.AMOUNT),
        *_confidences(((0.9, True), (0.1, False)), FieldFamily.TEXT),
    ]
    points = choose_thresholds(samples, split=SplitName.CALIBRATION)
    assert [point.field_family for point in points] == [FieldFamily.AMOUNT, FieldFamily.TEXT]


@pytest.mark.parametrize("split", [SplitName.TRAIN, SplitName.TEST])
def test_platt_scaling_refuses_every_split_but_calibration(split: SplitName) -> None:
    with pytest.raises(SplitDisciplineError):
        fit_platt_scaling(_confidences(((0.9, True), (0.1, False)), split=split), split=split)


def test_platt_scaling_stays_monotone_in_confidence() -> None:
    rows = tuple((index / 10, index >= 5) for index in range(10))
    scaler = fit_platt_scaling(_confidences(rows), split=SplitName.CALIBRATION)
    assert isinstance(scaler, PlattScaler)
    rescaled = [scaler.apply(confidence) for confidence, _ in rows]
    assert rescaled == sorted(rescaled)
    assert all(0.0 <= value <= 1.0 for value in rescaled)


def test_uneven_sample_counts_still_produce_ten_bins() -> None:
    rows = tuple((index / 25, index % 2 == 0) for index in range(25))
    bins = reliability_bins(_confidences(rows, FieldFamily.DATE, SplitName.TEST), FieldFamily.DATE)
    assert len(bins) == BIN_COUNT
    assert sum(one_bin.count for one_bin in bins) == 25


def test_fewer_samples_than_bins_reports_only_the_bins_that_exist() -> None:
    rows = ((0.2, False), (0.8, True), (0.9, True))
    bins = reliability_bins(_confidences(rows, FieldFamily.TEXT, SplitName.TEST), FieldFamily.TEXT)
    assert [one_bin.count for one_bin in bins] == [1, 1, 1]


def test_an_absent_family_has_no_bins_and_no_error() -> None:
    samples = _confidences(((0.5, True),), FieldFamily.TEXT, SplitName.TEST)
    bins = reliability_bins(samples, FieldFamily.REFERENCE)
    assert bins == ()
    assert expected_calibration_error(bins) == pytest.approx(0.0)
