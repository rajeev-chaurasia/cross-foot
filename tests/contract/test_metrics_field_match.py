"""Contract tests for the typed field comparison rules in crossfoot.evals.metrics.

Written against docs/contracts-phase1.md before the implementation exists.
Both truth docs below satisfy the composer invariants: subtotal equals the
sum of line amounts and crossfoot_delta_cents() is zero.
"""

from datetime import date

import pytest

from crossfoot.constants import (
    FIELD_FAMILIES,
    DocType,
    ExtractionRoute,
    FieldName,
    FieldSource,
    LineType,
    Oem,
    ReviewStatus,
)
from crossfoot.models.extraction import ExtractedField, FieldSignals
from crossfoot.models.statement import StatementDoc, StatementLine

metrics = pytest.importorskip("crossfoot.evals.metrics")

# Lines: 123456 + (-12345) = 111111 = subtotal.
# Total: 50000 previous + 111111 lines + 0 adjustments = 161111, so the delta is 0.
TRUTH = StatementDoc(
    doc_id="doc-warranty_credit_memo-dlr-meridian-202607-01",
    dealer_id="dlr-meridian",
    doc_type=DocType.WARRANTY_CREDIT_MEMO,
    oem=Oem.MERIDIAN,
    statement_number="WCM-4471",
    statement_date=date(2026, 7, 31),
    period_start=date(2026, 7, 1),
    period_end=date(2026, 7, 31),
    previous_balance_cents=50_000,
    subtotal_cents=111_111,
    adjustments_cents=0,
    total_cents=161_111,
    lines=(
        StatementLine(
            line_no=1,
            line_type=LineType.CHARGE,
            claim_number="4821A00551",
            ro_number="RO123",
            line_date=date(2026, 7, 15),
            description="warranty repair",
            amount_cents=123_456,
        ),
        StatementLine(
            line_no=2,
            line_type=LineType.CREDIT,
            line_date=date(2026, 7, 20),
            description="Core return credit",
            amount_cents=-12_345,
        ),
    ),
)

# One line of 5000 = subtotal; no previous balance, so total = 0 + 5000 + 0.
TRUTH_NO_PREV = StatementDoc(
    doc_id="doc-incentive_statement-dlr-kaizen-202607-01",
    dealer_id="dlr-kaizen",
    doc_type=DocType.INCENTIVE_STATEMENT,
    oem=Oem.KAIZEN,
    statement_number="INC-2026-07",
    statement_date=date(2026, 7, 31),
    period_start=date(2026, 7, 1),
    period_end=date(2026, 7, 31),
    previous_balance_cents=None,
    subtotal_cents=5_000,
    total_cents=5_000,
    lines=(
        StatementLine(
            line_no=1,
            line_type=LineType.CREDIT,
            program_code="KZN0042",
            line_date=date(2026, 7, 5),
            description="Volume bonus",
            amount_cents=5_000,
        ),
    ),
)


def make_field(
    name: FieldName,
    *,
    line_no: int | None = None,
    value: str | None = None,
    value_cents: int | None = None,
    value_date: date | None = None,
    raw_text: str | None = None,
    doc_id: str = TRUTH.doc_id,
) -> ExtractedField:
    return ExtractedField(
        field_id=f"fld-{doc_id}-{line_no}-{name}",
        doc_id=doc_id,
        line_no=line_no,
        name=name,
        family=FIELD_FAMILIES[name],
        raw_text=raw_text,
        value=value,
        value_cents=value_cents,
        value_date=value_date,
        source=FieldSource.DETERMINISTIC,
        signals=FieldSignals(route=ExtractionRoute.CSV),
        confidence=1.0,
        status=ReviewStatus.AUTO_ACCEPTED,
    )


def test_truth_fixtures_satisfy_composer_invariants() -> None:
    for doc in (TRUTH, TRUTH_NO_PREV):
        assert doc.subtotal_cents == sum(line.amount_cents for line in doc.lines)
        assert doc.crossfoot_delta_cents() == 0


# AMOUNT family: exact integer cents, sign included.


def test_amount_exact_cents_match() -> None:
    field = make_field(FieldName.LINE_AMOUNT, line_no=1, value_cents=123_456)
    assert metrics.field_is_correct(field, TRUTH) is True


