"""Contract tests for crossfoot.confidence: signals, scorer, calibration.

Written against docs/contracts-phase2.md before the implementation exists, so the
module-level importorskip keeps collection clean today.

Phase 2 freezes the behaviour but names no signatures, so these tests pin the
smallest surface that can express it:

    signals.PERIOD_GRACE_DAYS
    signals.vin_check_digit_ok(vin) -> bool
    signals.grammar_matches(oem, name, value) -> bool
    signals.date_within_period(value, period_start, period_end) -> bool
    signals.amount_sign_consistent(amount_cents, line_type) -> bool
    signals.char_ambiguity(text) -> float
    signals.SignalContext(self_consistency, det_llm_agreement)
    signals.attach_signals(doc, context) -> ExtractedDocument

    scorer.encode(field_signals) -> tuple[float, ...]
    scorer.fit(family, samples) -> model, model.predict(field_signals) -> float

    calibration.PRECISION_TARGETS, calibration.BIN_COUNT
    calibration.SplitDisciplineError
    calibration.TrainingSample(field_family, signals, correct, split)
    calibration.ConfidenceSample(field_family, confidence, correct, split)
    calibration.fit_scorers(samples, split) -> mapping
    calibration.choose_thresholds(samples, split) -> tuple[ThresholdPoint, ...]
    calibration.reliability_bins(samples, field_family) -> tuple[CalibrationBin, ...]
    calibration.expected_calibration_error(bins) -> float

Every expected number is worked out by hand in the comment tables.

One signature moved after phase 2 froze it. `SignalContext` used to carry the
true marque, the true statement period, and the generator's quality tier, and
`attach_signals` copied that tier onto every field, where the scorer one-hot
encoded it as a feature. An adversarial audit measured what that was worth: the
published 16.02% review rate was partly bought with a degradation label no real
document carries. The signals now come off the artifact and the extraction, so
the context holds only the upstream measurements a field's own row cannot, and
this file pins the honest shape rather than the one that was refuted.
"""

from datetime import date
from typing import Any

import pytest

from crossfoot.constants import (
    FIELD_FAMILIES,
    DocType,
    ExtractionRoute,
    FieldFamily,
    FieldName,
    FieldSource,
    LineType,
    Oem,
    SplitName,
)
from crossfoot.models.extraction import ExtractedDocument, ExtractedField, FieldSignals
from crossfoot.models.scorecard import CalibrationBin, ThresholdPoint

signals = pytest.importorskip("crossfoot.confidence.signals")
scorer = pytest.importorskip("crossfoot.confidence.scorer")
calibration = pytest.importorskip("crossfoot.confidence.calibration")

PERIOD_START = date(2026, 7, 1)
PERIOD_END = date(2026, 7, 31)

# ISO 3779 check digits computed against the tables in crossfoot.constants.
VIN_GOOD = "1FTFW1ET9DFC10312"
VIN_GOOD_2 = "1G1ZT53826F109149"
VIN_BAD_CHECK_DIGIT = "1FTFW1ET0DFC10312"  # same VIN with position 9 set to 0


def _field(
    name: FieldName,
    *,
    doc_id: str = "doc-parts_statement-dlr-meridian-202607-01",
    line_no: int | None = None,
    value: str | None = None,
    value_cents: int | None = None,
    value_date: date | None = None,
    raw_text: str | None = None,
) -> ExtractedField:
    return ExtractedField(
        field_id=f"fld-{doc_id}-{line_no}-{name}",
        doc_id=doc_id,
        line_no=line_no,
        name=name,
        family=FIELD_FAMILIES[name],
        raw_text=raw_text if raw_text is not None else value,
        value=value,
        value_cents=value_cents,
        value_date=value_date,
        source=FieldSource.LLM_VISION,
        signals=FieldSignals(),
    )


# ---------------------------------------------------------------------------
# Individual validator signals
# ---------------------------------------------------------------------------


