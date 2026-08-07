"""Field and reconciliation scoring per docs/contracts-phase1.md."""

import re
from collections import defaultdict
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import date

from crossfoot.constants import (
    FIELD_FAMILIES,
    ExceptionType,
    FieldFamily,
    FieldName,
    QualityTier,
    ReconMode,
    SplitName,
)
from crossfoot.extraction.normalize import normalize_reference
from crossfoot.models.extraction import ExtractedDocument, ExtractedField
from crossfoot.models.manifest import DatasetManifest, InjectedDiscrepancy
from crossfoot.models.reconciliation import ExceptionRecord
from crossfoot.models.scorecard import FieldAccuracyCell, ReconCell
from crossfoot.models.statement import StatementDoc, StatementLine

# Zeros directly after a leading non-digit prefix are leading zeros of the
# numeric part: "RO000123" and "RO123" canonicalize to the same reference.
_PREFIX_ZEROS = re.compile(r"^(\D*)0+")

_HEADER_FIELD_NAMES = (
    FieldName.STATEMENT_NUMBER,
    FieldName.STATEMENT_DATE,
    FieldName.TOTAL,
    FieldName.SUBTOTAL,
    FieldName.PREVIOUS_BALANCE,
)
_LINE_FIELD_NAMES = (
    FieldName.CLAIM_NUMBER,
    FieldName.RO_NUMBER,
    FieldName.VIN,
    FieldName.INVOICE_NUMBER,
    FieldName.PROGRAM_CODE,
    FieldName.LINE_DATE,
    FieldName.LINE_AMOUNT,
    FieldName.DESCRIPTION,
)

_FAMILY_ORDER = {family: index for index, family in enumerate(FieldFamily)}
_TIER_ORDER = {tier: index for index, tier in enumerate(QualityTier)}

TruthValue = int | str | date | None


def field_is_correct(field: ExtractedField, truth: StatementDoc) -> bool | None:
    """Typed comparison against truth; None when truth has no value there."""
    truth_value = _resolve_truth_value(field, truth)
    if truth_value is None:
        return None
    family = FIELD_FAMILIES[field.name]
    if family is FieldFamily.AMOUNT:
        return field.value_cents == truth_value
    if family is FieldFamily.DATE:
        return field.value_date == truth_value
    if field.value is None:
        return False
    text = str(truth_value)
    if family is FieldFamily.REFERENCE:
        return _canonical_reference(field.value) == _canonical_reference(text)
    return _canonical_text(field.value) == _canonical_text(text)


def raw_is_correct(field: ExtractedField, rendered: str | None) -> bool | None:
    """Verbatim equality against the rendered string; None when it is absent."""
    if rendered is None:
        return None
    return field.raw_text == rendered


def score_fields(
    docs: Sequence[ExtractedDocument], manifest: DatasetManifest, split: SplitName
) -> tuple[FieldAccuracyCell, ...]:
    """Per (family, tier) accuracy over the split; unextracted docs still count.

    A truth field is expected only when the artifact printed it, which the
    manifest records as a rendered_values key. The phase 1 denominator survives
    beside it as fields_in_truth so a reader can judge the amendment.
    """
    docs_by_id = {doc.doc_id: doc for doc in docs}
    counts: defaultdict[tuple[FieldFamily, QualityTier], _FieldCounts] = defaultdict(_FieldCounts)
    for record in manifest.records:
        if record.split is not split or record.truth is None:
            continue
        truth = record.truth
        for key, name, value in _truth_fields(truth):
            if value is None:
                continue
            cell = counts[FIELD_FAMILIES[name], record.quality_tier]
            cell.in_truth += 1
            if key in record.rendered_values:
                cell.expected += 1
        doc = docs_by_id.get(record.doc_id)
        if doc is None:
            continue
        for field in (*doc.header_fields, *doc.line_fields):
            cell = counts[FIELD_FAMILIES[field.name], record.quality_tier]
            correct = field_is_correct(field, truth)
            if correct is None:
                cell.spurious += 1  # extracted, but resolves to no truth field
                continue
            cell.extracted += 1
            if correct:
                cell.canonical += 1
            if raw_is_correct(field, record.rendered_values.get(_rendered_key(field))):
                cell.raw += 1
    populated = sorted(
        (key for key, cell in counts.items() if cell.expected > 0),
        key=lambda key: (_FAMILY_ORDER[key[0]], _TIER_ORDER[key[1]]),
    )
    return tuple(
        FieldAccuracyCell(
            field_family=family,
            quality_tier=tier,
            fields_in_truth=counts[family, tier].in_truth,
            fields_expected=counts[family, tier].expected,
            fields_extracted=counts[family, tier].extracted,
            fields_spurious=counts[family, tier].spurious,
            correct_canonical=counts[family, tier].canonical,
            correct_raw=counts[family, tier].raw,
        )
        for family, tier in populated
    )


