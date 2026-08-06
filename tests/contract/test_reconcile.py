"""Contract tests for the matching engine in crossfoot.reconcile.

Written against docs/contracts-phase2.md before the implementation exists, so the
module-level importorskip keeps collection clean today.

Phase 2 freezes the behaviour but names no signatures, so these tests pin the
smallest surface that can express it:

    engine.FUZZY_THRESHOLD, engine.REFERENCE_WEIGHT, engine.AMOUNT_WEIGHT,
    engine.DATE_WEIGHT, engine.DATE_DECAY_DAYS, engine.BLOCKING_GRACE_DAYS
    engine.PRIMARY_REFERENCES: dict[DocType, tuple[FieldName, ...]]
    engine.reconcile(doc, book, mode, run_id, now) -> result
        result.matches: tuple[MatchedLine, ...]
        result.exceptions: tuple[ExceptionRecord, ...]

The engine takes a StatementDoc so oracle mode and end to end mode run the same
code over the same shape: ground truth lines in one case, lines assembled from
extraction in the other.

Every ledger, statement, score, and dollar impact below is worked out by hand in
the comment tables. Every StatementDoc satisfies the composer invariants because
_doc derives the subtotal and total from its lines.
"""

from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime
from typing import Any

import pytest

from crossfoot.constants import (
    DOC_TYPE_SCHEDULES,
    DocType,
    ExceptionType,
    FieldName,
    LineType,
    Oem,
    ReconMode,
    ScheduleType,
)
from crossfoot.models.ledger import Dealer, LedgerBook, LedgerEntry
from crossfoot.models.reconciliation import ExceptionRecord, MatchedLine
from crossfoot.models.statement import StatementDoc, StatementLine

engine = pytest.importorskip("crossfoot.reconcile.engine")

DEALER = "dlr-meridian"
OTHER_DEALER = "dlr-kaizen"
PERIOD_START = date(2026, 7, 1)
PERIOD_END = date(2026, 7, 31)
STATEMENT_DATE = date(2026, 7, 31)
RUN_ID = "run-contract-0001"
NOW = datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC)

VIN_A = "JTDKN3DU8A1234567"
# Same last 8 ("A1234567"), two substitutions in the first nine characters, so
# Damerau-Levenshtein over the whole string is 2 and only the last-8 rule can fire.
VIN_A_GARBLED = "JTDXN8DU8A1234567"
VIN_B = "1G1ZT53826F109149"

DEALERS = (
    Dealer(dealer_id=DEALER, name="Meridian Motors of Ardmore", oem=Oem.MERIDIAN),
    Dealer(dealer_id=OTHER_DEALER, name="Kaizen of Bala Cynwyd", oem=Oem.KAIZEN),
)


def _line(
    line_no: int,
    *,
    amount_cents: int,
    line_date: date,
    description: str = "Contract fixture line",
    line_type: LineType = LineType.CHARGE,
    claim_number: str | None = None,
    ro_number: str | None = None,
    vin: str | None = None,
    invoice_number: str | None = None,
    program_code: str | None = None,
) -> StatementLine:
    return StatementLine(
        line_no=line_no,
        line_type=line_type,
        claim_number=claim_number,
        ro_number=ro_number,
        vin=vin,
        invoice_number=invoice_number,
        program_code=program_code,
        line_date=line_date,
        description=description,
        amount_cents=amount_cents,
    )


def _doc(
    doc_id: str,
    doc_type: DocType,
    lines: Sequence[StatementLine],
    *,
    dealer_id: str = DEALER,
    oem: Oem = Oem.MERIDIAN,
    statement_number: str = "PS-2026-07-001",
) -> StatementDoc:
    """Subtotal and total are derived, so crossfoot_delta_cents() is always zero."""
    subtotal = sum(line.amount_cents for line in lines)
    return StatementDoc(
        doc_id=doc_id,
        dealer_id=dealer_id,
        doc_type=doc_type,
        oem=oem,
        statement_number=statement_number,
        statement_date=STATEMENT_DATE,
        period_start=PERIOD_START,
        period_end=PERIOD_END,
        previous_balance_cents=None,
        subtotal_cents=subtotal,
        adjustments_cents=0,
        total_cents=subtotal,
        lines=tuple(lines),
    )


def _entry(
    entry_id: str,
    *,
    amount_cents: int,
    post_date: date,
    schedule: ScheduleType = ScheduleType.PARTS_PAYABLE,
    dealer_id: str = DEALER,
    claim_number: str | None = None,
    ro_number: str | None = None,
    vin: str | None = None,
    invoice_number: str | None = None,
    program_code: str | None = None,
) -> LedgerEntry:
    return LedgerEntry(
        entry_id=entry_id,
        dealer_id=dealer_id,
        schedule=schedule,
        gl_account="1400-00",
        claim_number=claim_number,
        ro_number=ro_number,
        vin=vin,
        invoice_number=invoice_number,
        program_code=program_code,
        post_date=post_date,
        amount_cents=amount_cents,
        description="Contract fixture entry",
        counterparty="Meridian Motor Company",
    )


