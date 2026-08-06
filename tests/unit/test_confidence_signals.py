"""Edge cases for crossfoot.confidence.signals beyond the contract fixtures."""

from collections.abc import Mapping
from datetime import date

import pytest

from crossfoot.confidence.signals import (
    SignalContext,
    amount_sign_consistent,
    attach_signals,
    crossfoot_delta_cents,
)
from crossfoot.constants import (
    FIELD_FAMILIES,
    DocType,
    ExtractionRoute,
    FieldName,
    FieldSource,
    LineType,
    Oem,
    QualityTier,
)
from crossfoot.models.extraction import ExtractedDocument, ExtractedField, FieldSignals

DOC_ID = "doc-parts_statement-dlr-meridian-202607-99"
PERIOD_START = date(2026, 7, 1)
PERIOD_END = date(2026, 7, 31)
VIN_GOOD = "1FTFW1ET9DFC10312"


def _field(
    name: FieldName,
    *,
    line_no: int | None = None,
    value: str | None = None,
    value_cents: int | None = None,
    value_date: date | None = None,
) -> ExtractedField:
    return ExtractedField(
        field_id=f"fld-{DOC_ID}-{line_no}-{name}",
        doc_id=DOC_ID,
        line_no=line_no,
        name=name,
        family=FIELD_FAMILIES[name],
        raw_text=value,
        value=value,
        value_cents=value_cents,
        value_date=value_date,
        source=FieldSource.LLM_VISION,
        signals=FieldSignals(quality_tier=QualityTier.CLEAN_DIGITAL),
    )


def _doc(
    header: tuple[ExtractedField, ...] = (),
    lines: tuple[ExtractedField, ...] = (),
) -> ExtractedDocument:
    return ExtractedDocument(
        doc_id=DOC_ID,
        file_path=f"files/{DOC_ID}.pdf",
        route=ExtractionRoute.DIGITAL_PDF,
        doc_type=DocType.PARTS_STATEMENT,
        header_fields=header,
        line_fields=lines,
    )


def _amounts(*cents: int) -> tuple[ExtractedField, ...]:
    return tuple(
        _field(FieldName.LINE_AMOUNT, line_no=index, value_cents=amount)
        for index, amount in enumerate(cents, start=1)
    )


def _context(
    *,
    line_types: Mapping[int, LineType] | None = None,
    self_consistency: Mapping[str, float] | None = None,
    det_llm_agreement: Mapping[str, float] | None = None,
) -> SignalContext:
    return SignalContext(
        oem=Oem.MERIDIAN,
        period_start=PERIOD_START,
        period_end=PERIOD_END,
        quality_tier=QualityTier.CLEAN_DIGITAL,
        self_consistency=self_consistency or {},
        det_llm_agreement=det_llm_agreement or {},
        line_types=line_types or {},
    )


def test_no_extracted_total_leaves_crossfoot_unavailable() -> None:
    doc = _doc(lines=_amounts(100, 200))
    assert crossfoot_delta_cents(doc) is None
    scored = attach_signals(doc, _context())
    assert scored.crossfoot_delta_cents is None
    for scored_field in scored.line_fields:
        assert scored_field.signals.crossfoot_ok is None
        assert scored_field.signals.crossfoot_residual_suspect is False


def test_previous_balance_carries_into_the_crossfoot() -> None:
    header = (
        _field(FieldName.TOTAL, value_cents=145_000),
        _field(FieldName.PREVIOUS_BALANCE, value_cents=45_000),
    )
    doc = _doc(header=header, lines=_amounts(100_000))
    assert crossfoot_delta_cents(doc) == 0
    scored = attach_signals(doc, _context())
    assert all(f.signals.crossfoot_ok == 1.0 for f in scored.line_fields)


def test_one_cent_of_rounding_still_foots() -> None:
    doc = _doc(header=(_field(FieldName.TOTAL, value_cents=100_001),), lines=_amounts(100_000))
    scored = attach_signals(doc, _context())
    assert scored.line_fields[0].signals.crossfoot_ok == 1.0