def score_recon(
    exceptions: Sequence[ExceptionRecord],
    manifest: DatasetManifest,
    split: SplitName,
    mode: ReconMode,
) -> tuple[ReconCell, ...]:
    """Detection accuracy per exception type with injected-side dollar accounting."""
    split_records = [record for record in manifest.records if record.split is split]
    injections = [injection for record in split_records for injection in record.injected]
    split_doc_ids = {record.doc_id for record in split_records}
    counts: defaultdict[ExceptionType, _ReconCounts] = defaultdict(_ReconCounts)
    for injection in injections:
        cell = counts[injection.expected_exception]
        cell.injected += 1
        cell.injected_dollar_cents += abs(injection.dollar_impact_cents)
    caught_ids: set[str] = set()
    for exception in exceptions:
        if exception.doc_id not in split_doc_ids:
            continue
        matched = [inj for inj in injections if _detection_matches(exception, inj)]
        if matched:
            counts[exception.exception_type].detected_true += 1
            caught_ids.update(injection.discrepancy_id for injection in matched)
        else:
            counts[exception.exception_type].detected_false += 1
    for injection in injections:
        if injection.discrepancy_id in caught_ids:
            cell = counts[injection.expected_exception]
            cell.caught_dollar_cents += abs(injection.dollar_impact_cents)
    return tuple(
        ReconCell(
            mode=mode,
            exception_type=exception_type,
            injected=counts[exception_type].injected,
            detected_true=counts[exception_type].detected_true,
            detected_false=counts[exception_type].detected_false,
            injected_dollar_cents=counts[exception_type].injected_dollar_cents,
            caught_dollar_cents=counts[exception_type].caught_dollar_cents,
        )
        for exception_type in ExceptionType
        if exception_type in counts and counts[exception_type].has_activity()
    )


@dataclass
class _FieldCounts:
    in_truth: int = 0
    expected: int = 0
    extracted: int = 0
    spurious: int = 0
    canonical: int = 0
    raw: int = 0


@dataclass
class _ReconCounts:
    injected: int = 0
    detected_true: int = 0
    detected_false: int = 0
    injected_dollar_cents: int = 0
    caught_dollar_cents: int = 0

    def has_activity(self) -> bool:
        return bool(self.injected or self.detected_true or self.detected_false)


def _detection_matches(exception: ExceptionRecord, injection: InjectedDiscrepancy) -> bool:
    """True positive: type AND doc AND (statement line OR ledger entry) match."""
    if exception.exception_type is not injection.expected_exception:
        return False
    if exception.doc_id != injection.doc_id:
        return False
    line_match = (
        injection.statement_line_no is not None
        and exception.statement_line_no == injection.statement_line_no
    )
    ledger_match = (
        injection.ledger_entry_id is not None
        and exception.ledger_entry_id == injection.ledger_entry_id
    )
    return line_match or ledger_match


def _resolve_truth_value(field: ExtractedField, truth: StatementDoc) -> TruthValue:
    if field.line_no is None:
        return _header_value(field.name, truth)
    line = next((ln for ln in truth.lines if ln.line_no == field.line_no), None)
    if line is None:
        return None
    return _line_value(field.name, line)


def _header_value(name: FieldName, truth: StatementDoc) -> TruthValue:
    if name is FieldName.STATEMENT_NUMBER:
        return truth.statement_number
    if name is FieldName.STATEMENT_DATE:
        return truth.statement_date
    if name is FieldName.TOTAL:
        return truth.total_cents
    if name is FieldName.SUBTOTAL:
        return truth.subtotal_cents
    if name is FieldName.PREVIOUS_BALANCE:
        return truth.previous_balance_cents
    return None


def _line_value(name: FieldName, line: StatementLine) -> TruthValue:
    if name is FieldName.CLAIM_NUMBER:
        return line.claim_number
    if name is FieldName.RO_NUMBER:
        return line.ro_number
    if name is FieldName.VIN:
        return line.vin
    if name is FieldName.INVOICE_NUMBER:
        return line.invoice_number
    if name is FieldName.PROGRAM_CODE:
        return line.program_code
    if name is FieldName.LINE_DATE:
        return line.line_date
    if name is FieldName.LINE_AMOUNT:
        return line.amount_cents
    if name is FieldName.DESCRIPTION:
        return line.description
    return None


def _truth_fields(truth: StatementDoc) -> Iterator[tuple[str, FieldName, TruthValue]]:
    """Every truth slot as (rendered_values key, field name, value)."""
    for name in _HEADER_FIELD_NAMES:
        yield _header_key(name), name, _header_value(name, truth)
    for line in truth.lines:
        for name in _LINE_FIELD_NAMES:
            yield _line_key(line.line_no, name), name, _line_value(name, line)


def _header_key(name: FieldName) -> str:
    return f"header:{name}"


def _line_key(line_no: int, name: FieldName) -> str:
    return f"{line_no}:{name}"


def _rendered_key(field: ExtractedField) -> str:
    if field.line_no is None:
        return _header_key(field.name)
    return _line_key(field.line_no, field.name)


def _canonical_reference(text: str) -> str:
    return _PREFIX_ZEROS.sub(r"\1", normalize_reference(text))


def _canonical_text(text: str) -> str:
    return " ".join(text.split()).casefold()
