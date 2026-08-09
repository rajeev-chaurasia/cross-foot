"""Edge cases for crossfoot.confidence.signals beyond the contract fixtures.

Every case here builds a document and asks what the signals say about it. That is
the whole point of the module after the leak was closed: nothing is passed in
beside the extraction, so a test that cannot express its setup as a document is a
test of something the pipeline could not compute in production either.
"""

from collections.abc import Mapping
from datetime import date

import pytest

from crossfoot.confidence.signals import (
    SignalContext,
    amount_sign_consistent,
    attach_signals,
    crossfoot_delta_cents,
    date_windows,
    infer_oem,
)
from crossfoot.constants import (
    FIELD_FAMILIES,
    DocType,
    ExtractionRoute,
    FieldName,
    FieldSource,
    LineType,
    Oem,
)
from crossfoot.models.extraction import ExtractedDocument, ExtractedField, FieldSignals

DOC_ID = "doc-parts_statement-dlr-meridian-202607-99"
STATEMENT_DATE = date(2026, 7, 31)
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
        signals=FieldSignals(),
    )


def _doc(
    header: tuple[ExtractedField, ...] = (),
    lines: tuple[ExtractedField, ...] = (),
    route: ExtractionRoute = ExtractionRoute.DIGITAL_PDF,
) -> ExtractedDocument:
    return ExtractedDocument(
        doc_id=DOC_ID,
        file_path=f"files/{DOC_ID}.pdf",
        route=route,
        doc_type=DocType.PARTS_STATEMENT,
        header_fields=header,
        line_fields=lines,
    )


def _amounts(*cents: int) -> tuple[ExtractedField, ...]:
    return tuple(
        _field(FieldName.LINE_AMOUNT, line_no=index, value_cents=amount)
        for index, amount in enumerate(cents, start=1)
    )


def _statement_date(value: date = STATEMENT_DATE) -> ExtractedField:
    return _field(FieldName.STATEMENT_DATE, value_date=value)


def _context(
    *,
    self_consistency: Mapping[str, float] | None = None,
    det_llm_agreement: Mapping[str, float] | None = None,
) -> SignalContext:
    return SignalContext(
        self_consistency=self_consistency or {},
        det_llm_agreement=det_llm_agreement or {},
    )


def _claim(line_no: int, value: str | None) -> ExtractedField:
    return _field(FieldName.CLAIM_NUMBER, line_no=line_no, value=value)


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


# ---------------------------------------------------------------------------
# Marque: elected by the extraction, never read from the manifest
# ---------------------------------------------------------------------------


def test_the_document_elects_the_marque_its_own_references_fit() -> None:
    # Three Meridian claim numbers against one Northstar-shaped one. Meridian
    # wins the vote, so the odd value is measured against Meridian and fails.
    lines = (
        _claim(1, "4821A00551"),
        _claim(2, "4821A00552"),
        _claim(3, "4821A00553"),
        _claim(4, "NS12345678"),
    )
    doc = _doc(lines=lines)
    assert infer_oem(doc) is Oem.MERIDIAN
    scored = attach_signals(doc, _context())
    assert [f.signals.grammar_match for f in scored.line_fields] == [1.0, 1.0, 1.0, 0.0]


def test_a_value_no_marque_recognizes_fails_the_grammar() -> None:
    lines = (_claim(1, "4821A00551"), _claim(2, "4821A0055"))  # second is a digit short
    scored = attach_signals(_doc(lines=lines), _context())
    assert scored.line_fields[0].signals.grammar_match == 1.0
    assert scored.line_fields[1].signals.grammar_match == 0.0


def test_a_tied_vote_elects_nobody_and_asks_whether_any_marque_would_do() -> None:
    # One Meridian claim and one Northstar claim: nothing to choose between them.
    # Both values are still real reference numbers somewhere, and saying so is
    # weaker than naming a marque, which is the honest answer here.
    lines = (_claim(1, "4821A00551"), _claim(2, "NS12345678"), _claim(3, "not-a-claim"))
    doc = _doc(lines=lines)
    assert infer_oem(doc) is None
    scored = attach_signals(doc, _context())
    assert [f.signals.grammar_match for f in scored.line_fields] == [1.0, 1.0, 0.0]


def test_a_missing_reference_value_fails_rather_than_going_absent() -> None:
    scored = attach_signals(_doc(lines=(_claim(1, None),)), _context())
    assert scored.line_fields[0].signals.grammar_match == 0.0


# ---------------------------------------------------------------------------
# Dates: the window comes from the dates this extraction produced
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [(date(2026, 7, 15), 1.0), (date(2026, 9, 29), 1.0), (date(2026, 9, 30), 0.0)],
)
def test_line_dates_are_judged_against_the_extracted_statement_date(
    value: date, expected: float
) -> None:
    # The statement date this same run read off the page anchors the window, and
    # 2026-07-31 plus the 60 grace days ends on 2026-09-29.
    line = _field(FieldName.LINE_DATE, line_no=1, value_date=value)
    scored = attach_signals(_doc(header=(_statement_date(),), lines=(line,)), _context())
    assert scored.line_fields[0].signals.validator_pass == expected


