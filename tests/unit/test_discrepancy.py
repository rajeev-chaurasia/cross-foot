"""Unit tests for the discrepancy injectors, mirroring the contract semantics."""

import re

import pytest

from crossfoot.constants import REF_GRAMMARS, DocType, ExceptionType, FieldName
from crossfoot.generator.compose import compose_statements
from crossfoot.generator.discrepancy import inject
from crossfoot.generator.ledger_gen import generate_ledger, record_seed
from crossfoot.models.ledger import LedgerBook
from crossfoot.models.manifest import InjectedDiscrepancy
from crossfoot.models.statement import StatementDoc, StatementLine

MASTER_SEED = 42

_SHORT_PAY_TYPES = frozenset({DocType.WARRANTY_CREDIT_MEMO, DocType.INCENTIVE_STATEMENT})


@pytest.fixture(scope="module")
def book() -> LedgerBook:
    return generate_ledger(MASTER_SEED)


@pytest.fixture(scope="module")
def injected_docs(
    book: LedgerBook,
) -> list[tuple[StatementDoc, StatementDoc, tuple[InjectedDiscrepancy, ...]]]:
    """(original, mutated, records) for every composed statement."""
    results = []
    for doc in compose_statements(book, MASTER_SEED):
        mutated, records = inject(doc, book, record_seed(MASTER_SEED, f"discrepancy:{doc.doc_id}"))
        results.append((doc, mutated, records))
    return results


def _line_by_no(doc: StatementDoc, line_no: int) -> StatementLine:
    match = [line for line in doc.lines if line.line_no == line_no]
    assert len(match) == 1
    return match[0]


def _has_duplicate_pair(doc: StatementDoc) -> bool:
    keys = [
        (
            line.claim_number,
            line.ro_number,
            line.vin,
            line.invoice_number,
            line.program_code,
            line.amount_cents,
        )
        for line in doc.lines
        if any(
            (line.claim_number, line.ro_number, line.vin, line.invoice_number, line.program_code)
        )
    ]
    return len(keys) != len(set(keys))


def test_inject_is_deterministic(book: LedgerBook) -> None:
    doc = compose_statements(book, MASTER_SEED)[0]
    assert inject(doc, book, 1234) == inject(doc, book, 1234)


def test_between_one_and_three_distinct_types(
    injected_docs: list[tuple[StatementDoc, StatementDoc, tuple[InjectedDiscrepancy, ...]]],
) -> None:
    for _, _, records in injected_docs:
        assert 1 <= len(records) <= 3
        kinds = [record.expected_exception for record in records]
        assert len(kinds) == len(set(kinds))


def test_discrepancy_ids_are_sequential_slugs(
    injected_docs: list[tuple[StatementDoc, StatementDoc, tuple[InjectedDiscrepancy, ...]]],
) -> None:
    for _, mutated, records in injected_docs:
        for seq, record in enumerate(records, start=1):
            assert record.discrepancy_id == f"dis-{mutated.doc_id}-{seq}"
            assert record.doc_id == mutated.doc_id


def test_docs_recrossfoot_after_injection(
    injected_docs: list[tuple[StatementDoc, StatementDoc, tuple[InjectedDiscrepancy, ...]]],
) -> None:
    for _, mutated, _ in injected_docs:
        assert mutated.subtotal_cents == sum(line.amount_cents for line in mutated.lines)
        assert mutated.crossfoot_delta_cents() == 0
        assert [line.line_no for line in mutated.lines] == list(range(1, len(mutated.lines) + 1))


def test_missing_from_statement_semantics(
    book: LedgerBook,
    injected_docs: list[tuple[StatementDoc, StatementDoc, tuple[InjectedDiscrepancy, ...]]],
) -> None:
    entry_ids = {entry.entry_id for entry in book.entries}
    seen = 0
    for original, mutated, records in injected_docs:
        original_sources = {line.source_entry_id for line in original.lines}
        mutated_sources = {line.source_entry_id for line in mutated.lines}
        for record in records:
            if record.expected_exception is not ExceptionType.MISSING_FROM_STATEMENT:
                continue
            seen += 1
            assert record.ledger_entry_id is not None
            assert record.ledger_entry_id in entry_ids
            assert record.ledger_entry_id in original_sources
            assert record.ledger_entry_id not in mutated_sources
            assert record.statement_line_no is None
            assert record.dollar_impact_cents != 0
    assert seen > 0