def _book(*entries: LedgerEntry) -> LedgerBook:
    return LedgerBook(dealers=DEALERS, entries=entries)


Scenario = tuple[StatementDoc, LedgerBook]


def _run(scenario: Scenario, mode: ReconMode = ReconMode.END_TO_END) -> Any:
    doc, book = scenario
    return engine.reconcile(doc, book, mode=mode, run_id=RUN_ID, now=NOW)


def _matches(scenario: Scenario, mode: ReconMode = ReconMode.END_TO_END) -> list[MatchedLine]:
    matches = list(_run(scenario, mode).matches)
    for match in matches:
        assert isinstance(match, MatchedLine)
    return matches


def _exceptions(
    scenario: Scenario, mode: ReconMode = ReconMode.END_TO_END
) -> list[ExceptionRecord]:
    exceptions = list(_run(scenario, mode).exceptions)
    for exception in exceptions:
        assert isinstance(exception, ExceptionRecord)
        assert exception.run_id == RUN_ID
        assert exception.detected_at == NOW
    assert len({exception.exception_id for exception in exceptions}) == len(exceptions)
    return exceptions


def _only(exceptions: Sequence[ExceptionRecord]) -> ExceptionRecord:
    assert len(exceptions) == 1, [exception.exception_type for exception in exceptions]
    return exceptions[0]


def _summary(
    exceptions: Sequence[ExceptionRecord],
) -> set[tuple[ExceptionType, int | None, str | None, int, int]]:
    return {
        (
            exception.exception_type,
            exception.statement_line_no,
            exception.ledger_entry_id,
            exception.dollar_impact_cents,
            exception.memo_amount_cents,
        )
        for exception in exceptions
    }


# ---------------------------------------------------------------------------
# Frozen constants
# ---------------------------------------------------------------------------


def test_scoring_weights_and_threshold_are_named_constants() -> None:
    assert engine.REFERENCE_WEIGHT == 0.5
    assert engine.AMOUNT_WEIGHT == 0.35
    assert engine.DATE_WEIGHT == 0.15
    assert engine.FUZZY_THRESHOLD == 0.6
    assert engine.DATE_DECAY_DAYS == 45
    assert engine.BLOCKING_GRACE_DAYS == 60
    assert engine.REFERENCE_WEIGHT + engine.AMOUNT_WEIGHT + engine.DATE_WEIGHT == 1.0


def test_primary_reference_per_doc_type_matches_the_phase_one_clarification() -> None:
    assert engine.PRIMARY_REFERENCES[DocType.WARRANTY_CREDIT_MEMO] == (FieldName.CLAIM_NUMBER,)
    assert engine.PRIMARY_REFERENCES[DocType.PARTS_STATEMENT] == (FieldName.INVOICE_NUMBER,)
    assert engine.PRIMARY_REFERENCES[DocType.FLOORPLAN_STATEMENT] == (FieldName.VIN,)
    assert engine.PRIMARY_REFERENCES[DocType.INCENTIVE_STATEMENT] == (
        FieldName.PROGRAM_CODE,
        FieldName.VIN,
    )


# ---------------------------------------------------------------------------
# Pass 1: exact primary reference plus exact amount
# ---------------------------------------------------------------------------

#   statement line          ledger entry           outcome
#   1 M1000001 100000 07/05 E1 M1000001 100000     exact match, score 1.0
#   2 M1000002  25000 07/12 E2 M1000002  25000     exact match, score 1.0
#   subtotal 125000, total 125000, delta 0, no exceptions.


def scenario_exact_parts() -> Scenario:
    doc = _doc(
        "doc-parts_statement-dlr-meridian-202607-01",
        DocType.PARTS_STATEMENT,
        [
            _line(1, invoice_number="M1000001", amount_cents=100_000, line_date=date(2026, 7, 5)),
            _line(2, invoice_number="M1000002", amount_cents=25_000, line_date=date(2026, 7, 12)),
        ],
    )
    book = _book(
        _entry(
            "led-parts_payable-00001",
            invoice_number="M1000001",
            amount_cents=100_000,
            post_date=date(2026, 7, 5),
        ),
        _entry(
            "led-parts_payable-00002",
            invoice_number="M1000002",
            amount_cents=25_000,
            post_date=date(2026, 7, 12),
        ),
    )
    return doc, book


def test_pass_one_matches_exact_reference_and_amount() -> None:
    matches = _matches(scenario_exact_parts())
    assert len(matches) == 2
    assert {(match.statement_line_no, match.ledger_entry_id) for match in matches} == {
        (1, "led-parts_payable-00001"),
        (2, "led-parts_payable-00002"),
    }
    for match in matches:
        assert match.score == 1.0


def test_pass_one_leaves_no_exceptions_behind() -> None:
    assert _exceptions(scenario_exact_parts()) == []


# Primary reference per doc type: the entry carries a different secondary
# reference in each case, so only the primary one can be doing the work.


