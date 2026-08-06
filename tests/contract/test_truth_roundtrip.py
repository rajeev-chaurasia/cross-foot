"""Hard gate: the deterministic CSV extractor must recover CSV truth completely.

Typed comparison is implemented here on purpose; importing crossfoot.evals
would let the scorer and the gate drift together.
"""

import re
from pathlib import Path

import pytest

from crossfoot.constants import FieldName, QualityTier
from crossfoot.models.extraction import ExtractedDocument, ExtractedField
from crossfoot.models.manifest import DatasetManifest
from crossfoot.models.statement import StatementLine

_SEPARATORS = re.compile(r"[\- ]")


def _canon_ref(value: str) -> str:
    # Reference comparison: uppercase, drop "-" and " ", drop leading zeros.
    return _SEPARATORS.sub("", value).upper().lstrip("0")


def _fields_by_key(doc: ExtractedDocument) -> dict[tuple[int, FieldName], list[ExtractedField]]:
    grouped: dict[tuple[int, FieldName], list[ExtractedField]] = {}
    for field in doc.line_fields:
        if field.line_no is not None:
            grouped.setdefault((field.line_no, field.name), []).append(field)
    return grouped


def _assert_line_recovered(
    doc_id: str,
    line: StatementLine,
    fields: dict[tuple[int, FieldName], list[ExtractedField]],
) -> None:
    label = f"{doc_id} line {line.line_no}"
    amounts = fields.get((line.line_no, FieldName.LINE_AMOUNT), [])
    assert any(field.value_cents == line.amount_cents for field in amounts), (
        f"{label}: amount {line.amount_cents} not recovered"
    )
    dates = fields.get((line.line_no, FieldName.LINE_DATE), [])
    assert any(field.value_date == line.line_date for field in dates), (
        f"{label}: date {line.line_date.isoformat()} not recovered"
    )
    references: tuple[tuple[FieldName, str | None], ...] = (
        (FieldName.CLAIM_NUMBER, line.claim_number),
        (FieldName.RO_NUMBER, line.ro_number),
        (FieldName.VIN, line.vin),
        (FieldName.INVOICE_NUMBER, line.invoice_number),
        (FieldName.PROGRAM_CODE, line.program_code),
    )
    for field_name, truth_value in references:
        if not truth_value:
            continue
        candidates = fields.get((line.line_no, field_name), [])
        assert any(
            field.value is not None and _canon_ref(field.value) == _canon_ref(truth_value)
            for field in candidates
        ), f"{label}: {field_name} {truth_value!r} not recovered"


def test_csv_extraction_recovers_all_truth(small_dataset: tuple[Path, DatasetManifest]) -> None:
    tabular = pytest.importorskip("crossfoot.extraction.tabular")
    out_dir, manifest = small_dataset
    csv_records = [record for record in manifest.records if record.quality_tier is QualityTier.CSV]
    assert csv_records, "SMALL dataset should contain CSV records"
    for record in csv_records:
        assert record.truth is not None, record.doc_id
        extracted = tabular.extract_csv(out_dir / record.file_path, record.doc_id)
        assert isinstance(extracted, ExtractedDocument)
        fields = _fields_by_key(extracted)
        for line in record.truth.lines:
            _assert_line_recovered(record.doc_id, line, fields)
