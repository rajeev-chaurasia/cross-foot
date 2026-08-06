"""One injector per ExceptionType; documents stay internally consistent after mutation."""

import random
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import timedelta

from crossfoot.constants import DOC_TYPE_SCHEDULES, DocType, ExceptionType
from crossfoot.generator.compose import DOC_LINE_TYPES
from crossfoot.generator.ledger_gen import (
    ReferenceSet,
    draw_amount,
    make_description,
    make_references,
)
from crossfoot.models.ledger import LedgerBook
from crossfoot.models.manifest import InjectedDiscrepancy
from crossfoot.models.statement import StatementDoc, StatementLine

MIN_INJECTIONS = 1
MAX_INJECTIONS = 3
SHORT_PAY_MIN_FRACTION = 0.10
SHORT_PAY_MAX_FRACTION = 0.60

_SHORT_PAY_DOC_TYPES = frozenset({DocType.WARRANTY_CREDIT_MEMO, DocType.INCENTIVE_STATEMENT})
_TIMING_SHIFT_MAX_DAYS = 10
_TRANSPOSE_PROBABILITY = 0.5
_OFF_BY_CENTS_OFFSETS = (-900, -100, -10, 10, 100, 900)
_MAX_REF_ATTEMPTS = 8

# Removals first, in-place edits next, additions last: index bookkeeping stays trivial.
_APPLY_ORDER: tuple[ExceptionType, ...] = (
    ExceptionType.MISSING_FROM_STATEMENT,
    ExceptionType.AMOUNT_MISMATCH,
    ExceptionType.SHORT_PAY,
    ExceptionType.TIMING_DIFFERENCE,
    ExceptionType.MISSING_FROM_LEDGER,
    ExceptionType.DUPLICATE,
)


@dataclass(eq=False)
class _Slot:
    """Identity handle for one statement line while injectors reorder the doc."""

    line: StatementLine
    touched: bool = False


@dataclass
class _State:
    doc: StatementDoc
    book: LedgerBook
    rng: random.Random
    slots: list[_Slot]


@dataclass(frozen=True)
class _Pending:
    exception: ExceptionType
    slot: _Slot | None
    ledger_entry_id: str | None
    dollar_impact_cents: int
    memo_amount_cents: int
    description: str


def inject(
    doc: StatementDoc, book: LedgerBook, seed: int
) -> tuple[StatementDoc, tuple[InjectedDiscrepancy, ...]]:
    """Apply 1 to 3 distinct discrepancy types, then re-crossfoot the printed totals."""
    rng = random.Random(seed)
    state = _State(doc=doc, book=book, rng=rng, slots=[_Slot(line=line) for line in doc.lines])
    eligible = [kind for kind in ExceptionType if _is_eligible(kind, doc)]
    count = min(rng.randint(MIN_INJECTIONS, MAX_INJECTIONS), len(eligible))
    chosen = set(rng.sample(eligible, count))
    pending: list[_Pending] = []
    for kind in _APPLY_ORDER:
        if kind not in chosen:
            continue
        result = _INJECTORS[kind](state)
        if result is not None:
            pending.append(result)
    return _rebuild(state, pending)


def _is_eligible(kind: ExceptionType, doc: StatementDoc) -> bool:
    if kind is ExceptionType.SHORT_PAY:
        return doc.doc_type in _SHORT_PAY_DOC_TYPES and bool(doc.lines)
    if kind is ExceptionType.MISSING_FROM_STATEMENT:
        return len(doc.lines) >= 2 and bool(_sole_source_ids(doc.lines))
    return bool(doc.lines)


def _sole_source_ids(lines: Iterable[StatementLine]) -> set[str]:
    counts = Counter(line.source_entry_id for line in lines if line.source_entry_id)
    return {entry_id for entry_id, occurrences in counts.items() if occurrences == 1}


def _pick_untouched(state: _State) -> _Slot | None:
    candidates = [slot for slot in state.slots if not slot.touched]
    if not candidates:
        return None
    slot = state.rng.choice(candidates)
    slot.touched = True
    return slot