def test_the_statement_date_is_judged_against_the_other_dates() -> None:
    # It cannot vouch for itself, so the line dates are its window: a statement
    # date a year away from every line on the page is the misread it looks like.
    lines = tuple(
        _field(FieldName.LINE_DATE, line_no=index, value_date=date(2026, 7, day))
        for index, day in enumerate((10, 12, 14), start=1)
    )
    near = attach_signals(_doc(header=(_statement_date(),), lines=lines), _context())
    far = attach_signals(
        _doc(header=(_statement_date(date(2027, 7, 31)),), lines=lines), _context()
    )
    assert near.header_fields[0].signals.validator_pass == 1.0
    assert far.header_fields[0].signals.validator_pass == 0.0


def test_a_date_with_nothing_to_compare_against_has_no_window() -> None:
    # One date and nothing else: any window would be the field vouching for
    # itself, so the signal is absent, which the scorer encodes as absent.
    line = _field(FieldName.LINE_DATE, line_no=1, value_date=date(2026, 7, 15))
    scored = attach_signals(_doc(lines=(line,)), _context())
    assert scored.line_fields[0].signals.validator_pass is None


def test_an_unparsed_date_fails_whether_or_not_a_window_exists() -> None:
    line = _field(FieldName.LINE_DATE, line_no=1, value="31/13/2026")
    scored = attach_signals(_doc(lines=(line,)), _context())
    assert scored.line_fields[0].signals.validator_pass == 0.0


def test_a_document_with_no_statement_date_still_anchors_on_its_line_dates() -> None:
    lines = (
        _field(FieldName.LINE_DATE, line_no=1, value_date=date(2026, 7, 10)),
        _field(FieldName.LINE_DATE, line_no=2, value_date=date(2026, 7, 12)),
        _field(FieldName.LINE_DATE, line_no=3, value_date=date(2020, 1, 1)),
    )
    scored = attach_signals(_doc(lines=lines), _context())
    assert [f.signals.validator_pass for f in scored.line_fields] == [1.0, 1.0, 0.0]


def test_date_windows_are_empty_for_a_document_with_no_dates() -> None:
    assert date_windows(_doc(lines=_amounts(100))) == {}


# ---------------------------------------------------------------------------
# Amounts: line type is never extracted, so it gates nothing
# ---------------------------------------------------------------------------


def test_the_amount_validator_is_a_parse_and_nothing_more() -> None:
    # A negative amount used to fail whenever the manifest called its line a
    # charge. No extractor produces a line type, so a document cannot be scored
    # on one; the arithmetic that stands in for it is the crossfoot beside it.
    negative = _field(FieldName.LINE_AMOUNT, line_no=1, value_cents=-100)
    positive = _field(FieldName.LINE_AMOUNT, line_no=2, value_cents=100)
    unparsed = _field(FieldName.LINE_AMOUNT, line_no=3, value="$ ??")
    scored = attach_signals(_doc(lines=(negative, positive, unparsed)), _context())
    assert [f.signals.validator_pass for f in scored.line_fields] == [1.0, 1.0, 0.0]


@pytest.mark.parametrize("line_type", [LineType.ADJUSTMENT, LineType.PAYMENT])
@pytest.mark.parametrize("cents", [-100, 100])
def test_unconstrained_line_types_accept_either_sign(cents: int, line_type: LineType) -> None:
    assert amount_sign_consistent(cents, line_type) is True


# ---------------------------------------------------------------------------
# Route and upstream measurements
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "route", [ExtractionRoute.DIGITAL_PDF, ExtractionRoute.SCANNED_PDF, ExtractionRoute.CSV]
)
def test_every_field_records_the_route_the_router_chose(route: ExtractionRoute) -> None:
    doc = _doc(header=(_statement_date(),), lines=_amounts(100), route=route)
    scored = attach_signals(doc, _context())
    for scored_field in (*scored.header_fields, *scored.line_fields):
        assert scored_field.signals.route is route


def test_signals_attach_without_any_context_at_all() -> None:
    # Production has no context to pass; the default has to work.
    scored = attach_signals(_doc(header=(_statement_date(),), lines=_amounts(100)))
    assert scored.line_fields[0].signals.route is ExtractionRoute.DIGITAL_PDF


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


def test_a_measurement_the_extractor_made_survives_rescoring() -> None:
    # The vision extractor measures k=2 agreement while reading and nothing can
    # recompute it later, so rescoring must carry it rather than blank it.
    line = _field(FieldName.LINE_AMOUNT, line_no=1, value_cents=100)
    measured = line.model_copy(update={"signals": FieldSignals(self_consistency=1.0)})
    scored = attach_signals(_doc(lines=(measured,)), _context())
    assert scored.line_fields[0].signals.self_consistency == 1.0