def scenario_warranty_primary_is_claim_number() -> Scenario:
    doc = _doc(
        "doc-warranty_credit_memo-dlr-meridian-202607-01",
        DocType.WARRANTY_CREDIT_MEMO,
        [
            _line(
                1,
                claim_number="4821A00551",
                ro_number="RO111111",
                amount_cents=60_000,
                line_date=date(2026, 7, 12),
            )
        ],
        statement_number="WCM-4471",
    )
    book = _book(
        _entry(
            "led-warranty_receivable-00001",
            schedule=ScheduleType.WARRANTY_RECEIVABLE,
            claim_number="4821A00551",
            ro_number="RO999999",
            amount_cents=60_000,
            post_date=date(2026, 7, 12),
        )
    )
    return doc, book


def scenario_floorplan_primary_is_vin() -> Scenario:
    doc = _doc(
        "doc-floorplan_statement-dlr-meridian-202607-01",
        DocType.FLOORPLAN_STATEMENT,
        [
            _line(
                1,
                vin=VIN_A,
                invoice_number="M0000009",
                amount_cents=200_000,
                line_date=date(2026, 7, 3),
            )
        ],
        statement_number="FP-2026-07",
    )
    book = _book(
        _entry(
            "led-floorplan_liability-00001",
            schedule=ScheduleType.FLOORPLAN_LIABILITY,
            vin=VIN_A,
            invoice_number="M0000099",
            amount_cents=200_000,
            post_date=date(2026, 7, 3),
        )
    )
    return doc, book


def scenario_incentive_primary_is_program_code_and_vin() -> Scenario:
    doc = _doc(
        "doc-incentive_statement-dlr-meridian-202607-01",
        DocType.INCENTIVE_STATEMENT,
        [
            _line(
                1,
                program_code="PGM-0042",
                vin=VIN_B,
                amount_cents=30_000,
                line_date=date(2026, 7, 9),
            )
        ],
        statement_number="INC-2026-07",
    )
    book = _book(
        _entry(
            "led-incentive_receivable-00001",
            schedule=ScheduleType.INCENTIVE_RECEIVABLE,
            program_code="PGM-0042",
            vin=VIN_B,
            amount_cents=30_000,
            post_date=date(2026, 7, 9),
        )
    )
    return doc, book


@pytest.mark.parametrize(
    ("builder", "entry_id"),
    [
        (scenario_exact_parts, "led-parts_payable-00001"),
        (scenario_warranty_primary_is_claim_number, "led-warranty_receivable-00001"),
        (scenario_floorplan_primary_is_vin, "led-floorplan_liability-00001"),
        (scenario_incentive_primary_is_program_code_and_vin, "led-incentive_receivable-00001"),
    ],
)
def test_each_doc_type_matches_on_its_primary_reference(
    builder: Callable[[], Scenario], entry_id: str
) -> None:
    matches = _matches(builder())
    assert any(
        match.statement_line_no == 1 and match.ledger_entry_id == entry_id for match in matches
    )


def scenario_secondary_reference_is_not_enough() -> Scenario:
    # The ro_number agrees but the claim_number, which is the primary reference
    # for a warranty credit memo, does not. Fuzzy score:
    #   0.5 * 0 (distance 9, no VIN) + 0.35 * 1.0 (exact amount)
    #   + 0.15 * 1.0 (same day) = 0.50, below the 0.6 threshold.
    doc = _doc(
        "doc-warranty_credit_memo-dlr-meridian-202607-02",
        DocType.WARRANTY_CREDIT_MEMO,
        [
            _line(
                1,
                claim_number="1111B22222",
                ro_number="RO123456",
                amount_cents=60_000,
                line_date=date(2026, 7, 12),
            )
        ],
        statement_number="WCM-4472",
    )
    book = _book(
        _entry(
            "led-warranty_receivable-00002",
            schedule=ScheduleType.WARRANTY_RECEIVABLE,
            claim_number="4821A00551",
            ro_number="RO123456",
            amount_cents=60_000,
            post_date=date(2026, 7, 12),
        )
    )
    return doc, book


def test_a_matching_secondary_reference_does_not_match_the_line() -> None:
    scenario = scenario_secondary_reference_is_not_enough()
    assert _matches(scenario) == []
    assert _summary(_exceptions(scenario)) == {
        (ExceptionType.MISSING_FROM_LEDGER, 1, None, 60_000, 0),
        (ExceptionType.MISSING_FROM_STATEMENT, None, "led-warranty_receivable-00002", -60_000, 0),
    }


# ---------------------------------------------------------------------------
# Pass 2: exact reference, differing amount
# ---------------------------------------------------------------------------

#   doc type              statement  ledger   expected type      impact
#   warranty_credit_memo      55000   60000   short_pay           +5000
#   incentive_statement       28000   30000   short_pay           +2000
#   parts_statement           95000  100000   amount_mismatch     -5000
#   floorplan_statement      190000  200000   amount_mismatch    -10000
#   warranty_credit_memo      65000   60000   amount_mismatch     +5000
#
# Short pay applies only when the doc type is a payment context AND the
# statement fell short. Mismatch impact is statement minus ledger; short pay
# impact is the shortfall, ledger minus statement.


