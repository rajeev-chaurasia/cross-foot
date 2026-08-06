"""Matching edges in crossfoot.reconcile.engine beyond the contract scenarios."""

from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta

from crossfoot.constants import (
    DocType,
    ExceptionType,
    LineType,
    Oem,
    ReconMode,
    ScheduleType,
)
from crossfoot.models.ledger import Dealer, LedgerBook, LedgerEntry
from crossfoot.models.statement import StatementDoc, StatementLine
from crossfoot.reconcile.engine import (
    BLOCKING_GRACE_DAYS,
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
    inside = PERIOD_END + timedelta(days=BLOCKING_GRACE_DAYS)
    outside = inside + timedelta(days=1)
    doc = _doc([_line(1, invoice_number="M1000001", amount_cents=100_000)])
    book = _book(
        _entry(
            "led-parts_payable-00001", invoice_number="M2000001", amount_cents=1, post_date=inside
        ),
        _entry(
            "led-parts_payable-00002", invoice_number="M3000001", amount_cents=1, post_date=outside
        ),
    )
    unconsumed = {
        exception.ledger_entry_id
        for exception in _run(doc, book).exceptions
        if exception.exception_type is ExceptionType.MISSING_FROM_STATEMENT
    }
    assert unconsumed == {"led-parts_payable-00001"}


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