def test_two_plausible_residuals_flag_nothing() -> None:
    # Total 100 against lines 60 and 60: each line's residual is 40, positive and
    # plausible for both, so the error is not localized and neither is flagged.
    doc = _doc(header=(_field(FieldName.TOTAL, value_cents=100),), lines=_amounts(60, 60))
    scored = attach_signals(doc, _context())
    assert crossfoot_delta_cents(doc) == -20
    for scored_field in scored.line_fields:
        assert scored_field.signals.crossfoot_ok == 0.0
        assert scored_field.signals.crossfoot_residual_suspect is False


def test_vin_validator_reaches_the_field() -> None:
    good = _field(FieldName.VIN, line_no=1, value=VIN_GOOD)
    bad = _field(FieldName.VIN, line_no=2, value=VIN_GOOD[:-1] + "9")
    scored = attach_signals(_doc(lines=(good, bad)), _context())
    assert scored.line_fields[0].signals.validator_pass == 1.0
    assert scored.line_fields[1].signals.validator_pass == 0.0


def test_vin_carries_no_grammar_signal() -> None:
    # REF_GRAMMARS defines no VIN pattern, so the signal is absent rather than false.
    scored = attach_signals(
        _doc(lines=(_field(FieldName.VIN, line_no=1, value=VIN_GOOD),)), _context()
    )
    assert scored.line_fields[0].signals.grammar_match is None


def test_reference_grammar_signal_is_scored_against_the_marque() -> None:
    good = _field(FieldName.CLAIM_NUMBER, line_no=1, value="4821A00551")
    bad = _field(FieldName.CLAIM_NUMBER, line_no=2, value="NS12345678")
    scored = attach_signals(_doc(lines=(good, bad)), _context())
    assert scored.line_fields[0].signals.grammar_match == 1.0
    assert scored.line_fields[1].signals.grammar_match == 0.0


@pytest.mark.parametrize(
    ("value", "expected"),
    [(date(2026, 7, 15), 1.0), (date(2026, 9, 29), 1.0), (date(2026, 9, 30), 0.0)],
)
def test_date_validator_uses_the_grace_window(value: date, expected: float) -> None:
    line = _field(FieldName.LINE_DATE, line_no=1, value_date=value)
    scored = attach_signals(_doc(lines=(line,)), _context())
    assert scored.line_fields[0].signals.validator_pass == expected


def test_amount_sign_is_checked_only_when_the_line_type_is_known() -> None:
    line = _field(FieldName.LINE_AMOUNT, line_no=1, value_cents=-100)
    without = attach_signals(_doc(lines=(line,)), _context())
    with_type = attach_signals(_doc(lines=(line,)), _context(line_types={1: LineType.CHARGE}))
    assert without.line_fields[0].signals.validator_pass == 1.0
    assert with_type.line_fields[0].signals.validator_pass == 0.0


@pytest.mark.parametrize("line_type", [LineType.ADJUSTMENT, LineType.PAYMENT])
@pytest.mark.parametrize("cents", [-100, 100])
def test_unconstrained_line_types_accept_either_sign(cents: int, line_type: LineType) -> None:
    assert amount_sign_consistent(cents, line_type) is True


def test_upstream_agreement_maps_reach_the_signals() -> None:
    line = _field(FieldName.LINE_AMOUNT, line_no=1, value_cents=100)
    context = _context(
        self_consistency={line.field_id: 0.5}, det_llm_agreement={line.field_id: 0.25}
    )
    scored = attach_signals(_doc(lines=(line,)), context)
    assert scored.line_fields[0].signals.self_consistency == 0.5
    assert scored.line_fields[0].signals.det_llm_agreement == 0.25


def test_absent_agreement_stays_none() -> None:
    line = _field(FieldName.LINE_AMOUNT, line_no=1, value_cents=100)
    scored = attach_signals(_doc(lines=(line,)), _context())
    assert scored.line_fields[0].signals.self_consistency is None
    assert scored.line_fields[0].signals.det_llm_agreement is None
