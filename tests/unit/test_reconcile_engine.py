"""Matching edges in crossfoot.reconcile.engine beyond the contract scenarios."""

from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta

import pytest

from crossfoot.constants import (
    DocType,
    ExceptionType,
    LineType,
    Oem,
    ReconMode,
    ScheduleType,
)
from crossfoot.models.ledger import Dealer, LedgerBook, LedgerEntry
from crossfoot.models.reconciliation import ExceptionRecord
from crossfoot.models.statement import StatementDoc, StatementLine
from crossfoot.reconcile.engine import (
    BLOCKING_GRACE_DAYS,
    FUZZY_THRESHOLD,
    MatchPass,
    ReconciliationResult,
    reconcile,
)

DEALER = "dlr-meridian"
PERIOD_START = date(2026, 7, 1)
PERIOD_END = date(2026, 7, 31)
RUN_ID = "run-unit-0001"
NOW = datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC)
VIN = "1G1ZT53826F109149"


def _line(
    line_no: int,
    *,
    amount_cents: int,
    line_date: date = date(2026, 7, 10),
    invoice_number: str | None = None,
    program_code: str | None = None,
    vin: str | None = None,
) -> StatementLine:
    return StatementLine(
        line_no=line_no,
        line_type=LineType.CHARGE,
        invoice_number=invoice_number,
        program_code=program_code,
        vin=vin,
        line_date=line_date,
        description="Unit fixture line",
        amount_cents=amount_cents,
    )


def _doc(
    lines: Sequence[StatementLine], doc_type: DocType = DocType.PARTS_STATEMENT
) -> StatementDoc:
    subtotal = sum(line.amount_cents for line in lines)
    return StatementDoc(
        doc_id=f"doc-{doc_type}-{DEALER}-202607-90",
        dealer_id=DEALER,
        doc_type=doc_type,
        oem=Oem.MERIDIAN,
        statement_number="PS-2026-07-090",
        statement_date=PERIOD_END,
        period_start=PERIOD_START,
        period_end=PERIOD_END,
        subtotal_cents=subtotal,
        total_cents=subtotal,
        lines=tuple(lines),
    )


def _entry(
    entry_id: str,
    *,
    amount_cents: int,
    post_date: date = date(2026, 7, 10),
    schedule: ScheduleType = ScheduleType.PARTS_PAYABLE,
    invoice_number: str | None = None,
    program_code: str | None = None,
    vin: str | None = None,
) -> LedgerEntry:
    return LedgerEntry(
        entry_id=entry_id,
        dealer_id=DEALER,
        schedule=schedule,
        gl_account="1400-00",
        invoice_number=invoice_number,
        program_code=program_code,
        vin=vin,
        post_date=post_date,
        amount_cents=amount_cents,
        description="Unit fixture entry",
        counterparty="Meridian Motor Company",
    )


def _book(*entries: LedgerEntry) -> LedgerBook:
    dealers = (Dealer(dealer_id=DEALER, name="Meridian Motors of Ardmore", oem=Oem.MERIDIAN),)
    return LedgerBook(dealers=dealers, entries=entries)


def _run(
    doc: StatementDoc, book: LedgerBook, mode: ReconMode = ReconMode.END_TO_END
) -> ReconciliationResult:
    return reconcile(doc, book, mode=mode, run_id=RUN_ID, now=NOW)


def test_a_line_without_its_primary_reference_never_matches() -> None:
    doc = _doc([_line(1, amount_cents=100_000)])
    book = _book(_entry("led-parts_payable-00001", invoice_number="M1000001", amount_cents=100_000))
    result = _run(doc, book)
    assert result.matches == ()
    assert {exception.exception_type for exception in result.exceptions} == {
        ExceptionType.MISSING_FROM_LEDGER,
        ExceptionType.MISSING_FROM_STATEMENT,
    }


