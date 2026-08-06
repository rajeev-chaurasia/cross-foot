"""Invariants of the generated dataset, checked against the shared SMALL fixture."""

import re
from pathlib import Path

import pdfplumber

from crossfoot.constants import (
    REF_GRAMMARS,
    VIN_CHAR_VALUES,
    VIN_CHECK_DIGIT_INDEX,
    VIN_LENGTH,
    VIN_POSITION_WEIGHTS,
    ExceptionType,
    FieldName,
    QualityTier,
)
from crossfoot.models.manifest import DatasetManifest
from crossfoot.models.statement import StatementDoc, StatementLine

SCAN_TIERS = frozenset({QualityTier.SCAN_LIGHT, QualityTier.SCAN_HEAVY})


def _expected_vin_check_digit(vin: str) -> str:
    # ISO 3779: weighted transliteration sum mod 11; remainder 10 prints as X.
    weighted = sum(
        VIN_CHAR_VALUES[char] * weight
        for char, weight in zip(vin, VIN_POSITION_WEIGHTS, strict=True)
    )
    remainder = weighted % 11
    return "X" if remainder == 10 else str(remainder)


def _reference_values(line: StatementLine) -> tuple[tuple[FieldName, str | None], ...]:
    return (
        (FieldName.CLAIM_NUMBER, line.claim_number),
        (FieldName.RO_NUMBER, line.ro_number),
        (FieldName.INVOICE_NUMBER, line.invoice_number),
        (FieldName.PROGRAM_CODE, line.program_code),
    )


def _has_duplicate_pair(doc: StatementDoc) -> bool:
    # A duplicate is a re-billed line: same reference identity, same amount.
    keys: list[tuple[str | None, str | None, str | None, str | None, str | None, int]] = []
    for line in doc.lines:
        refs = (
            line.claim_number,
            line.ro_number,
            line.vin,
            line.invoice_number,
            line.program_code,
        )
        if any(refs):
            keys.append((*refs, line.amount_cents))
    return len(keys) != len(set(keys))


def _pdf_char_count(path: Path) -> int:
    with pdfplumber.open(path) as pdf:
        return sum(len(page.chars) for page in pdf.pages)


def test_truth_docs_crossfoot_cleanly(small_dataset: tuple[Path, DatasetManifest]) -> None:
    _, manifest = small_dataset
    for record in manifest.records:
        if record.corruption is not None:
            continue
        assert record.truth is not None, f"{record.doc_id}: non-corrupted record without truth"
        doc = record.truth
        assert doc.subtotal_cents == sum(line.amount_cents for line in doc.lines), doc.doc_id
        assert doc.crossfoot_delta_cents() == 0, doc.doc_id


def test_every_truth_vin_passes_iso3779(small_dataset: tuple[Path, DatasetManifest]) -> None:
    _, manifest = small_dataset
    vins: list[tuple[str, str]] = []
    for record in manifest.records:
        if record.truth is None:
            continue
        vins.extend((record.doc_id, line.vin) for line in record.truth.lines if line.vin)
    assert vins, "SMALL dataset should contain at least one VIN-bearing truth line"
    for doc_id, vin in vins:
        assert len(vin) == VIN_LENGTH, f"{doc_id}: {vin!r}"
        assert all(char in VIN_CHAR_VALUES for char in vin), f"{doc_id}: {vin!r}"
        assert vin[VIN_CHECK_DIGIT_INDEX] == _expected_vin_check_digit(vin), f"{doc_id}: {vin!r}"


def test_truth_references_match_oem_grammar(small_dataset: tuple[Path, DatasetManifest]) -> None:
    _, manifest = small_dataset
    checked = 0
    for record in manifest.records:
        if record.truth is None:
            continue
        grammar = REF_GRAMMARS[record.truth.oem]
        for line in record.truth.lines:
            for field_name, value in _reference_values(line):
                if not value:
                    continue
                pattern = grammar[field_name]
                assert re.fullmatch(pattern, value), (
                    f"{record.doc_id} line {line.line_no}: {field_name} {value!r} "
                    f"does not match {pattern!r} for {record.truth.oem}"
                )
                checked += 1
    assert checked > 0, "SMALL dataset should contain reference values to check"


def test_every_record_file_exists(small_dataset: tuple[Path, DatasetManifest]) -> None:
    out_dir, manifest = small_dataset
    for record in manifest.records:
        assert (out_dir / record.file_path).is_file(), record.file_path


def test_scan_tiers_have_no_text_layer(small_dataset: tuple[Path, DatasetManifest]) -> None:
    out_dir, manifest = small_dataset
    scanned = [record for record in manifest.records if record.quality_tier in SCAN_TIERS]
    assert scanned, "SMALL dataset should contain at least one scanned record"
    for record in scanned:
        assert _pdf_char_count(out_dir / record.file_path) == 0, record.doc_id


def test_clean_digital_pdfs_have_text_layer(small_dataset: tuple[Path, DatasetManifest]) -> None:
    out_dir, manifest = small_dataset
    clean = [
        record for record in manifest.records if record.quality_tier is QualityTier.CLEAN_DIGITAL
    ]
    assert clean, "SMALL dataset should contain at least one clean digital record"
    for record in clean:
        assert _pdf_char_count(out_dir / record.file_path) > 200, record.doc_id


def test_corruption_split_and_truth_are_consistent(
    small_dataset: tuple[Path, DatasetManifest],
) -> None:
    _, manifest = small_dataset
    for record in manifest.records:
        if record.quality_tier is QualityTier.CORRUPTED:
            assert record.corruption is not None, record.doc_id
            assert record.truth is None, record.doc_id
            assert record.split is None, record.doc_id
        else:
            assert record.corruption is None, record.doc_id
            assert record.split is not None, record.doc_id


def test_injected_discrepancies_match_their_semantics(
    small_dataset: tuple[Path, DatasetManifest],
) -> None:
    _, manifest = small_dataset
    assert any(record.injected for record in manifest.records), (
        "SMALL dataset should carry at least one injected discrepancy"
    )
    for record in manifest.records:
        if record.truth is None:
            continue
        doc = record.truth
        lines_by_no = {line.line_no: line for line in doc.lines}
        source_ids = {line.source_entry_id for line in doc.lines if line.source_entry_id}
        for injected in record.injected:
            label = f"{record.doc_id}: {injected.discrepancy_id}"
            kind = injected.expected_exception
            if kind is ExceptionType.MISSING_FROM_STATEMENT:
                assert injected.ledger_entry_id is not None, label
                assert injected.ledger_entry_id not in source_ids, label
            elif kind is ExceptionType.MISSING_FROM_LEDGER:
                assert injected.statement_line_no is not None, label
                assert injected.statement_line_no in lines_by_no, label
                assert lines_by_no[injected.statement_line_no].source_entry_id is None, label
            elif kind is ExceptionType.DUPLICATE:
                assert _has_duplicate_pair(doc), label
            elif kind in (ExceptionType.AMOUNT_MISMATCH, ExceptionType.SHORT_PAY):
                assert injected.statement_line_no is not None, label
                assert injected.dollar_impact_cents != 0, label
            elif kind is ExceptionType.TIMING_DIFFERENCE:
                assert injected.dollar_impact_cents == 0, label
                assert injected.memo_amount_cents != 0, label


def test_rendered_values_present_for_non_corrupted(
    small_dataset: tuple[Path, DatasetManifest],
) -> None:
    _, manifest = small_dataset
    for record in manifest.records:
        if record.corruption is None:
            assert record.rendered_values, record.doc_id