def scenario_warranty_short_pay() -> Scenario:
    doc = _doc(
        "doc-warranty_credit_memo-dlr-meridian-202607-03",
        DocType.WARRANTY_CREDIT_MEMO,
        [_line(1, claim_number="4821A00551", amount_cents=55_000, line_date=date(2026, 7, 12))],
        statement_number="WCM-4473",
    )
    book = _book(
        _entry(
            "led-warranty_receivable-00003",
            schedule=ScheduleType.WARRANTY_RECEIVABLE,
            claim_number="4821A00551",
            amount_cents=60_000,
            post_date=date(2026, 7, 12),
        )
    )
    return doc, book


def scenario_incentive_short_pay() -> Scenario:
    doc = _doc(
        "doc-incentive_statement-dlr-meridian-202607-02",
        DocType.INCENTIVE_STATEMENT,
        [
            _line(
                1,
                program_code="PGM-0042",
                vin=VIN_B,
                amount_cents=28_000,
                line_date=date(2026, 7, 9),
            )
        ],
        statement_number="INC-2026-08",
    )
    book = _book(
        _entry(
            "led-incentive_receivable-00002",
            schedule=ScheduleType.INCENTIVE_RECEIVABLE,
            program_code="PGM-0042",
            vin=VIN_B,
            amount_cents=30_000,
            post_date=date(2026, 7, 9),
        )
    )
    return doc, book


def scenario_parts_lower_amount() -> Scenario:
    doc = _doc(
        "doc-parts_statement-dlr-meridian-202607-02",
        DocType.PARTS_STATEMENT,
        [_line(1, invoice_number="M1000001", amount_cents=95_000, line_date=date(2026, 7, 5))],
    )
    book = _book(
        _entry(
            "led-parts_payable-00001",
            invoice_number="M1000001",
            amount_cents=100_000,
            post_date=date(2026, 7, 5),
        )
    )
    return doc, book


def scenario_floorplan_lower_amount() -> Scenario:
    doc = _doc(
        "doc-floorplan_statement-dlr-meridian-202607-02",
        DocType.FLOORPLAN_STATEMENT,
        [_line(1, vin=VIN_A, amount_cents=190_000, line_date=date(2026, 7, 3))],
        statement_number="FP-2026-08",
    )
    book = _book(
        _entry(
            "led-floorplan_liability-00001",
            schedule=ScheduleType.FLOORPLAN_LIABILITY,
            vin=VIN_A,
            amount_cents=200_000,
            post_date=date(2026, 7, 3),
        )
    )
    return doc, book


def scenario_warranty_over_amount() -> Scenario:
    doc = _doc(
        "doc-warranty_credit_memo-dlr-meridian-202607-04",
        DocType.WARRANTY_CREDIT_MEMO,
        [_line(1, claim_number="4821A00551", amount_cents=65_000, line_date=date(2026, 7, 12))],
        statement_number="WCM-4474",
    )
    book = _book(
        _entry(
            "led-warranty_receivable-00003",
            schedule=ScheduleType.WARRANTY_RECEIVABLE,
            claim_number="4821A00551",
            amount_cents=60_000,
            post_date=date(2026, 7, 12),
        )
    )
    return doc, book


def test_warranty_credit_memo_below_the_ledger_is_a_short_pay() -> None:
    exception = _only(_exceptions(scenario_warranty_short_pay()))
    assert exception.exception_type is ExceptionType.SHORT_PAY
    assert exception.statement_amount_cents == 55_000
    assert exception.ledger_amount_cents == 60_000
    assert exception.dollar_impact_cents == 5_000


def test_incentive_statement_below_the_ledger_is_a_short_pay() -> None:
    exception = _only(_exceptions(scenario_incentive_short_pay()))
    assert exception.exception_type is ExceptionType.SHORT_PAY
    assert exception.dollar_impact_cents == 2_000


def test_parts_statement_below_the_ledger_is_an_amount_mismatch_not_a_short_pay() -> None:
    exception = _only(_exceptions(scenario_parts_lower_amount()))
    assert exception.exception_type is ExceptionType.AMOUNT_MISMATCH
    assert exception.dollar_impact_cents == -5_000


def test_floorplan_statement_below_the_ledger_is_an_amount_mismatch() -> None:
    exception = _only(_exceptions(scenario_floorplan_lower_amount()))
    assert exception.exception_type is ExceptionType.AMOUNT_MISMATCH
    assert exception.dollar_impact_cents == -10_000


def test_payment_context_above_the_ledger_is_an_amount_mismatch() -> None:
    exception = _only(_exceptions(scenario_warranty_over_amount()))
    assert exception.exception_type is ExceptionType.AMOUNT_MISMATCH
    assert exception.dollar_impact_cents == 5_000


# ---------------------------------------------------------------------------
# Pass 3: fuzzy candidates, 0.5 / 0.35 / 0.15 against a 0.6 threshold
# ---------------------------------------------------------------------------