def test_vin_check_digit_accepts_valid_vins() -> None:
    assert signals.vin_check_digit_ok(VIN_GOOD) is True
    assert signals.vin_check_digit_ok(VIN_GOOD_2) is True


def test_vin_check_digit_rejects_a_wrong_check_digit() -> None:
    # Position 9 must be 9 for this VIN; the fixture prints 0.
    assert signals.vin_check_digit_ok(VIN_BAD_CHECK_DIGIT) is False


def test_vin_check_digit_rejects_wrong_length_and_illegal_glyphs() -> None:
    assert signals.vin_check_digit_ok(VIN_GOOD[:-1]) is False
    # I, O, and Q are not ISO 3779 VIN characters at all.
    assert signals.vin_check_digit_ok("1FTFW1ETIDFC10312") is False


def test_grammar_match_hits_the_marque_grammar() -> None:
    assert signals.grammar_matches(Oem.MERIDIAN, FieldName.CLAIM_NUMBER, "4821A00551") is True
    assert signals.grammar_matches(Oem.MERIDIAN, FieldName.RO_NUMBER, "RO123456") is True
    assert signals.grammar_matches(Oem.KAIZEN, FieldName.CLAIM_NUMBER, "K123-456789") is True
    assert signals.grammar_matches(Oem.NORTHSTAR, FieldName.PROGRAM_CODE, "NS-AB123") is True


def test_grammar_match_misses_are_reported() -> None:
    # Meridian claim numbers are 4 digits, a letter, then 5 digits: one short.
    assert signals.grammar_matches(Oem.MERIDIAN, FieldName.CLAIM_NUMBER, "4821A0055") is False
    # The grammar fullmatches, so a separator that the grammar omits fails.
    assert signals.grammar_matches(Oem.MERIDIAN, FieldName.RO_NUMBER, "RO-123456") is False
    # Kaizen claim numbers keep their hyphen.
    assert signals.grammar_matches(Oem.KAIZEN, FieldName.CLAIM_NUMBER, "K123456789") is False
    assert signals.grammar_matches(Oem.NORTHSTAR, FieldName.PROGRAM_CODE, "NS-AB12") is False


def test_period_grace_is_sixty_days() -> None:
    assert signals.PERIOD_GRACE_DAYS == 60


def test_date_inside_the_statement_period_passes() -> None:
    assert signals.date_within_period(date(2026, 7, 15), PERIOD_START, PERIOD_END) is True
    assert signals.date_within_period(PERIOD_START, PERIOD_START, PERIOD_END) is True
    assert signals.date_within_period(PERIOD_END, PERIOD_START, PERIOD_END) is True


def test_date_inside_the_grace_window_passes() -> None:
    # 2026-07-31 plus 60 days is 2026-09-29; 2026-07-01 minus 60 days is 2026-05-02.
    assert signals.date_within_period(date(2026, 9, 29), PERIOD_START, PERIOD_END) is True
    assert signals.date_within_period(date(2026, 5, 2), PERIOD_START, PERIOD_END) is True


def test_date_outside_the_grace_window_fails() -> None:
    assert signals.date_within_period(date(2026, 9, 30), PERIOD_START, PERIOD_END) is False
    assert signals.date_within_period(date(2026, 5, 1), PERIOD_START, PERIOD_END) is False
    assert signals.date_within_period(date(2026, 12, 1), PERIOD_START, PERIOD_END) is False


def test_amount_sign_matches_the_line_type() -> None:
    assert signals.amount_sign_consistent(100_000, LineType.CHARGE) is True
    assert signals.amount_sign_consistent(-60_000, LineType.CREDIT) is True


def test_amount_sign_against_the_line_type_fails() -> None:
    assert signals.amount_sign_consistent(-100_000, LineType.CHARGE) is False
    assert signals.amount_sign_consistent(60_000, LineType.CREDIT) is False