def test_amount_off_by_one_cent_is_wrong() -> None:
    field = make_field(FieldName.LINE_AMOUNT, line_no=1, value_cents=123_455)
    assert metrics.field_is_correct(field, TRUTH) is False


def test_amount_negative_matches_sign_exactly() -> None:
    field = make_field(FieldName.LINE_AMOUNT, line_no=2, value_cents=-12_345)
    assert metrics.field_is_correct(field, TRUTH) is True


def test_amount_sign_flip_is_wrong() -> None:
    field = make_field(FieldName.LINE_AMOUNT, line_no=2, value_cents=12_345)
    assert metrics.field_is_correct(field, TRUTH) is False


def test_header_total_matches_truth_cents() -> None:
    field = make_field(FieldName.TOTAL, value_cents=161_111)
    assert metrics.field_is_correct(field, TRUTH) is True


def test_header_previous_balance_matches_when_present() -> None:
    field = make_field(FieldName.PREVIOUS_BALANCE, value_cents=50_000)
    assert metrics.field_is_correct(field, TRUTH) is True


# DATE family: compares value_date to the truth date.


def test_date_match() -> None:
    field = make_field(FieldName.LINE_DATE, line_no=1, value_date=date(2026, 7, 15))
    assert metrics.field_is_correct(field, TRUTH) is True


def test_date_mismatch_is_wrong() -> None:
    field = make_field(FieldName.LINE_DATE, line_no=1, value_date=date(2026, 7, 16))
    assert metrics.field_is_correct(field, TRUTH) is False


def test_header_statement_date_match() -> None:
    field = make_field(FieldName.STATEMENT_DATE, value_date=date(2026, 7, 31))
    assert metrics.field_is_correct(field, TRUTH) is True


# REFERENCE family: uppercase, strip "-", " ", and leading zeros before comparing.


def test_reference_normalization_matches() -> None:
    # "ro-000123" -> "RO123" after uppercasing and stripping separators and zeros.
    field = make_field(FieldName.RO_NUMBER, line_no=1, value="ro-000123")
    assert metrics.field_is_correct(field, TRUTH) is True


def test_reference_different_number_is_wrong() -> None:
    field = make_field(FieldName.RO_NUMBER, line_no=1, value="RO124")
    assert metrics.field_is_correct(field, TRUTH) is False


# TEXT family: casefold and collapse whitespace before comparing.


def test_text_casefold_and_whitespace_collapse() -> None:
    field = make_field(FieldName.DESCRIPTION, line_no=1, value="Warranty  repair")
    assert metrics.field_is_correct(field, TRUTH) is True


def test_text_different_words_is_wrong() -> None:
    field = make_field(FieldName.DESCRIPTION, line_no=1, value="Warranty replacement")
    assert metrics.field_is_correct(field, TRUTH) is False


# None cases: the truth doc has nothing to compare against.


def test_none_when_truth_line_lacks_the_field() -> None:
    field = make_field(FieldName.VIN, line_no=1, value="1FTFW1ET9DFC10312")
    assert metrics.field_is_correct(field, TRUTH) is None


def test_none_for_header_field_the_doc_lacks() -> None:
    field = make_field(FieldName.PREVIOUS_BALANCE, value_cents=0, doc_id=TRUTH_NO_PREV.doc_id)
    assert metrics.field_is_correct(field, TRUTH_NO_PREV) is None


def test_none_for_line_no_beyond_the_lines() -> None:
    field = make_field(FieldName.LINE_AMOUNT, line_no=99, value_cents=123_456)
    assert metrics.field_is_correct(field, TRUTH) is None


# raw_is_correct: verbatim equality against the rendered string.


def test_raw_verbatim_match() -> None:
    field = make_field(FieldName.LINE_AMOUNT, line_no=1, value_cents=123_456, raw_text="$1,234.56")
    assert metrics.raw_is_correct(field, "$1,234.56") is True


def test_raw_near_miss_is_wrong() -> None:
    field = make_field(FieldName.LINE_AMOUNT, line_no=1, value_cents=123_456, raw_text="$1,234.56")
    assert metrics.raw_is_correct(field, "1,234.56") is False


def test_raw_none_when_rendered_absent() -> None:
    field = make_field(FieldName.LINE_AMOUNT, line_no=1, value_cents=123_456, raw_text="$1,234.56")
    assert metrics.raw_is_correct(field, None) is None