def test_missing_from_ledger_semantics(
    injected_docs: list[tuple[StatementDoc, StatementDoc, tuple[InjectedDiscrepancy, ...]]],
) -> None:
    seen = 0
    for _, mutated, records in injected_docs:
        for record in records:
            if record.expected_exception is not ExceptionType.MISSING_FROM_LEDGER:
                continue
            seen += 1
            assert record.statement_line_no is not None
            orphan = _line_by_no(mutated, record.statement_line_no)
            assert orphan.source_entry_id is None
            assert record.dollar_impact_cents == orphan.amount_cents != 0
            assert mutated.period_start <= orphan.line_date <= mutated.period_end
            grammar = REF_GRAMMARS[mutated.oem]
            references = (
                (FieldName.CLAIM_NUMBER, orphan.claim_number),
                (FieldName.RO_NUMBER, orphan.ro_number),
                (FieldName.INVOICE_NUMBER, orphan.invoice_number),
                (FieldName.PROGRAM_CODE, orphan.program_code),
            )
            assert any(value for _, value in references) or orphan.vin
            for field_name, value in references:
                if value is not None:
                    assert re.fullmatch(grammar[field_name], value), value
    assert seen > 0


def test_amount_mismatch_semantics(
    book: LedgerBook,
    injected_docs: list[tuple[StatementDoc, StatementDoc, tuple[InjectedDiscrepancy, ...]]],
) -> None:
    entries = {entry.entry_id: entry for entry in book.entries}
    seen = 0
    for _, mutated, records in injected_docs:
        for record in records:
            if record.expected_exception is not ExceptionType.AMOUNT_MISMATCH:
                continue
            seen += 1
            assert record.statement_line_no is not None
            assert record.dollar_impact_cents != 0
            line = _line_by_no(mutated, record.statement_line_no)
            assert line.source_entry_id is not None
            ledger_amount = entries[line.source_entry_id].amount_cents
            assert line.amount_cents - ledger_amount == record.dollar_impact_cents
    assert seen > 0


def test_short_pay_semantics(
    book: LedgerBook,
    injected_docs: list[tuple[StatementDoc, StatementDoc, tuple[InjectedDiscrepancy, ...]]],
) -> None:
    entries = {entry.entry_id: entry for entry in book.entries}
    seen = 0
    for _, mutated, records in injected_docs:
        for record in records:
            if record.expected_exception is not ExceptionType.SHORT_PAY:
                continue
            seen += 1
            assert mutated.doc_type in _SHORT_PAY_TYPES
            assert record.statement_line_no is not None
            line = _line_by_no(mutated, record.statement_line_no)
            assert line.source_entry_id is not None
            ledger_amount = entries[line.source_entry_id].amount_cents
            shortfall = ledger_amount - line.amount_cents
            assert shortfall == record.dollar_impact_cents > 0
            # Allow one cent of rounding slack around the 10 to 60 percent band.
            assert ledger_amount * 0.10 - 1 <= shortfall <= ledger_amount * 0.60 + 1
    assert seen > 0


def test_duplicate_semantics(
    injected_docs: list[tuple[StatementDoc, StatementDoc, tuple[InjectedDiscrepancy, ...]]],
) -> None:
    seen = 0
    for _, mutated, records in injected_docs:
        for record in records:
            if record.expected_exception is not ExceptionType.DUPLICATE:
                continue
            seen += 1
            assert _has_duplicate_pair(mutated)
            assert record.statement_line_no is not None
            assert record.dollar_impact_cents != 0
    assert seen > 0


def test_timing_difference_semantics(
    injected_docs: list[tuple[StatementDoc, StatementDoc, tuple[InjectedDiscrepancy, ...]]],
) -> None:
    seen = 0
    for _, mutated, records in injected_docs:
        for record in records:
            if record.expected_exception is not ExceptionType.TIMING_DIFFERENCE:
                continue
            seen += 1
            assert record.dollar_impact_cents == 0
            assert record.memo_amount_cents != 0
            assert record.statement_line_no is not None
            line = _line_by_no(mutated, record.statement_line_no)
            assert record.memo_amount_cents == line.amount_cents
            outside = line.line_date < mutated.period_start or line.line_date > mutated.period_end
            assert outside
    assert seen > 0


def test_short_pay_never_hits_payable_doc_types(book: LedgerBook) -> None:
    for doc in compose_statements(book, MASTER_SEED):
        if doc.doc_type in _SHORT_PAY_TYPES:
            continue
        for extra_seed in range(20):
            _, records = inject(doc, book, extra_seed)
            kinds = {record.expected_exception for record in records}
            assert ExceptionType.SHORT_PAY not in kinds