# ---------------------------------------------------------------------------
# char_ambiguity: only the frozen confusable classes {O0, I1l, S5, B8, Z2}
# ---------------------------------------------------------------------------


def test_char_ambiguity_counts_every_confusable_class() -> None:
    # One representative from each of the five classes, 5 of 5 characters.
    assert signals.char_ambiguity("1O5B2") == pytest.approx(1.0)
    assert signals.char_ambiguity("0I8SZ") == pytest.approx(1.0)


def test_char_ambiguity_counts_only_the_listed_glyphs() -> None:
    # G and Q look alike in print but are not in the frozen classes.
    assert signals.char_ambiguity("GQ") == pytest.approx(0.0)
    # Lowercase l is in {I1l}; lowercase o is not in {O0}.
    assert signals.char_ambiguity("lo") == pytest.approx(0.5)


def test_char_ambiguity_is_a_fraction_of_the_raw_text() -> None:
    # X, Y are not confusable and Z is: 1 of 3.
    assert signals.char_ambiguity("XYZ") == pytest.approx(1 / 3)
    # MERIDIAN: M E R I D I A N, the two I glyphs count, so 2 of 8.
    assert signals.char_ambiguity("MERIDIAN") == pytest.approx(0.25)


def test_char_ambiguity_of_empty_text_is_zero() -> None:
    assert signals.char_ambiguity("") == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Crossfoot and the residual suspect, worked by hand
# ---------------------------------------------------------------------------

# Truth prints 100000 + 25000 + 20000 = 145000 and a total of 145000.
# The extractor reads line 1 as 200000, so the extracted sum is 245000 and the
# crossfoot delta is 145000 - 245000 = -100000: the document does not foot.
#
# Residual per line, total minus the sum of every other extracted line:
#   line 1: 145000 - (25000 + 20000)   =  100000  positive, a plausible charge
#   line 2: 145000 - (200000 + 20000)  =  -75000  not a plausible charge amount
#   line 3: 145000 - (200000 + 25000)  =  -80000  not a plausible charge amount
# Exactly one line has a plausible residual, so exactly line 1 is flagged.
BROKEN_DOC = ExtractedDocument(
    doc_id="doc-parts_statement-dlr-meridian-202607-03",
    file_path="files/doc-parts_statement-dlr-meridian-202607-03.pdf",
    route=ExtractionRoute.DIGITAL_PDF,
    doc_type=DocType.PARTS_STATEMENT,
    doc_type_confidence=1.0,
    header_fields=(
        _field(
            FieldName.TOTAL,
            doc_id="doc-parts_statement-dlr-meridian-202607-03",
            value_cents=145_000,
            raw_text="$1,450.00",
        ),
    ),
    line_fields=(
        _field(
            FieldName.LINE_AMOUNT,
            doc_id="doc-parts_statement-dlr-meridian-202607-03",
            line_no=1,
            value_cents=200_000,
            raw_text="$2,000.00",
        ),
        _field(
            FieldName.LINE_AMOUNT,
            doc_id="doc-parts_statement-dlr-meridian-202607-03",
            line_no=2,
            value_cents=25_000,
            raw_text="$250.00",
        ),
        _field(
            FieldName.LINE_AMOUNT,
            doc_id="doc-parts_statement-dlr-meridian-202607-03",
            line_no=3,
            value_cents=20_000,
            raw_text="$200.00",
        ),
    ),
    crossfoot_delta_cents=-100_000,
)

# The same document read correctly: 100000 + 25000 + 20000 = 145000, delta 0.
GOOD_DOC = BROKEN_DOC.model_copy(
    update={
        "line_fields": (
            BROKEN_DOC.line_fields[0].model_copy(
                update={"value_cents": 100_000, "raw_text": "$1,000.00"}
            ),
            BROKEN_DOC.line_fields[1],
            BROKEN_DOC.line_fields[2],
        ),
        "crossfoot_delta_cents": 0,
    }
)