def _inject_missing_from_statement(state: _State) -> _Pending | None:
    if len(state.slots) < 2:
        return None
    sole = _sole_source_ids(slot.line for slot in state.slots)
    candidates = [
        slot for slot in state.slots if not slot.touched and slot.line.source_entry_id in sole
    ]
    if not candidates:
        return None
    slot = state.rng.choice(candidates)
    entry_id = slot.line.source_entry_id
    if entry_id is None:
        return None
    state.slots.remove(slot)
    return _Pending(
        exception=ExceptionType.MISSING_FROM_STATEMENT,
        slot=None,
        ledger_entry_id=entry_id,
        dollar_impact_cents=-slot.line.amount_cents,
        memo_amount_cents=0,
        description=f"Ledger entry {entry_id} dropped from the statement",
    )


def _inject_missing_from_ledger(state: _State) -> _Pending | None:
    doc = state.doc
    rng = state.rng
    schedule = DOC_TYPE_SCHEDULES[doc.doc_type]
    references = _fresh_references(state)
    period_days = (doc.period_end - doc.period_start).days
    orphan = StatementLine(
        line_no=0,  # renumbered on rebuild
        line_type=DOC_LINE_TYPES[doc.doc_type],
        claim_number=references.claim_number,
        ro_number=references.ro_number,
        vin=references.vin,
        invoice_number=references.invoice_number,
        program_code=references.program_code,
        line_date=doc.period_start + timedelta(days=rng.randint(0, period_days)),
        description=make_description(rng, schedule),
        amount_cents=draw_amount(rng, schedule),
        source_entry_id=None,
    )
    slot = _Slot(line=orphan, touched=True)
    state.slots.insert(rng.randint(0, len(state.slots)), slot)
    return _Pending(
        exception=ExceptionType.MISSING_FROM_LEDGER,
        slot=slot,
        ledger_entry_id=None,
        dollar_impact_cents=orphan.amount_cents,
        memo_amount_cents=0,
        description="Statement line without a ledger counterpart",
    )


def _fresh_references(state: _State) -> ReferenceSet:
    """Orphan references must look real without colliding with the actual books."""
    taken: set[str] = set()
    for entry in state.book.entries:
        for value in (
            entry.claim_number,
            entry.ro_number,
            entry.vin,
            entry.invoice_number,
            entry.program_code,
        ):
            if value:
                taken.add(value)
    schedule = DOC_TYPE_SCHEDULES[state.doc.doc_type]
    references = make_references(state.rng, state.doc.oem, schedule)
    for _ in range(_MAX_REF_ATTEMPTS):
        values = (
            references.claim_number,
            references.ro_number,
            references.vin,
            references.invoice_number,
            references.program_code,
        )
        if not any(value in taken for value in values if value):
            break
        references = make_references(state.rng, state.doc.oem, schedule)
    return references


def _inject_amount_mismatch(state: _State) -> _Pending | None:
    slot = _pick_untouched(state)
    if slot is None:
        return None
    original = slot.line.amount_cents
    mutated = _mutate_amount(original, state.rng)
    slot.line = slot.line.model_copy(update={"amount_cents": mutated})
    return _Pending(
        exception=ExceptionType.AMOUNT_MISMATCH,
        slot=slot,
        ledger_entry_id=slot.line.source_entry_id,
        dollar_impact_cents=mutated - original,
        memo_amount_cents=0,
        description=f"Amount altered from {original} to {mutated} cents",
    )


def _mutate_amount(amount: int, rng: random.Random) -> int:
    if rng.random() < _TRANSPOSE_PROBABILITY:
        digits = list(str(amount))
        swappable = [i for i in range(len(digits) - 1) if digits[i] != digits[i + 1]]
        if swappable:
            index = rng.choice(swappable)
            digits[index], digits[index + 1] = digits[index + 1], digits[index]
            transposed = int("".join(digits))
            if transposed != amount:
                return transposed
    offset = rng.choice(_OFF_BY_CENTS_OFFSETS)
    mutated = amount + offset
    return mutated if mutated > 0 else amount + abs(offset)