def test_the_exact_pass_prefers_the_entry_whose_amount_agrees() -> None:
    # Both entries carry the reference; the exact pass runs first, so the
    # amount decides which one is consumed regardless of ledger order.
    doc = _doc([_line(1, invoice_number="M1000001", amount_cents=100_000)])
    book = _book(
        _entry("led-parts_payable-00001", invoice_number="M1000001", amount_cents=70_000),
        _entry("led-parts_payable-00002", invoice_number="M1000001", amount_cents=100_000),
    )
    result = _run(doc, book)
    assert [match.ledger_entry_id for match in result.matches] == ["led-parts_payable-00002"]
    assert result.matches[0].match_key.startswith(MatchPass.EXACT)


def test_a_transposition_counts_as_one_edit() -> None:
    doc = _doc([_line(1, invoice_number="M1234576", amount_cents=100_000)])
    book = _book(_entry("led-parts_payable-00010", invoice_number="M1234567", amount_cents=100_000))
    result = _run(doc, book)
    assert len(result.matches) == 1
    assert result.matches[0].match_key.startswith(MatchPass.FUZZY)
    assert result.matches[0].score == 1.0


def test_an_incomplete_multi_field_key_falls_through_to_the_fuzzy_pass() -> None:
    # An incentive line needs program code and VIN together for the exact
    # passes; with the VIN missing the program code can still carry a fuzzy match.
    doc = _doc(
        [_line(1, program_code="PGM-0042", amount_cents=30_000)], DocType.INCENTIVE_STATEMENT
    )
    book = _book(
        _entry(
            "led-incentive_receivable-00001",
            schedule=ScheduleType.INCENTIVE_RECEIVABLE,
            program_code="PGM-0042",
            vin=VIN,
            amount_cents=30_000,
        )
    )
    result = _run(doc, book)
    assert len(result.matches) == 1
    assert result.matches[0].match_key.startswith(MatchPass.FUZZY)


def test_blocking_includes_the_last_day_of_the_grace_window() -> None:
    # Blocking is observed through matching, not through missing_from_statement:
    # an entry outside the period is a candidate but was never expected here.
    inside = PERIOD_END + timedelta(days=BLOCKING_GRACE_DAYS)
    outside = inside + timedelta(days=1)
    doc = _doc([_line(1, invoice_number="M1000001", amount_cents=100_000)])
    reachable = _book(
        _entry(
            "led-parts_payable-00001",
            invoice_number="M1000001",
            amount_cents=100_000,
            post_date=inside,
        )
    )
    unreachable = _book(
        _entry(
            "led-parts_payable-00002",
            invoice_number="M1000001",
            amount_cents=100_000,
            post_date=outside,
        )
    )
    assert [match.ledger_entry_id for match in _run(doc, reachable).matches] == [
        "led-parts_payable-00001"
    ]
    assert _run(doc, unreachable).matches == ()
    assert {exception.exception_type for exception in _run(doc, unreachable).exceptions} == {
        ExceptionType.MISSING_FROM_LEDGER
    }


def test_exception_ids_carry_the_mode_so_two_runs_never_collide() -> None:
    doc = _doc([_line(1, invoice_number="M9999999", amount_cents=100_000)])
    book = _book()
    oracle = _run(doc, book, ReconMode.ORACLE).exceptions
    end_to_end = _run(doc, book, ReconMode.END_TO_END).exceptions
    assert oracle[0].exception_id.startswith(f"exc-{ReconMode.ORACLE}-")
    assert end_to_end[0].exception_id.startswith(f"exc-{ReconMode.END_TO_END}-")
    assert oracle[0].exception_id != end_to_end[0].exception_id


def test_result_is_the_frozen_pair_of_matches_and_exceptions() -> None:
    doc = _doc([_line(1, invoice_number="M1000001", amount_cents=100_000)])
    result = _run(doc, _book())
    assert isinstance(result, ReconciliationResult)
    assert isinstance(result.matches, tuple)
    assert isinstance(result.exceptions, tuple)