# Nothing from the manifest survives here. The context holds only what the
# extractor measured while reading, and both maps are empty for these fixtures.
CONTEXT_KWARGS: dict[str, Any] = {}


def _attach(doc: ExtractedDocument) -> ExtractedDocument:
    context = signals.SignalContext(**CONTEXT_KWARGS)
    result = signals.attach_signals(doc, context)
    assert isinstance(result, ExtractedDocument)
    return result


def _by_line(doc: ExtractedDocument, line_no: int) -> ExtractedField:
    matches = [f for f in doc.line_fields if f.line_no == line_no]
    assert len(matches) == 1
    return matches[0]


def test_broken_crossfoot_is_zero_for_every_amount_field() -> None:
    scored = _attach(BROKEN_DOC)
    for field in (*scored.header_fields, *scored.line_fields):
        assert FIELD_FAMILIES[field.name] is FieldFamily.AMOUNT
        assert field.signals.crossfoot_ok == 0.0, field.field_id


def test_crossfoot_residual_flags_exactly_the_offending_line() -> None:
    scored = _attach(BROKEN_DOC)
    assert _by_line(scored, 1).signals.crossfoot_residual_suspect is True
    assert _by_line(scored, 2).signals.crossfoot_residual_suspect is False
    assert _by_line(scored, 3).signals.crossfoot_residual_suspect is False


def test_header_total_is_never_a_residual_suspect() -> None:
    scored = _attach(BROKEN_DOC)
    assert scored.header_fields[0].signals.crossfoot_residual_suspect is False


def test_document_that_foots_gets_crossfoot_ok_and_no_suspects() -> None:
    scored = _attach(GOOD_DOC)
    for field in (*scored.header_fields, *scored.line_fields):
        assert field.signals.crossfoot_ok == 1.0, field.field_id
        assert field.signals.crossfoot_residual_suspect is False, field.field_id


def test_attach_signals_records_the_route_the_router_chose() -> None:
    # The route replaced the quality tier. It says the same useful thing about a
    # page (how it had to be read) and, unlike a degradation label, a real
    # document states it: this one carries a text layer, so it routed digital.
    scored = _attach(GOOD_DOC)
    for field in (*scored.header_fields, *scored.line_fields):
        assert field.signals.route is ExtractionRoute.DIGITAL_PDF, field.field_id


# ---------------------------------------------------------------------------
# scorer: logistic regression with learnable absence
# ---------------------------------------------------------------------------


def _sig(
    *,
    self_consistency: float | None = None,
    validator_pass: float | None = None,
    grammar_match: float | None = None,
    char_ambiguity: float = 0.0,
) -> FieldSignals:
    return FieldSignals(
        self_consistency=self_consistency,
        validator_pass=validator_pass,
        grammar_match=grammar_match,
        char_ambiguity=char_ambiguity,
    )


ALL_HIGH = _sig(self_consistency=1.0, validator_pass=1.0, grammar_match=1.0)
ALL_MID = _sig(self_consistency=0.5, validator_pass=0.5, grammar_match=0.5)
ALL_LOW = _sig(self_consistency=0.0, validator_pass=0.0, grammar_match=0.0)


def test_missing_signal_encodes_as_an_indicator_zero_pair() -> None:
    present_zero = _sig(self_consistency=0.0, validator_pass=1.0, grammar_match=1.0)
    absent = _sig(self_consistency=None, validator_pass=1.0, grammar_match=1.0)
    encoded_present = tuple(scorer.encode(present_zero))
    encoded_absent = tuple(scorer.encode(absent))
    assert len(encoded_present) == len(encoded_absent)
    assert encoded_present != encoded_absent
    # The only difference is the presence indicator, so the encodings differ by
    # exactly 1.0 in total: the value slot is 0.0 either way.
    assert abs(sum(encoded_present) - sum(encoded_absent)) == pytest.approx(1.0)