def scenario_reference_distance_one() -> Scenario:
    # M1234567 against M1234568 is one substitution, so reference similarity is 1.0.
    # Amount is exact and the dates are 9 days apart:
    #   0.5 * 1.0 + 0.35 * 1.0 + 0.15 * (1 - 9/45)
    # = 0.5 + 0.35 + 0.15 * 0.8 = 0.5 + 0.35 + 0.12 = 0.97
    doc = _doc(
        "doc-parts_statement-dlr-meridian-202607-03",
        DocType.PARTS_STATEMENT,
        [_line(1, invoice_number="M1234568", amount_cents=100_000, line_date=date(2026, 7, 19))],
    )
    book = _book(
        _entry(
            "led-parts_payable-00010",
            invoice_number="M1234567",
            amount_cents=100_000,
            post_date=date(2026, 7, 10),
        )
    )
    return doc, book


def test_distance_one_reference_with_an_exact_amount_matches() -> None:
    matches = _matches(scenario_reference_distance_one())
    assert len(matches) == 1
    assert matches[0].ledger_entry_id == "led-parts_payable-00010"
    assert matches[0].score == pytest.approx(0.97)


def scenario_reference_distance_two() -> Scenario:
    # M1234567 against M1234588 is two substitutions, so reference similarity is 0:
    #   0.5 * 0 + 0.35 * 1.0 + 0.15 * 1.0 = 0.50, under the 0.6 threshold.
    doc = _doc(
        "doc-parts_statement-dlr-meridian-202607-04",
        DocType.PARTS_STATEMENT,
        [_line(1, invoice_number="M1234588", amount_cents=100_000, line_date=date(2026, 7, 10))],
    )
    book = _book(
        _entry(
            "led-parts_payable-00010",
            invoice_number="M1234567",
            amount_cents=100_000,
            post_date=date(2026, 7, 10),
        )
    )
    return doc, book


def test_distance_two_reference_does_not_match() -> None:
    scenario = scenario_reference_distance_two()
    assert _matches(scenario) == []
    assert _summary(_exceptions(scenario)) == {
        (ExceptionType.MISSING_FROM_LEDGER, 1, None, 100_000, 0),
        (ExceptionType.MISSING_FROM_STATEMENT, None, "led-parts_payable-00010", -100_000, 0),
    }


def scenario_vin_last_eight() -> Scenario:
    # Reference similarity comes from the shared last 8 ("A1234567").
    # Amounts 199000 against 200000 is 0.5 percent, inside 1 percent, so 0.5.
    # Dates are 15 days apart: 1 - 15/45 = 2/3.
    #   0.5 * 1.0 + 0.35 * 0.5 + 0.15 * (2/3) = 0.5 + 0.175 + 0.1 = 0.775
    doc = _doc(
        "doc-floorplan_statement-dlr-meridian-202607-03",
        DocType.FLOORPLAN_STATEMENT,
        [_line(1, vin=VIN_A_GARBLED, amount_cents=199_000, line_date=date(2026, 7, 16))],
        statement_number="FP-2026-09",
    )
    book = _book(
        _entry(
            "led-floorplan_liability-00010",
            schedule=ScheduleType.FLOORPLAN_LIABILITY,
            vin=VIN_A,
            amount_cents=200_000,
            post_date=date(2026, 7, 1),
        )
    )
    return doc, book


def test_vin_last_eight_match_clears_the_threshold() -> None:
    scenario = scenario_vin_last_eight()
    matches = _matches(scenario)
    assert len(matches) == 1
    assert matches[0].ledger_entry_id == "led-floorplan_liability-00010"
    assert matches[0].score == pytest.approx(0.775)
    types = {exception.exception_type for exception in _exceptions(scenario)}
    assert ExceptionType.MISSING_FROM_LEDGER not in types
    assert ExceptionType.MISSING_FROM_STATEMENT not in types


def scenario_just_over_the_threshold() -> Scenario:
    # Reference distance 1, amount within 1 percent, dates a full 45 days apart:
    #   0.5 * 1.0 + 0.35 * 0.5 + 0.15 * 0 = 0.5 + 0.175 = 0.675, above 0.6.
    doc = _doc(
        "doc-parts_statement-dlr-meridian-202607-05",
        DocType.PARTS_STATEMENT,
        [_line(1, invoice_number="M2000002", amount_cents=100_500, line_date=date(2026, 7, 16))],
    )
    book = _book(
        _entry(
            "led-parts_payable-00020",
            invoice_number="M2000001",
            amount_cents=100_000,
            post_date=date(2026, 6, 1),
        )
    )
    return doc, book


def test_candidate_just_over_the_threshold_matches() -> None:
    matches = _matches(scenario_just_over_the_threshold())
    assert len(matches) == 1
    assert matches[0].score == pytest.approx(0.675)


def scenario_just_under_the_threshold() -> Scenario:
    # Reference distance 1 and nothing else: the amount is five times the ledger
    # and the dates are a full 45 days apart.
    #   0.5 * 1.0 + 0.35 * 0 + 0.15 * 0 = 0.50, under 0.6.
    doc = _doc(
        "doc-parts_statement-dlr-meridian-202607-06",
        DocType.PARTS_STATEMENT,
        [_line(1, invoice_number="M3000002", amount_cents=500_000, line_date=date(2026, 7, 16))],
    )
    book = _book(
        _entry(
            "led-parts_payable-00030",
            invoice_number="M3000001",
            amount_cents=100_000,
            post_date=date(2026, 6, 1),
        )
    )
    return doc, book


