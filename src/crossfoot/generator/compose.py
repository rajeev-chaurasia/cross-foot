"""Compose monthly statements from ledger entries; printed totals always crossfoot."""

import calendar
import random
from collections import defaultdict
from datetime import date

from crossfoot.constants import DOC_TYPE_SCHEDULES, DocType, LineType, Oem, ScheduleType
from crossfoot.generator.ledger_gen import record_seed
from crossfoot.models.ledger import Dealer, LedgerBook, LedgerEntry
from crossfoot.models.statement import StatementDoc, StatementLine

DOC_LINE_TYPES: dict[DocType, LineType] = {
    DocType.PARTS_STATEMENT: LineType.CHARGE,
    DocType.WARRANTY_CREDIT_MEMO: LineType.CREDIT,
    DocType.FLOORPLAN_STATEMENT: LineType.CHARGE,
    DocType.INCENTIVE_STATEMENT: LineType.CREDIT,
}

# Balance-forward doc types carry a deterministic pseudo previous balance.
_PREVIOUS_BALANCE_RANGES: dict[DocType, tuple[int, int]] = {
    DocType.PARTS_STATEMENT: (250_000, 4_000_000),
    DocType.FLOORPLAN_STATEMENT: (8_000_000, 60_000_000),
}

_TYPE_CODES: dict[DocType, str] = {
    DocType.PARTS_STATEMENT: "PS",
    DocType.WARRANTY_CREDIT_MEMO: "WC",
    DocType.FLOORPLAN_STATEMENT: "FP",
    DocType.INCENTIVE_STATEMENT: "IN",
}

_ADJUSTMENT_PROBABILITY = 0.15
_ADJUSTMENT_RANGE_CENTS = (500, 40_000)

_GroupKey = tuple[str, ScheduleType, int, int]


def compose_statements(book: LedgerBook, master_seed: int) -> tuple[StatementDoc, ...]:
    """One statement per (dealer, doc_type, month) where entries exist."""
    grouped: dict[_GroupKey, list[LedgerEntry]] = defaultdict(list)
    for entry in book.entries:
        key = (entry.dealer_id, entry.schedule, entry.post_date.year, entry.post_date.month)
        grouped[key].append(entry)
    months = sorted({(entry.post_date.year, entry.post_date.month) for entry in book.entries})

    documents: list[StatementDoc] = []
    for dealer_num, dealer in enumerate(book.dealers, start=1):
        for doc_type in DocType:
            schedule = DOC_TYPE_SCHEDULES[doc_type]
            for year, month in months:
                entries = grouped.get((dealer.dealer_id, schedule, year, month))
                if not entries:
                    continue
                documents.append(
                    _compose_one(dealer, dealer_num, doc_type, year, month, entries, master_seed)
                )
    return tuple(documents)


def _compose_one(
    dealer: Dealer,
    dealer_num: int,
    doc_type: DocType,
    year: int,
    month: int,
    entries: list[LedgerEntry],
    master_seed: int,
) -> StatementDoc:
    doc_id = f"doc-{doc_type}-{dealer.dealer_id}-{year:04d}{month:02d}-01"
    rng = random.Random(record_seed(master_seed, doc_id))

    lines = tuple(
        _line_from_entry(line_no, doc_type, entry)
        for line_no, entry in enumerate(
            sorted(entries, key=lambda entry: (entry.post_date, entry.entry_id)), start=1
        )
    )

    previous_balance: int | None = None
    balance_range = _PREVIOUS_BALANCE_RANGES.get(doc_type)
    if balance_range is not None:
        previous_balance = rng.randint(*balance_range)

    adjustments = 0
    if rng.random() < _ADJUSTMENT_PROBABILITY:
        low, high = _ADJUSTMENT_RANGE_CENTS
        adjustments = rng.choice((-1, 1)) * rng.randint(low, high)

    subtotal = sum(line.amount_cents for line in lines)
    period_end = date(year, month, calendar.monthrange(year, month)[1])
    return StatementDoc(
        doc_id=doc_id,
        dealer_id=dealer.dealer_id,
        doc_type=doc_type,
        oem=dealer.oem,
        statement_number=_statement_number(dealer.oem, doc_type, year, month, dealer_num),
        statement_date=period_end,
        period_start=date(year, month, 1),
        period_end=period_end,
        previous_balance_cents=previous_balance,
        subtotal_cents=subtotal,
        adjustments_cents=adjustments,
        total_cents=(previous_balance or 0) + subtotal + adjustments,
        lines=lines,
    )


def _line_from_entry(line_no: int, doc_type: DocType, entry: LedgerEntry) -> StatementLine:
    return StatementLine(
        line_no=line_no,
        line_type=DOC_LINE_TYPES[doc_type],
        claim_number=entry.claim_number,
        ro_number=entry.ro_number,
        vin=entry.vin,
        invoice_number=entry.invoice_number,
        program_code=entry.program_code,
        line_date=entry.post_date,
        description=entry.description,
        amount_cents=entry.amount_cents,
        source_entry_id=entry.entry_id,
    )


def _statement_number(oem: Oem, doc_type: DocType, year: int, month: int, dealer_num: int) -> str:
    yyyymm = f"{year:04d}{month:02d}"
    code = _TYPE_CODES[doc_type]
    if oem is Oem.MERIDIAN:
        return f"MER-{code}-{yyyymm}-{dealer_num:03d}"
    if oem is Oem.NORTHSTAR:
        return f"NS{yyyymm}{dealer_num:03d}{code}"
    if oem is Oem.KAIZEN:
        return f"KZ-{yyyymm}-{code}{dealer_num:03d}"
    return f"AT-{code}-{yyyymm}-{dealer_num:03d}"