def _fit_separable() -> Any:
    samples = [(ALL_HIGH, True)] * 8 + [(ALL_LOW, False)] * 8
    return scorer.fit(FieldFamily.REFERENCE, samples)


def test_separable_dataset_orders_confidences() -> None:
    model = _fit_separable()
    high = float(model.predict(ALL_HIGH))
    mid = float(model.predict(ALL_MID))
    low = float(model.predict(ALL_LOW))
    assert high > mid > low
    assert high > 0.5
    assert low < 0.5


def test_predictions_are_probabilities() -> None:
    model = _fit_separable()
    for sig in (ALL_HIGH, ALL_MID, ALL_LOW):
        value = float(model.predict(sig))
        assert 0.0 <= value <= 1.0


def test_fit_is_deterministic() -> None:
    first = _fit_separable()
    second = _fit_separable()
    for sig in (ALL_HIGH, ALL_MID, ALL_LOW):
        assert float(first.predict(sig)) == float(second.predict(sig))


def test_absent_signal_is_learnable_against_a_present_zero() -> None:
    # Identical fields except that grammar_match is present-and-zero in the
    # correct group and absent in the incorrect group. A model that collapsed
    # missing to 0.0 could not tell these apart and would score them the same.
    present_zero = _sig(self_consistency=1.0, validator_pass=1.0, grammar_match=0.0)
    absent = _sig(self_consistency=1.0, validator_pass=1.0, grammar_match=None)
    samples = [(present_zero, True)] * 8 + [(absent, False)] * 8
    model = scorer.fit(FieldFamily.REFERENCE, samples)
    assert float(model.predict(present_zero)) > float(model.predict(absent))
    assert float(model.predict(present_zero)) > 0.5
    assert float(model.predict(absent)) < 0.5


# ---------------------------------------------------------------------------
# calibration: split discipline, thresholds, reliability
# ---------------------------------------------------------------------------


def test_precision_targets_are_named_constants() -> None:
    assert calibration.PRECISION_TARGETS[FieldFamily.AMOUNT] == 0.995
    assert calibration.PRECISION_TARGETS[FieldFamily.REFERENCE] == 0.995
    assert calibration.PRECISION_TARGETS[FieldFamily.DATE] == 0.99
    assert calibration.PRECISION_TARGETS[FieldFamily.TEXT] == 0.97


def _training_samples(split: SplitName) -> list[Any]:
    return [
        calibration.TrainingSample(
            field_family=FieldFamily.REFERENCE,
            signals=ALL_HIGH if correct else ALL_LOW,
            correct=correct,
            split=split,
        )
        for correct in [True] * 8 + [False] * 8
    ]


def test_fit_accepts_train_data() -> None:
    calibration.fit_scorers(_training_samples(SplitName.TRAIN), split=SplitName.TRAIN)


def test_fit_refuses_test_data() -> None:
    with pytest.raises(calibration.SplitDisciplineError):
        calibration.fit_scorers(_training_samples(SplitName.TEST), split=SplitName.TEST)


def test_fit_refuses_calibration_data() -> None:
    with pytest.raises(calibration.SplitDisciplineError):
        calibration.fit_scorers(
            _training_samples(SplitName.CALIBRATION), split=SplitName.CALIBRATION
        )


def test_fit_refuses_test_rows_smuggled_into_a_train_call() -> None:
    smuggled = [*_training_samples(SplitName.TRAIN), *_training_samples(SplitName.TEST)]
    with pytest.raises(calibration.SplitDisciplineError):
        calibration.fit_scorers(smuggled, split=SplitName.TRAIN)