def test_candidate_just_under_the_threshold_does_not_match() -> None:
    assert _matches(scenario_just_under_the_threshold()) == []


# ---------------------------------------------------------------------------
# Greedy assignment, tie breaking, and one consumption per ledger entry
# ---------------------------------------------------------------------------

# Two identical lines chase one ledger entry. Reference, amount, and date are
# equal, so the candidate scores are equal and the documented tie break order
# (amount-exact, then date-proximity, then lower line_no) resolves on line_no.
# Line 1 takes the entry; line 2 repeats a consumed match, which is a duplicate.
# The lines are listed line 2 first so source order cannot produce the answer.


def scenario_duplicate_line() -> Scenario:
    doc = _doc(
        "doc-parts_statement-dlr-meridian-202607-07",
        DocType.PARTS_STATEMENT,
        [
            _line(
                2,
                invoice_number="M4000001",
                amount_cents=100_000,
                line_date=date(2026, 7, 10),
                description="Brake pads",
            ),
            _line(
                1,
                invoice_number="M4000001",
                amount_cents=100_000,
                line_date=date(2026, 7, 10),
                description="Brake pads",
            ),
        ],
    )
    book = _book(
        _entry(
            "led-parts_payable-00040",
            invoice_number="M4000001",
            amount_cents=100_000,
            post_date=date(2026, 7, 10),
        )
    )
    return doc, book


def test_tie_between_two_lines_goes_to_the_lower_line_no() -> None:
    matches = _matches(scenario_duplicate_line())
    assert len(matches) == 1
    assert matches[0].statement_line_no == 1


def test_repeated_line_is_a_duplicate_carrying_the_duplicated_amount() -> None:
    exception = _only(_exceptions(scenario_duplicate_line()))
    assert exception.exception_type is ExceptionType.DUPLICATE
    assert exception.statement_line_no == 2
    assert exception.dollar_impact_cents == 100_000


def scenario_two_lines_one_entry() -> Scenario:
    # Same reference, different amounts, so line 2 is not a duplicate. Line 1
    # takes the entry on pass 1 and line 2 has nothing left to match.
    doc = _doc(
        "doc-parts_statement-dlr-meridian-202607-08",
        DocType.PARTS_STATEMENT,
        [
            _line(1, invoice_number="M5000001", amount_cents=100_000, line_date=date(2026, 7, 10)),
            _line(2, invoice_number="M5000001", amount_cents=70_000, line_date=date(2026, 7, 10)),
        ],
    )
    book = _book(
        _entry(
            "led-parts_payable-00050",
            invoice_number="M5000001",
            amount_cents=100_000,
            post_date=date(2026, 7, 10),
        )
    )
    return doc, book


def test_a_ledger_entry_is_consumed_once() -> None:
    scenario = scenario_two_lines_one_entry()
    matches = _matches(scenario)
    exceptions = _exceptions(scenario)
    assert len(matches) == 1
    assert matches[0].statement_line_no == 1
    assert matches[0].ledger_entry_id == "led-parts_payable-00050"
    exception = _only(exceptions)
    assert exception.exception_type is ExceptionType.MISSING_FROM_LEDGER
    assert exception.statement_line_no == 2
    assert exception.dollar_impact_cents == 70_000


# ---------------------------------------------------------------------------
# Timing differences and the two missing-side exceptions
# ---------------------------------------------------------------------------


def scenario_timing_difference() -> Scenario:
    # The entry posts on 2026-08-10, outside the 07/01 to 07/31 period but well
    # inside the 60 day blocking window, so it still blocks and still matches.
    doc = _doc(
        "doc-parts_statement-dlr-meridian-202607-09",
        DocType.PARTS_STATEMENT,
        [_line(1, invoice_number="M6000001", amount_cents=45_000, line_date=date(2026, 7, 28))],
    )
    book = _book(
        _entry(
            "led-parts_payable-00060",
            invoice_number="M6000001",
            amount_cents=45_000,
            post_date=date(2026, 8, 10),
        )
    )
    return doc, book


def test_timing_difference_carries_zero_impact_and_a_memo_amount() -> None:
    scenario = scenario_timing_difference()
    exception = _only(_exceptions(scenario))
    assert exception.exception_type is ExceptionType.TIMING_DIFFERENCE
    assert exception.dollar_impact_cents == 0
    assert exception.memo_amount_cents == 45_000
    assert exception.statement_line_no == 1
    matches = _matches(scenario)
    assert len(matches) == 1
    assert matches[0].ledger_entry_id == "led-parts_payable-00060"


