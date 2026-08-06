"""Three-pass matching between statement lines and the dealer's own books.

Matching and classification are independent: the passes decide which ledger
entry a line belongs to, classification decides what is wrong with it. The
engine takes a StatementDoc either way, so oracle mode and end to end mode run
the same code over the same shape and their published numbers are comparable.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import StrEnum

from crossfoot.constants import (
    DOC_TYPE_SCHEDULES,
    DocType,
    ExceptionType,
    FieldName,
    ReconMode,
)
from crossfoot.extraction.normalize import normalize_reference
from crossfoot.models.ledger import LedgerBook, LedgerEntry
from crossfoot.models.reconciliation import ExceptionRecord, MatchedLine
from crossfoot.models.statement import StatementDoc, StatementLine

REFERENCE_WEIGHT = 0.5
AMOUNT_WEIGHT = 0.35
DATE_WEIGHT = 0.15
FUZZY_THRESHOLD = 0.6
DATE_DECAY_DAYS = 45
BLOCKING_GRACE_DAYS = 60

# The exact passes assert an identity, not a resemblance.
EXACT_SCORE = 1.0
# Reference similarity is all or nothing: an OCR slip is one edit, or it is a
# different reference. VIN tails survive garbling of the manufacturer prefix.
MAX_REFERENCE_EDITS = 1
VIN_TAIL_LENGTH = 8
# Amount proximity: exact, or partial credit inside a one percent band.
NEAR_AMOUNT_SIMILARITY = 0.5
AMOUNT_TOLERANCE_PERCENT = 1
PERCENT_SCALE = 100

KEY_SEPARATOR = "|"

PRIMARY_REFERENCES: dict[DocType, tuple[FieldName, ...]] = {
    DocType.WARRANTY_CREDIT_MEMO: (FieldName.CLAIM_NUMBER,),
    DocType.PARTS_STATEMENT: (FieldName.INVOICE_NUMBER,),
    DocType.FLOORPLAN_STATEMENT: (FieldName.VIN,),
    DocType.INCENTIVE_STATEMENT: (FieldName.PROGRAM_CODE, FieldName.VIN),
}

# Doc types where the factory pays the dealer, so paying under the books is a
# short pay rather than a plain disagreement about the amount.
PAYMENT_CONTEXTS: frozenset[DocType] = frozenset(
    {DocType.WARRANTY_CREDIT_MEMO, DocType.INCENTIVE_STATEMENT}
)

ReferenceKey = tuple[str, ...]


class MatchPass(StrEnum):
    EXACT = "exact"  # pass 1: primary reference and amount
    REFERENCE = "reference"  # pass 2: primary reference, amount free
    FUZZY = "fuzzy"  # pass 3: weighted similarity over the threshold


@dataclass(frozen=True, slots=True)
class Pairing:
    line: StatementLine
    entry: LedgerEntry
    score: float
    match_pass: MatchPass


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    matches: tuple[MatchedLine, ...]
    exceptions: tuple[ExceptionRecord, ...]


def reconcile(
    doc: StatementDoc,
    book: LedgerBook,
    *,
    mode: ReconMode,
    run_id: str,
    now: datetime,
) -> ReconciliationResult:
    """Match every line against the blocked ledger and classify what is left over."""
    names = PRIMARY_REFERENCES[doc.doc_type]
    lines = sorted(doc.lines, key=lambda line: line.line_no)
    pairings, unconsumed = _assign(lines, _blocked(doc, book), names)
    matched_line_nos = {pairing.line.line_no for pairing in pairings}
    # A duplicate repeats the primary reference and amount of a consumed match.
    consumed = {(_reference_key(p.line, names), p.line.amount_cents) for p in pairings}

    records = _Records(doc=doc, mode=mode, run_id=run_id, now=now)
    for pairing in pairings:
        records.classify(pairing)
    for line in lines:
        if line.line_no in matched_line_nos:
            continue
        key = _reference_key(line, names)
        if key is not None and (key, line.amount_cents) in consumed:
            records.duplicate(line, key)
        else:
            records.missing_from_ledger(line)
    for entry in unconsumed:
        records.missing_from_statement(entry)
    return ReconciliationResult(
        matches=tuple(_matched_line(doc, pairing, names) for pairing in pairings),
        exceptions=tuple(records.emitted),
    )


def _blocked(doc: StatementDoc, book: LedgerBook) -> tuple[LedgerEntry, ...]:
    """Candidate entries: same dealer, the schedule for the doc type, near the period."""
    schedule = DOC_TYPE_SCHEDULES[doc.doc_type]
    grace = timedelta(days=BLOCKING_GRACE_DAYS)
    earliest, latest = doc.period_start - grace, doc.period_end + grace
    return tuple(
        entry
        for entry in book.entries
        if entry.dealer_id == doc.dealer_id
        and entry.schedule is schedule
        and earliest <= entry.post_date <= latest
    )


def _assign(
    lines: Sequence[StatementLine],
    entries: Sequence[LedgerEntry],
    names: Sequence[FieldName],
) -> tuple[tuple[Pairing, ...], tuple[LedgerEntry, ...]]:
    """Run the three passes; every ledger entry is consumed at most once."""
    free = {entry.entry_id: entry for entry in entries}
    entry_keys = {entry.entry_id: _reference_key(entry, names) for entry in entries}
    pairings: list[Pairing] = []
    matched: set[int] = set()

    def consume(line: StatementLine, entry: LedgerEntry, score: float, taken: MatchPass) -> None:
        del free[entry.entry_id]
        matched.add(line.line_no)
        pairings.append(Pairing(line=line, entry=entry, score=score, match_pass=taken))

    for require_amount in (True, False):  # pass 1, then pass 2
        taken = MatchPass.EXACT if require_amount else MatchPass.REFERENCE
        for line in lines:
            key = _reference_key(line, names)
            if line.line_no in matched or key is None:
                continue
            entry = next(
                (
                    candidate
                    for candidate in free.values()
                    if entry_keys[candidate.entry_id] == key
                    and (not require_amount or candidate.amount_cents == line.amount_cents)
                ),
                None,
            )
            if entry is not None:
                consume(line, entry, EXACT_SCORE, taken)

    candidates = _fuzzy_candidates(lines, list(free.values()), matched, names)
    for candidate in sorted(candidates, key=_greedy_order):
        if candidate.line.line_no not in matched and candidate.entry.entry_id in free:
            consume(candidate.line, candidate.entry, candidate.score, MatchPass.FUZZY)
    return tuple(pairings), tuple(free.values())


def _fuzzy_candidates(
    lines: Sequence[StatementLine],
    entries: Sequence[LedgerEntry],
    matched: set[int],
    names: Sequence[FieldName],
) -> list[Pairing]:
    candidates: list[Pairing] = []
    for line in lines:
        if line.line_no in matched:
            continue
        for entry in entries:
            score = _score(line, entry, names)
            if score >= FUZZY_THRESHOLD:
                candidates.append(
                    Pairing(line=line, entry=entry, score=score, match_pass=MatchPass.FUZZY)
                )
    return candidates


def _greedy_order(candidate: Pairing) -> tuple[float, int, int, int]:
    """Descending score, then the documented tie breaks.

    At these weights an exact score tie already implies equal amount exactness
    and equal date proximity, so only line_no is reachable. The first two keys
    stay as defensive ordering against a future reweighting.
    """
    return (
        -candidate.score,
        0 if candidate.line.amount_cents == candidate.entry.amount_cents else 1,
        _days_apart(candidate.line.line_date, candidate.entry.post_date),
        candidate.line.line_no,
    )


def _score(line: StatementLine, entry: LedgerEntry, names: Sequence[FieldName]) -> float:
    return (
        REFERENCE_WEIGHT * _reference_similarity(line, entry, names)
        + AMOUNT_WEIGHT * _amount_similarity(line.amount_cents, entry.amount_cents)
        + DATE_WEIGHT * _date_similarity(line.line_date, entry.post_date)
    )


def _reference_similarity(
    line: StatementLine, entry: LedgerEntry, names: Sequence[FieldName]
) -> float:
    """Best evidence across the primary references, each all or nothing."""
    return max(
        _one_reference_similarity(name, _reference(line, name), _reference(entry, name))
        for name in names
    )


def _one_reference_similarity(name: FieldName, left: str | None, right: str | None) -> float:
    if left is None or right is None:
        return 0.0
    if _damerau_levenshtein(left, right) <= MAX_REFERENCE_EDITS:
        return 1.0
    tail_matches = (
        name is FieldName.VIN
        and min(len(left), len(right)) >= VIN_TAIL_LENGTH
        and left[-VIN_TAIL_LENGTH:] == right[-VIN_TAIL_LENGTH:]
    )
    return 1.0 if tail_matches else 0.0


def _amount_similarity(statement_cents: int, ledger_cents: int) -> float:
    """Integer cents throughout: the one percent band is a cross-multiplication."""
    if statement_cents == ledger_cents:
        return 1.0
    gap = abs(statement_cents - ledger_cents) * PERCENT_SCALE
    near = gap <= AMOUNT_TOLERANCE_PERCENT * abs(ledger_cents)
    return NEAR_AMOUNT_SIMILARITY if near else 0.0


def _date_similarity(line_date: date, post_date: date) -> float:
    """Linear decay to zero at the decay horizon."""
    return max(0.0, 1.0 - _days_apart(line_date, post_date) / DATE_DECAY_DAYS)


def _days_apart(left: date, right: date) -> int:
    return abs((left - right).days)


def _damerau_levenshtein(left: str, right: str) -> int:
    """Optimal string alignment distance: edits plus adjacent transpositions."""
    previous = list(range(len(right) + 1))
    older: list[int] = []
    for row, left_char in enumerate(left, start=1):
        current = [row, *([0] * len(right))]
        for column, right_char in enumerate(right, start=1):
            cost = 0 if left_char == right_char else 1
            current[column] = min(
                previous[column] + 1,
                current[column - 1] + 1,
                previous[column - 1] + cost,
            )
            transposed = (
                row > 1
                and column > 1
                and left_char == right[column - 2]
                and left[row - 2] == right_char
            )
            if transposed:
                current[column] = min(current[column], older[column - 2] + cost)
        older, previous = previous, current
    return previous[len(right)]


def _reference(source: StatementLine | LedgerEntry, name: FieldName) -> str | None:
    """Reference field names match the model attribute names by contract."""
    value = getattr(source, name.value, None)
    if not isinstance(value, str) or not value:
        return None
    return normalize_reference(value) or None


def _reference_key(
    source: StatementLine | LedgerEntry, names: Sequence[FieldName]
) -> ReferenceKey | None:
    """None when any primary reference is missing: an incomplete key never matches."""
    values: list[str] = []
    for name in names:
        value = _reference(source, name)
        if value is None:
            return None
        values.append(value)
    return tuple(values)


def _matched_line(doc: StatementDoc, pairing: Pairing, names: Sequence[FieldName]) -> MatchedLine:
    key = _reference_key(pairing.line, names)
    text = KEY_SEPARATOR.join(key) if key is not None else pairing.entry.entry_id
    return MatchedLine(
        doc_id=doc.doc_id,
        statement_line_no=pairing.line.line_no,
        ledger_entry_id=pairing.entry.entry_id,
        match_key=f"{pairing.match_pass}:{text}",
        score=pairing.score,
    )


@dataclass(slots=True)
class _Records:
    """Exception factory: fills in the run-level fields and the running id."""

    doc: StatementDoc
    mode: ReconMode
    run_id: str
    now: datetime
    emitted: list[ExceptionRecord] = field(default_factory=list)

    def classify(self, pairing: Pairing) -> None:
        """What is wrong with a matched line, independent of which pass matched it."""
        line, entry = pairing.line, pairing.entry
        if line.amount_cents == entry.amount_cents:
            if not self._within_period(entry.post_date):
                self._timing_difference(pairing)
            return
        if self.doc.doc_type in PAYMENT_CONTEXTS and line.amount_cents < entry.amount_cents:
            self._short_pay(pairing)
        else:
            self._amount_mismatch(pairing)

    def duplicate(self, line: StatementLine, key: ReferenceKey) -> None:
        self._add(
            ExceptionType.DUPLICATE,
            line=line,
            match_key=KEY_SEPARATOR.join(key),
            dollar_impact_cents=line.amount_cents,
            explanation="repeats the reference and amount of an already matched line",
        )

    def missing_from_ledger(self, line: StatementLine) -> None:
        self._add(
            ExceptionType.MISSING_FROM_LEDGER,
            line=line,
            dollar_impact_cents=line.amount_cents,
            explanation="no ledger entry matched this statement line",
        )

    def missing_from_statement(self, entry: LedgerEntry) -> None:
        self._add(
            ExceptionType.MISSING_FROM_STATEMENT,
            entry=entry,
            dollar_impact_cents=-entry.amount_cents,
            explanation="blocked ledger entry never appeared on the statement",
        )

    def _within_period(self, post_date: date) -> bool:
        return self.doc.period_start <= post_date <= self.doc.period_end

    def _timing_difference(self, pairing: Pairing) -> None:
        self._add(
            ExceptionType.TIMING_DIFFERENCE,
            line=pairing.line,
            entry=pairing.entry,
            dollar_impact_cents=0,
            memo_amount_cents=pairing.line.amount_cents,
            explanation=(
                f"ledger posted {pairing.entry.post_date.isoformat()}, outside the period"
            ),
        )

    def _short_pay(self, pairing: Pairing) -> None:
        self._add(
            ExceptionType.SHORT_PAY,
            line=pairing.line,
            entry=pairing.entry,
            # Positive by convention: the money the factory withheld.
            dollar_impact_cents=pairing.entry.amount_cents - pairing.line.amount_cents,
            explanation="statement pays less than the books expected",
        )

    def _amount_mismatch(self, pairing: Pairing) -> None:
        self._add(
            ExceptionType.AMOUNT_MISMATCH,
            line=pairing.line,
            entry=pairing.entry,
            # Signed statement minus ledger, so the sign says which side is high.
            dollar_impact_cents=pairing.line.amount_cents - pairing.entry.amount_cents,
            explanation="statement line and ledger entry disagree on the amount",
        )

    def _add(
        self,
        exception_type: ExceptionType,
        *,
        dollar_impact_cents: int,
        explanation: str,
        line: StatementLine | None = None,
        entry: LedgerEntry | None = None,
        match_key: str | None = None,
        memo_amount_cents: int = 0,
    ) -> None:
        self.emitted.append(
            ExceptionRecord(
                exception_id=f"exc-{self.mode}-{self.doc.doc_id}-{len(self.emitted) + 1:03d}",
                run_id=self.run_id,
                exception_type=exception_type,
                doc_id=self.doc.doc_id,
                statement_line_no=line.line_no if line is not None else None,
                ledger_entry_id=entry.entry_id if entry is not None else None,
                match_key=match_key,
                statement_amount_cents=line.amount_cents if line is not None else None,
                ledger_amount_cents=entry.amount_cents if entry is not None else None,
                dollar_impact_cents=dollar_impact_cents,
                memo_amount_cents=memo_amount_cents,
                explanation=explanation,
                detected_at=self.now,
            )
        )