# Threshold sweep for the amount family, target 0.995. Accept when
# confidence >= threshold; review rate is the fraction below it.
#
#   conf   correct
#   0.99   yes        t=0.99  accept 1  correct 1  precision 1.000  review 0.9
#   0.95   yes        t=0.95  accept 2  correct 2  precision 1.000  review 0.8
#   0.90   yes        t=0.90  accept 3  correct 3  precision 1.000  review 0.7  <- pick
#   0.85   no         t=0.85  accept 4  correct 3  precision 0.750  fails
#   0.80   yes        t=0.80  accept 5  correct 4  precision 0.800  fails
#   0.70   yes        t=0.70  accept 6  correct 5  precision 0.833  fails
#   0.60   no         t=0.60  accept 7  correct 5  precision 0.714  fails
#   0.50   yes        t=0.50  accept 8  correct 6  precision 0.750  fails
#   0.30   no         t=0.30  accept 9  correct 6  precision 0.667  fails
#   0.10   no         t=0.10  accept 10 correct 6  precision 0.600  fails
#
# Only 0.99, 0.95, and 0.90 clear 0.995, and 0.90 has the lowest review rate.
THRESHOLD_TABLE: tuple[tuple[float, bool], ...] = (
    (0.99, True),
    (0.95, True),
    (0.90, True),
    (0.85, False),
    (0.80, True),
    (0.70, True),
    (0.60, False),
    (0.50, True),
    (0.30, False),
    (0.10, False),
)


def _confidence_samples(
    table: tuple[tuple[float, bool], ...],
    family: FieldFamily,
    split: SplitName,
) -> list[Any]:
    return [
        calibration.ConfidenceSample(
            field_family=family, confidence=confidence, correct=correct, split=split
        )
        for confidence, correct in table
    ]


def _threshold_for(points: Any, family: FieldFamily) -> ThresholdPoint:
    matches = [point for point in points if point.field_family is family]
    assert len(matches) == 1, f"expected one threshold point for {family}"
    point = matches[0]
    assert isinstance(point, ThresholdPoint)
    return point


def test_threshold_picks_the_lowest_review_rate_meeting_the_target() -> None:
    samples = _confidence_samples(THRESHOLD_TABLE, FieldFamily.AMOUNT, SplitName.CALIBRATION)
    points = calibration.choose_thresholds(samples, split=SplitName.CALIBRATION)
    point = _threshold_for(points, FieldFamily.AMOUNT)
    assert point.threshold == pytest.approx(0.90)
    assert point.auto_accept_precision == pytest.approx(1.0)
    assert point.review_rate == pytest.approx(0.7)


def test_choose_thresholds_refuses_the_test_split() -> None:
    samples = _confidence_samples(THRESHOLD_TABLE, FieldFamily.AMOUNT, SplitName.TEST)
    with pytest.raises(calibration.SplitDisciplineError):
        calibration.choose_thresholds(samples, split=SplitName.TEST)


def test_choose_thresholds_refuses_the_train_split() -> None:
    samples = _confidence_samples(THRESHOLD_TABLE, FieldFamily.AMOUNT, SplitName.TRAIN)
    with pytest.raises(calibration.SplitDisciplineError):
        calibration.choose_thresholds(samples, split=SplitName.TRAIN)


def test_choose_thresholds_refuses_test_rows_smuggled_into_a_calibration_call() -> None:
    smuggled = [
        *_confidence_samples(THRESHOLD_TABLE, FieldFamily.AMOUNT, SplitName.CALIBRATION),
        *_confidence_samples(THRESHOLD_TABLE, FieldFamily.AMOUNT, SplitName.TEST),
    ]
    with pytest.raises(calibration.SplitDisciplineError):
        calibration.choose_thresholds(smuggled, split=SplitName.CALIBRATION)