def scenario_missing_from_ledger() -> Scenario:
    # Line 2 names an invoice the books have never seen. Against the only entry
    # its score is 0.5 * 0 + 0.35 * 0 + 0.15 * (1 - 15/45) = 0.1, and that entry
    # is consumed by line 1 anyway.
    doc = _doc(
        "doc-parts_statement-dlr-meridian-202607-10",
        DocType.PARTS_STATEMENT,
        [
            _line(1, invoice_number="M7000001", amount_cents=100_000, line_date=date(2026, 7, 5)),
            _line(2, invoice_number="M9999999", amount_cents=30_000, line_date=date(2026, 7, 20)),
        ],
    )
    book = _book(
        _entry(
            "led-parts_payable-00070",
            invoice_number="M7000001",
            amount_cents=100_000,
            post_date=date(2026, 7, 5),
        )
    )
    return doc, book


def test_unmatched_statement_line_is_missing_from_ledger() -> None:
    exception = _only(_exceptions(scenario_missing_from_ledger()))
    assert exception.exception_type is ExceptionType.MISSING_FROM_LEDGER
    assert exception.statement_line_no == 2
    assert exception.dollar_impact_cents == 30_000


def scenario_missing_from_statement() -> Scenario:
    # E2 blocks in and is never consumed. The three noise entries do not block:
    # another dealer, another schedule, and a post date past the 60 day window
    # (2026-07-31 plus 60 days is 2026-09-29).
    doc = _doc(
        "doc-parts_statement-dlr-meridian-202607-11",
        DocType.PARTS_STATEMENT,
        [_line(1, invoice_number="M8000001", amount_cents=100_000, line_date=date(2026, 7, 5))],
    )
    book = _book(
        _entry(
            "led-parts_payable-00080",
            invoice_number="M8000001",
            amount_cents=100_000,
            post_date=date(2026, 7, 5),
        ),
        _entry(
            "led-parts_payable-00081",
            invoice_number="M8000002",
            amount_cents=15_000,
            post_date=date(2026, 7, 20),
        ),
        _entry(
            "led-parts_payable-00082",
            invoice_number="M8000003",
            amount_cents=11_100,
            post_date=date(2026, 7, 20),
            dealer_id=OTHER_DEALER,
        ),
        _entry(
            "led-warranty_receivable-00090",
            schedule=ScheduleType.WARRANTY_RECEIVABLE,
            claim_number="4821A00551",
            amount_cents=22_200,
            post_date=date(2026, 7, 20),
        ),
        _entry(
            "led-parts_payable-00083",
            invoice_number="M8000004",
            amount_cents=33_300,
            post_date=date(2026, 11, 1),
        ),
    )
    return doc, book


def test_unconsumed_ledger_entry_is_missing_from_statement() -> None:
    exception = _only(_exceptions(scenario_missing_from_statement()))
    assert exception.exception_type is ExceptionType.MISSING_FROM_STATEMENT
    assert exception.ledger_entry_id == "led-parts_payable-00081"
    assert exception.dollar_impact_cents == -15_000


def test_blocking_excludes_other_dealers_schedules_and_stale_post_dates() -> None:
    ids = {
        exception.ledger_entry_id for exception in _exceptions(scenario_missing_from_statement())
    }
    assert "led-parts_payable-00082" not in ids
    assert "led-warranty_receivable-00090" not in ids
    assert "led-parts_payable-00083" not in ids


# ---------------------------------------------------------------------------
# Dollar impact signs, all six exception types
# ---------------------------------------------------------------------------

#   type                    scenario                     impact    memo
#   amount_mismatch         parts 95000 vs 100000         -5000       0
#   short_pay               warranty 55000 vs 60000       +5000       0
#   missing_from_ledger     statement line 30000         +30000       0
#   missing_from_statement  ledger entry 15000           -15000       0
#   duplicate               repeated line 100000        +100000       0
#   timing_difference       matched line 45000                0   45000
IMPACT_CASES: tuple[tuple[ExceptionType, Callable[[], Scenario], int, int], ...] = (
    (ExceptionType.AMOUNT_MISMATCH, scenario_parts_lower_amount, -5_000, 0),
    (ExceptionType.SHORT_PAY, scenario_warranty_short_pay, 5_000, 0),
    (ExceptionType.MISSING_FROM_LEDGER, scenario_missing_from_ledger, 30_000, 0),
    (ExceptionType.MISSING_FROM_STATEMENT, scenario_missing_from_statement, -15_000, 0),
    (ExceptionType.DUPLICATE, scenario_duplicate_line, 100_000, 0),
    (ExceptionType.TIMING_DIFFERENCE, scenario_timing_difference, 0, 45_000),
)


@pytest.mark.parametrize(("expected_type", "builder", "impact", "memo"), IMPACT_CASES)
def test_dollar_impact_signs_for_every_exception_type(
    expected_type: ExceptionType,
    builder: Callable[[], Scenario],
    impact: int,
    memo: int,
) -> None:
    exception = _only(_exceptions(builder()))
    assert exception.exception_type is expected_type
    assert exception.dollar_impact_cents == impact
    assert exception.memo_amount_cents == memo


def test_every_exception_type_is_covered_by_the_impact_table() -> None:
    assert {case[0] for case in IMPACT_CASES} == set(ExceptionType)


# ---------------------------------------------------------------------------
# Oracle mode
# ---------------------------------------------------------------------------