def _inject_short_pay(state: _State) -> _Pending | None:
    if state.doc.doc_type not in _SHORT_PAY_DOC_TYPES:
        return None
    slot = _pick_untouched(state)
    if slot is None or slot.line.amount_cents <= 1:
        return None
    original = slot.line.amount_cents
    fraction = state.rng.uniform(SHORT_PAY_MIN_FRACTION, SHORT_PAY_MAX_FRACTION)
    shortfall = min(max(1, round(original * fraction)), original - 1)
    slot.line = slot.line.model_copy(update={"amount_cents": original - shortfall})
    return _Pending(
        exception=ExceptionType.SHORT_PAY,
        slot=slot,
        ledger_entry_id=slot.line.source_entry_id,
        dollar_impact_cents=shortfall,
        memo_amount_cents=0,
        description=f"Line short paid by {shortfall} cents",
    )


def _inject_duplicate(state: _State) -> _Pending | None:
    sourced = [slot for slot in state.slots if slot.line.source_entry_id is not None]
    pool = sourced or state.slots
    if not pool:
        return None
    original = state.rng.choice(pool)
    copy_slot = _Slot(line=original.line.model_copy(), touched=True)
    state.slots.insert(state.slots.index(original) + 1, copy_slot)
    return _Pending(
        exception=ExceptionType.DUPLICATE,
        slot=copy_slot,
        ledger_entry_id=original.line.source_entry_id,
        dollar_impact_cents=original.line.amount_cents,
        memo_amount_cents=0,
        description="Line billed twice with identical references and amount",
    )


def _inject_timing_difference(state: _State) -> _Pending | None:
    slot = _pick_untouched(state)
    if slot is None:
        return None
    rng = state.rng
    shift = timedelta(days=rng.randint(1, _TIMING_SHIFT_MAX_DAYS))
    forward = rng.random() < 0.5
    shifted = state.doc.period_end + shift if forward else state.doc.period_start - shift
    original_date = slot.line.line_date
    slot.line = slot.line.model_copy(update={"line_date": shifted})
    return _Pending(
        exception=ExceptionType.TIMING_DIFFERENCE,
        slot=slot,
        ledger_entry_id=slot.line.source_entry_id,
        dollar_impact_cents=0,
        memo_amount_cents=slot.line.amount_cents,
        description=(
            f"Line date shifted from {original_date.isoformat()} to {shifted.isoformat()}"
        ),
    )


_INJECTORS: dict[ExceptionType, Callable[[_State], _Pending | None]] = {
    ExceptionType.MISSING_FROM_LEDGER: _inject_missing_from_ledger,
    ExceptionType.MISSING_FROM_STATEMENT: _inject_missing_from_statement,
    ExceptionType.AMOUNT_MISMATCH: _inject_amount_mismatch,
    ExceptionType.DUPLICATE: _inject_duplicate,
    ExceptionType.SHORT_PAY: _inject_short_pay,
    ExceptionType.TIMING_DIFFERENCE: _inject_timing_difference,
}


def _rebuild(
    state: _State, pending: list[_Pending]
) -> tuple[StatementDoc, tuple[InjectedDiscrepancy, ...]]:
    lines = tuple(
        slot.line.model_copy(update={"line_no": position})
        for position, slot in enumerate(state.slots, start=1)
    )
    subtotal = sum(line.amount_cents for line in lines)
    total = (state.doc.previous_balance_cents or 0) + subtotal + state.doc.adjustments_cents
    doc = state.doc.model_copy(
        update={"lines": lines, "subtotal_cents": subtotal, "total_cents": total}
    )
    positions = {id(slot): position for position, slot in enumerate(state.slots, start=1)}
    records = tuple(
        InjectedDiscrepancy(
            discrepancy_id=f"dis-{doc.doc_id}-{seq}",
            expected_exception=item.exception,
            doc_id=doc.doc_id,
            statement_line_no=positions[id(item.slot)] if item.slot is not None else None,
            ledger_entry_id=item.ledger_entry_id,
            dollar_impact_cents=item.dollar_impact_cents,
            memo_amount_cents=item.memo_amount_cents,
            description=item.description,
        )
        for seq, item in enumerate(pending, start=1)
    )
    return doc, records