# ---------------------------------------------------------------------------
# Expectation is narrower than blocking
# ---------------------------------------------------------------------------

# A statement carries the entries of its own dealer and schedule that posted
# inside its own period. Blocking reaches 60 days either side so a line whose
# date crossed the boundary can still find its entry, but a June or an August
# entry belongs on June's or August's statement, not on July's.
#
#   entry                     post date    blocked   expected here
#   led-parts_payable-00001   2026-07-10   yes       yes  (matched by line 1)
#   led-parts_payable-00002   2026-06-20   yes       no   (previous period)
#   led-parts_payable-00003   2026-08-14   yes       no   (following period)


def _typed(result: ReconciliationResult, kind: ExceptionType) -> list[ExceptionRecord]:
    return [exception for exception in result.exceptions if exception.exception_type is kind]


def test_an_adjacent_period_entry_is_not_missing_from_this_statement() -> None:
    doc = _doc([_line(1, invoice_number="M1000001", amount_cents=100_000)])
    book = _book(
        _entry("led-parts_payable-00001", invoice_number="M1000001", amount_cents=100_000),
        _entry(
            "led-parts_payable-00002",
            invoice_number="M2000002",
            amount_cents=55_000,
            post_date=date(2026, 6, 20),
        ),
        _entry(
            "led-parts_payable-00003",
            invoice_number="M3000003",
            amount_cents=44_000,
            post_date=date(2026, 8, 14),
        ),
    )
    result = _run(doc, book)
    assert [match.ledger_entry_id for match in result.matches] == ["led-parts_payable-00001"]
    assert result.exceptions == ()


def test_a_same_period_entry_absent_from_the_statement_is_still_reported() -> None:
    # Identical to the case above except that the unconsumed entry posted on
    # 2026-07-20, inside the period, so the statement owed it a line.
    doc = _doc([_line(1, invoice_number="M1000001", amount_cents=100_000)])
    book = _book(
        _entry("led-parts_payable-00001", invoice_number="M1000001", amount_cents=100_000),
        _entry(
            "led-parts_payable-00002",
            invoice_number="M2000002",
            amount_cents=55_000,
            post_date=date(2026, 7, 20),
        ),
    )
    result = _run(doc, book)
    exception = _typed(result, ExceptionType.MISSING_FROM_STATEMENT)
    assert len(result.exceptions) == 1
    assert exception[0].ledger_entry_id == "led-parts_payable-00002"
    assert exception[0].dollar_impact_cents == -55_000
    assert exception[0].memo_amount_cents == 0


# ---------------------------------------------------------------------------
# Timing differences: the pair straddles the boundary, either way round
# ---------------------------------------------------------------------------

#   line date    post date    period 07/01 to 07/31     outcome
#   2026-08-05   2026-07-28   line after the period     timing_difference
#   2026-06-26   2026-07-03   line before the period    timing_difference
#   2026-07-05   2026-07-25   both inside               matched, no exception
#
# Amounts agree in all three, so nothing but the dates can decide.


def _timing_scenario(line_date: date, post_date: date) -> ReconciliationResult:
    doc = _doc([_line(1, invoice_number="M6000001", amount_cents=45_000, line_date=line_date)])
    book = _book(
        _entry(
            "led-parts_payable-00060",
            invoice_number="M6000001",
            amount_cents=45_000,
            post_date=post_date,
        )
    )
    return _run(doc, book)


def test_a_line_shifted_past_period_end_matches_and_reports_a_timing_difference() -> None:
    result = _timing_scenario(date(2026, 8, 5), date(2026, 7, 28))
    assert [match.ledger_entry_id for match in result.matches] == ["led-parts_payable-00060"]
    assert len(result.exceptions) == 1
    exception = result.exceptions[0]
    assert exception.exception_type is ExceptionType.TIMING_DIFFERENCE
    assert exception.statement_line_no == 1
    assert exception.ledger_entry_id == "led-parts_payable-00060"
    assert exception.dollar_impact_cents == 0
    assert exception.memo_amount_cents == 45_000