#   statement line                     ledger                    outcome
#   1 M1000001 100000 07/05            E1 M1000001 100000 07/05  matched, clean
#   2 M1000002  30000 07/12            E2 M1000002  25000 07/12  amount_mismatch
#                                                                +30000-25000 = +5000
#   3 M2222222  30000 07/22            no counterpart            missing_from_ledger
#                                                                +30000
#                                      E3 M1000003 15000 07/20   missing_from_statement
#                                                                -15000
#   subtotal 100000 + 30000 + 30000 = 160000 = total, delta 0.
#
# Line 3 cannot reach E3: reference distance is far, 30000 against 15000 is well
# outside 1 percent, and 2 days apart contributes 0.15 * (1 - 2/45) = 0.143.


def scenario_oracle() -> Scenario:
    doc = _doc(
        "doc-parts_statement-dlr-meridian-202607-12",
        DocType.PARTS_STATEMENT,
        [
            _line(1, invoice_number="M1000001", amount_cents=100_000, line_date=date(2026, 7, 5)),
            _line(2, invoice_number="M1000002", amount_cents=30_000, line_date=date(2026, 7, 12)),
            _line(3, invoice_number="M2222222", amount_cents=30_000, line_date=date(2026, 7, 22)),
        ],
    )
    book = _book(
        _entry(
            "led-parts_payable-00101",
            invoice_number="M1000001",
            amount_cents=100_000,
            post_date=date(2026, 7, 5),
        ),
        _entry(
            "led-parts_payable-00102",
            invoice_number="M1000002",
            amount_cents=25_000,
            post_date=date(2026, 7, 12),
        ),
        _entry(
            "led-parts_payable-00103",
            invoice_number="M1000003",
            amount_cents=15_000,
            post_date=date(2026, 7, 20),
        ),
        _entry(
            "led-parts_payable-00104",
            invoice_number="M1000004",
            amount_cents=44_400,
            post_date=date(2026, 7, 15),
            dealer_id=OTHER_DEALER,
        ),
    )
    return doc, book


ORACLE_EXPECTED: set[tuple[ExceptionType, int | None, str | None, int, int]] = {
    (ExceptionType.AMOUNT_MISMATCH, 2, "led-parts_payable-00102", 5_000, 0),
    (ExceptionType.MISSING_FROM_LEDGER, 3, None, 30_000, 0),
    (ExceptionType.MISSING_FROM_STATEMENT, None, "led-parts_payable-00103", -15_000, 0),
}


def test_oracle_mode_finds_every_injected_discrepancy_and_nothing_else() -> None:
    exceptions = _exceptions(scenario_oracle(), ReconMode.ORACLE)
    assert len(exceptions) == 3
    assert _summary(exceptions) == ORACLE_EXPECTED


def test_perfect_extraction_adds_no_extraction_attributable_exceptions() -> None:
    # End to end over the same lines is the same engine over the same input, so
    # the gap between the two published numbers is zero when extraction is perfect.
    oracle = _summary(_exceptions(scenario_oracle(), ReconMode.ORACLE))
    end_to_end = _summary(_exceptions(scenario_oracle(), ReconMode.END_TO_END))
    assert end_to_end == oracle


def test_oracle_mode_matches_the_two_lines_that_have_counterparts() -> None:
    matches = _matches(scenario_oracle(), ReconMode.ORACLE)
    assert {match.statement_line_no for match in matches} == {1, 2}


# ---------------------------------------------------------------------------
# Fixture hygiene
# ---------------------------------------------------------------------------

ALL_SCENARIOS: tuple[Callable[[], Scenario], ...] = (
    scenario_exact_parts,
    scenario_warranty_primary_is_claim_number,
    scenario_floorplan_primary_is_vin,
    scenario_incentive_primary_is_program_code_and_vin,
    scenario_secondary_reference_is_not_enough,
    scenario_warranty_short_pay,
    scenario_incentive_short_pay,
    scenario_parts_lower_amount,
    scenario_floorplan_lower_amount,
    scenario_warranty_over_amount,
    scenario_reference_distance_one,
    scenario_reference_distance_two,
    scenario_vin_last_eight,
    scenario_just_over_the_threshold,
    scenario_just_under_the_threshold,
    scenario_duplicate_line,
    scenario_two_lines_one_entry,
    scenario_timing_difference,
    scenario_missing_from_ledger,
    scenario_missing_from_statement,
    scenario_oracle,
)


def test_every_statement_fixture_satisfies_the_composer_invariants() -> None:
    for builder in ALL_SCENARIOS:
        doc, _book_unused = builder()
        assert doc.subtotal_cents == sum(line.amount_cents for line in doc.lines), doc.doc_id
        assert doc.crossfoot_delta_cents() == 0, doc.doc_id


def test_every_ledger_fixture_uses_the_schedule_for_its_doc_type() -> None:
    for builder in ALL_SCENARIOS:
        doc, book = builder()
        schedule = DOC_TYPE_SCHEDULES[doc.doc_type]
        blocked = [
            entry
            for entry in book.entries
            if entry.dealer_id == doc.dealer_id and entry.schedule is schedule
        ]
        assert blocked, doc.doc_id
