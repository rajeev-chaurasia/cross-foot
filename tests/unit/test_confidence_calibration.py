"""Split guards, threshold fallbacks, and binning edges in the calibration module."""

import pytest

from crossfoot.confidence.calibration import (
    BIN_COUNT,
    MAX_EXPECTED_CALIBRATION_ERROR,
    ConfidenceSample,
    PlattScaler,
    SplitDisciplineError,
    TrainingSample,
    choose_thresholds,
    expected_calibration_error,
    fit_platt_scalers,
    fit_platt_scaling,
    fit_scorers,
    platt_cells,
    reliability_bins,
    rescale,
)
from crossfoot.constants import ExtractionRoute, FieldFamily, SplitName
from crossfoot.models.extraction import FieldSignals

HIGH = FieldSignals(route=ExtractionRoute.DIGITAL_PDF, validator_pass=1.0)
LOW = FieldSignals(route=ExtractionRoute.DIGITAL_PDF, validator_pass=0.0)

# States far more confidence than it earns at every level: 0.99 confidence on
# fields it reads right 70 percent of the time, and so on down. Right and wrong
# alternate within a level rather than running in blocks, because equal-count
# bins split a run of tied confidences wherever the count falls and a block
# would put every wrong reading in one bin.
OVERCONFIDENT = tuple(
    (confidence, index % 10 < correct_in_ten)
    for confidence, correct_in_ten in ((0.99, 7), (0.95, 6), (0.90, 5))
    for index in range(100)
)


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


@pytest.mark.parametrize("split", [SplitName.TRAIN, SplitName.TEST])
def test_platt_scalers_refuse_every_split_but_calibration(split: SplitName) -> None:
    with pytest.raises(SplitDisciplineError):
        fit_platt_scalers(_confidences(((0.9, True), (0.1, False)), split=split), split=split)


def test_platt_scaling_stays_monotone_in_confidence() -> None:
    rows = tuple((index / 10, index >= 5) for index in range(10))
    scaler = fit_platt_scaling(_confidences(rows), split=SplitName.CALIBRATION)
    assert isinstance(scaler, PlattScaler)
    rescaled = [scaler.apply(confidence) for confidence, _ in rows]
    assert rescaled == sorted(rescaled)
    assert all(0.0 <= value <= 1.0 for value in rescaled)


def test_rescaling_preserves_the_order_a_threshold_reads() -> None:
    # The property the workload claim rests on: a monotone transform moves the
    # numbers without moving the ranking, so the same fields sit above the same
    # quantile before and after and the review queue is the same queue.
    samples = _confidences(OVERCONFIDENT)
    scaler = fit_platt_scaling(samples, split=SplitName.CALIBRATION)
    scaled = rescale(samples, {FieldFamily.AMOUNT: scaler})
    ranked = sorted(
        zip((s.confidence for s in samples), (s.confidence for s in scaled), strict=True)
    )
    assert [after for _, after in ranked] == sorted(after for _, after in ranked)


def test_platt_scaling_lowers_error_on_an_overconfident_scorer() -> None:
    samples = _confidences(OVERCONFIDENT)
    before = expected_calibration_error(reliability_bins(samples, FieldFamily.AMOUNT))
    scaler = fit_platt_scaling(samples, split=SplitName.CALIBRATION)
    scaled = rescale(samples, {FieldFamily.AMOUNT: scaler})
    after = expected_calibration_error(reliability_bins(scaled, FieldFamily.AMOUNT))
    assert before > MAX_EXPECTED_CALIBRATION_ERROR
    assert after < MAX_EXPECTED_CALIBRATION_ERROR


def test_a_family_with_no_scaler_is_left_exactly_as_it_was() -> None:
    samples = _confidences(((0.9, True), (0.1, False)))
    assert rescale(samples, {}) == tuple(samples)


def test_the_identity_scaler_returns_the_confidence_it_was_given() -> None:
    # Slope one over the scorer's own logit is a no-op, which is what lets an
    # already calibrated family be fit without being knocked off its mark.
    scaler = PlattScaler(slope=1.0, intercept=0.0)
    for confidence in (0.01, 0.25, 0.5, 0.75, 0.99):
        assert scaler.apply(confidence) == pytest.approx(confidence)


def test_scalers_come_back_one_per_family() -> None:
    samples = [
        *_confidences(((0.9, True), (0.1, False)), FieldFamily.AMOUNT),
        *_confidences(((0.9, True), (0.1, False)), FieldFamily.REFERENCE),
    ]
    scalers = fit_platt_scalers(samples, split=SplitName.CALIBRATION)
    assert set(scalers) == {FieldFamily.AMOUNT, FieldFamily.REFERENCE}


def test_the_published_cells_carry_the_scalers_that_were_fit() -> None:
    # Handed text first, so the cells prove they publish in family order rather
    # than in whichever order the rows happened to arrive.
    samples = [
        *_confidences(OVERCONFIDENT, FieldFamily.TEXT),
        *_confidences(OVERCONFIDENT, FieldFamily.AMOUNT),
    ]
    scalers = fit_platt_scalers(samples, split=SplitName.CALIBRATION)
    cells = platt_cells(scalers)
    assert [cell.field_family for cell in cells] == [FieldFamily.AMOUNT, FieldFamily.TEXT]
    for cell in cells:
        scaler = scalers[cell.field_family]
        assert (cell.slope, cell.intercept) == (scaler.slope, scaler.intercept)


def test_a_cell_reproduces_the_rescaling_it_reports() -> None:
    # What reproducibility from the scorecard alone means: the two numbers on the
    # page rebuild the scaler that moved the confidences.
    samples = _confidences(OVERCONFIDENT)
    scalers = fit_platt_scalers(samples, split=SplitName.CALIBRATION)
    cell = platt_cells(scalers)[0]
    rebuilt = PlattScaler(slope=cell.slope, intercept=cell.intercept)
    scaled = rescale(samples, scalers)
    assert [sample.confidence for sample in scaled] == pytest.approx(
        [rebuilt.apply(sample.confidence) for sample in samples]
    )


def test_no_scalers_publish_no_cells() -> None:
    assert platt_cells({}) == ()


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