def test_a_line_shifted_before_period_start_reports_the_same_timing_difference() -> None:
    result = _timing_scenario(date(2026, 6, 26), date(2026, 7, 3))
    assert [match.ledger_entry_id for match in result.matches] == ["led-parts_payable-00060"]
    assert len(result.exceptions) == 1
    exception = result.exceptions[0]
    assert exception.exception_type is ExceptionType.TIMING_DIFFERENCE
    assert exception.dollar_impact_cents == 0
    assert exception.memo_amount_cents == 45_000


def test_a_line_matching_inside_the_period_reports_no_timing_difference() -> None:
    # Different days, same period: period membership decides, not date equality.
    result = _timing_scenario(date(2026, 7, 5), date(2026, 7, 25))
    assert [match.ledger_entry_id for match in result.matches] == ["led-parts_payable-00060"]
    assert result.exceptions == ()


# ---------------------------------------------------------------------------
# Short pay: the two false-positive shapes are the frozen rules, not bugs
# ---------------------------------------------------------------------------


def test_a_downward_amount_change_in_a_payment_context_is_a_short_pay() -> None:
    # An amount altered downward on a payment-context doc and a genuine short
    # pay leave identical evidence: same reference, statement below the books.
    # The frozen pass 2 rule calls that SHORT_PAY, so a generator that labelled
    # the same edit AMOUNT_MISMATCH disagrees on the label, not on the facts.
    doc = _doc(
        [_line(1, program_code="PGM-0042", vin=VIN, amount_cents=348_160)],
        DocType.INCENTIVE_STATEMENT,
    )
    book = _book(
        _entry(
            "led-incentive_receivable-00001",
            schedule=ScheduleType.INCENTIVE_RECEIVABLE,
            program_code="PGM-0042",
            vin=VIN,
            amount_cents=438_160,
        )
    )
    exceptions = _run(doc, book).exceptions
    assert len(exceptions) == 1
    assert exceptions[0].exception_type is ExceptionType.SHORT_PAY
    assert exceptions[0].dollar_impact_cents == 90_000


# An orphan line can still clear the fuzzy threshold on a near-miss program
# code alone, which classification then reads as a short pay:
#   reference 0.5 * 1.0   PGM3626 against PGM3326, one substitution
#   amount    0.35 * 0    204114 against 232715 is 12.3 percent apart
#   date      0.15 * (1 - 8/45)   2026-07-07 against 2026-06-29
#   total     0.5 + 0.1233... = 0.6233..., over the 0.6 threshold
_ORPHAN_VIN = "1MEA57NC3CA6TFK3L"
_LEDGER_VIN = "1MEJS0X26BYMP70BM"  # different last 8, so only the code can score


def test_an_orphan_matching_on_a_near_miss_reference_is_classified_by_amount() -> None:
    doc = _doc(
        [
            _line(
                1,
                program_code="PGM-3626",
                vin=_ORPHAN_VIN,
                amount_cents=204_114,
                line_date=date(2026, 7, 7),
            )
        ],
        DocType.INCENTIVE_STATEMENT,
    )
    book = _book(
        _entry(
            "led-incentive_receivable-00015",
            schedule=ScheduleType.INCENTIVE_RECEIVABLE,
            program_code="PGM-3326",
            vin=_LEDGER_VIN,
            amount_cents=232_715,
            post_date=date(2026, 6, 29),
        )
    )
    result = _run(doc, book)
    assert len(result.matches) == 1
    assert result.matches[0].match_key.startswith(MatchPass.FUZZY)
    assert result.matches[0].score == pytest.approx(0.5 + 0.15 * (1 - 8 / 45))
    assert result.matches[0].score >= FUZZY_THRESHOLD
    assert len(result.exceptions) == 1
    assert result.exceptions[0].exception_type is ExceptionType.SHORT_PAY
    assert result.exceptions[0].dollar_impact_cents == 28_601
