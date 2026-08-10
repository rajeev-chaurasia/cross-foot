"""Unit tests for the statement composer."""

import re

import pytest

from crossfoot.constants import DOC_TYPE_SCHEDULES, REF_GRAMMARS, DocType, FieldName, Oem
from crossfoot.generator.compose import DOC_LINE_TYPES, compose_statements
from crossfoot.generator.ledger_gen import GENERATION_MONTHS, generate_ledger
from crossfoot.models.ledger import LedgerBook

MASTER_SEED = 42

_BALANCE_FORWARD = frozenset({DocType.PARTS_STATEMENT, DocType.FLOORPLAN_STATEMENT})


@pytest.fixture(scope="module")
def book() -> LedgerBook:
    return generate_ledger(MASTER_SEED)


def test_compose_is_deterministic(book: LedgerBook) -> None:
    assert compose_statements(book, MASTER_SEED) == compose_statements(book, MASTER_SEED)


def test_one_statement_per_dealer_doc_type_month(book: LedgerBook) -> None:
    docs = compose_statements(book, MASTER_SEED)
    keys = [(doc.dealer_id, doc.doc_type, doc.period_start) for doc in docs]
    assert len(keys) == len(set(keys))
    assert len(docs) == len(book.dealers) * len(DocType) * len(GENERATION_MONTHS)


def test_doc_ids_are_deterministic_slugs(book: LedgerBook) -> None:
    for doc in compose_statements(book, MASTER_SEED):
        yyyymm = f"{doc.period_start.year:04d}{doc.period_start.month:02d}"
        assert doc.doc_id == f"doc-{doc.doc_type}-{doc.dealer_id}-{yyyymm}-01"


def test_every_statement_carries_lines(book: LedgerBook) -> None:
    # A statement with no lines would satisfy every per-line loop in this file
    # vacuously, so the premise those loops rest on is asserted once, here.
    docs = compose_statements(book, MASTER_SEED)
    assert docs
    for doc in docs:
        assert doc.lines, doc.doc_id


def test_lines_ordered_by_post_date_and_renumbered(book: LedgerBook) -> None:
    for doc in compose_statements(book, MASTER_SEED):
        assert doc.lines, doc.doc_id
        assert [line.line_no for line in doc.lines] == list(range(1, len(doc.lines) + 1))
        dates = [line.line_date for line in doc.lines]
        assert dates == sorted(dates)


def test_every_line_carries_matching_ledger_provenance(book: LedgerBook) -> None:
    entries = {entry.entry_id: entry for entry in book.entries}
    for doc in compose_statements(book, MASTER_SEED):
        schedule = DOC_TYPE_SCHEDULES[doc.doc_type]
        assert doc.lines, doc.doc_id
        for line in doc.lines:
            assert line.source_entry_id is not None
            entry = entries[line.source_entry_id]
            assert entry.dealer_id == doc.dealer_id
            assert entry.schedule is schedule
            assert line.amount_cents == entry.amount_cents
            assert line.line_date == entry.post_date
            assert line.description == entry.description
            assert line.claim_number == entry.claim_number
            assert line.ro_number == entry.ro_number
            assert line.vin == entry.vin
            assert line.invoice_number == entry.invoice_number
            assert line.program_code == entry.program_code
            assert line.line_type is DOC_LINE_TYPES[doc.doc_type]


def test_crossfoot_invariants(book: LedgerBook) -> None:
    for doc in compose_statements(book, MASTER_SEED):
        assert doc.subtotal_cents == sum(line.amount_cents for line in doc.lines)
        assert doc.crossfoot_delta_cents() == 0


def test_previous_balance_only_on_balance_forward_types(book: LedgerBook) -> None:
    for doc in compose_statements(book, MASTER_SEED):
        if doc.doc_type in _BALANCE_FORWARD:
            assert doc.previous_balance_cents is not None
            assert doc.previous_balance_cents > 0
        else:
            assert doc.previous_balance_cents is None


def test_adjustments_usually_zero_occasionally_not(book: LedgerBook) -> None:
    # Both halves: a rounding line or a manual credit is rare on a real
    # statement, and a generator that never prints one is not modelling one.
    docs = compose_statements(book, MASTER_SEED)
    zero = sum(1 for doc in docs if doc.adjustments_cents == 0)
    assert zero > len(docs) // 2
    assert zero < len(docs)
    assert any(doc.adjustments_cents < 0 for doc in docs)
    assert any(doc.adjustments_cents > 0 for doc in docs)


def test_period_covers_line_dates(book: LedgerBook) -> None:
    for doc in compose_statements(book, MASTER_SEED):
        assert doc.statement_date == doc.period_end
        assert doc.period_start.day == 1
        assert doc.lines, doc.doc_id
        for line in doc.lines:
            assert doc.period_start <= line.line_date <= doc.period_end


def test_statement_numbers_unique_and_marque_styled(book: LedgerBook) -> None:
    docs = compose_statements(book, MASTER_SEED)
    numbers = [doc.statement_number for doc in docs]
    assert len(numbers) == len(set(numbers))
    styles = {
        "dlr-meridian": r"MER-[A-Z]{2}-\d{6}-\d{3}",
        "dlr-northstar": r"NS\d{9}[A-Z]{2}",
        "dlr-kaizen": r"KZ-\d{6}-[A-Z]{2}\d{3}",
        # Atlas prints a flat serial matching its REF_GRAMMARS reference style.
        "dlr-atlas": REF_GRAMMARS[Oem.ATLAS][FieldName.CLAIM_NUMBER],
    }
    for doc in docs:
        assert re.fullmatch(styles[doc.dealer_id], doc.statement_number), doc.statement_number


def test_empty_book_composes_nothing() -> None:
    empty = LedgerBook(dealers=(), entries=())
    assert compose_statements(empty, MASTER_SEED) == ()