# Reliability: 20 date-family samples, 10 equal-count bins of 2.
#
#   bin  confidences  correct  mean_conf  accuracy  gap
#   1    0.05 0.05    0 of 2   0.05       0.0       0.05
#   2    0.15 0.15    0 of 2   0.15       0.0       0.15
#   3    0.25 0.25    0 of 2   0.25       0.0       0.25
#   4    0.35 0.35    1 of 2   0.35       0.5       0.15
#   5    0.45 0.45    1 of 2   0.45       0.5       0.05
#   6    0.55 0.55    1 of 2   0.55       0.5       0.05
#   7    0.65 0.65    2 of 2   0.65       1.0       0.35
#   8    0.75 0.75    2 of 2   0.75       1.0       0.25
#   9    0.85 0.85    2 of 2   0.85       1.0       0.15
#   10   0.95 0.95    2 of 2   0.95       1.0       0.05
#
# Bins are equal count, so every weight is 1/10 and
# ECE = (0.05+0.15+0.25+0.15+0.05+0.05+0.35+0.25+0.15+0.05)/10 = 1.50/10 = 0.15.
RELIABILITY_TABLE: tuple[tuple[float, bool], ...] = (
    (0.05, False),
    (0.05, False),
    (0.15, False),
    (0.15, False),
    (0.25, False),
    (0.25, False),
    (0.35, False),
    (0.35, True),
    (0.45, False),
    (0.45, True),
    (0.55, True),
    (0.55, False),
    (0.65, True),
    (0.65, True),
    (0.75, True),
    (0.75, True),
    (0.85, True),
    (0.85, True),
    (0.95, True),
    (0.95, True),
)

EXPECTED_BINS: tuple[tuple[float, float], ...] = (
    (0.05, 0.0),
    (0.15, 0.0),
    (0.25, 0.0),
    (0.35, 0.5),
    (0.45, 0.5),
    (0.55, 0.5),
    (0.65, 1.0),
    (0.75, 1.0),
    (0.85, 1.0),
    (0.95, 1.0),
)


def _reliability_input() -> list[Any]:
    # Deliberately unsorted, and salted with another family that must be ignored.
    shuffled = tuple(RELIABILITY_TABLE[1::2]) + tuple(RELIABILITY_TABLE[0::2])
    return [
        *_confidence_samples(shuffled, FieldFamily.DATE, SplitName.TEST),
        *_confidence_samples(
            ((0.5, True), (0.5, False), (0.9, True), (0.9, False)),
            FieldFamily.TEXT,
            SplitName.TEST,
        ),
    ]


def test_bin_count_is_ten() -> None:
    assert calibration.BIN_COUNT == 10


def test_reliability_uses_ten_equal_count_bins_for_its_own_family() -> None:
    bins = calibration.reliability_bins(_reliability_input(), FieldFamily.DATE)
    assert len(bins) == 10
    for one_bin in bins:
        assert isinstance(one_bin, CalibrationBin)
        assert one_bin.field_family is FieldFamily.DATE
        assert one_bin.count == 2


def test_reliability_bins_match_the_hand_computed_table() -> None:
    bins = calibration.reliability_bins(_reliability_input(), FieldFamily.DATE)
    for one_bin, (mean_confidence, accuracy) in zip(bins, EXPECTED_BINS, strict=True):
        assert one_bin.mean_confidence == pytest.approx(mean_confidence)
        assert one_bin.empirical_accuracy == pytest.approx(accuracy)


def test_expected_calibration_error_is_the_weighted_mean_absolute_gap() -> None:
    bins = calibration.reliability_bins(_reliability_input(), FieldFamily.DATE)
    assert calibration.expected_calibration_error(bins) == pytest.approx(0.15)


def test_expected_calibration_error_weights_bins_by_count() -> None:
    # Two bins, gaps 0.4 and 0.0, counts 3 and 1:
    # (3 * 0.4 + 1 * 0.0) / 4 = 1.2 / 4 = 0.3.
    weighted = (
        CalibrationBin(
            field_family=FieldFamily.DATE,
            mean_confidence=0.9,
            empirical_accuracy=0.5,
            count=3,
        ),
        CalibrationBin(
            field_family=FieldFamily.DATE,
            mean_confidence=0.5,
            empirical_accuracy=0.5,
            count=1,
        ),
    )
    assert calibration.expected_calibration_error(weighted) == pytest.approx(0.3)
