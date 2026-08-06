"""Contract tests for score_fields and score_recon in crossfoot.evals.metrics.

Written against docs/contracts-phase1.md before the implementation exists.
Every expected count is worked out by hand in the comment tables below.
"""

from collections.abc import Iterable
from datetime import UTC, date, datetime

import pytest

from crossfoot.constants import (
    FIELD_FAMILIES,
    DocType,
    ExceptionType,
    ExtractionRoute,
    FieldFamily,
    FieldName,
    FieldSource,
    LineType,
    Oem,
    QualityTier,
    ReconMode,
    ReviewStatus,
    SplitName,
)
from crossfoot.models.extraction import ExtractedDocument, ExtractedField, FieldSignals
from crossfoot.models.manifest import DatasetManifest, InjectedDiscrepancy, ManifestRecord
from crossfoot.models.reconciliation import ExceptionRecord
from crossfoot.models.scorecard import FieldAccuracyCell, ReconCell
from crossfoot.models.statement import StatementDoc, StatementLine

metrics = pytest.importorskip("crossfoot.evals.metrics")

DOC_A = "doc-parts_statement-dlr-meridian-202607-01"
DOC_B = "doc-warranty_credit_memo-dlr-kaizen-202607-01"
DOC_C = "doc-parts_statement-dlr-meridian-202607-02"

# Doc A: parts statement, tier csv, split train, no previous balance.
# Lines: 100000 + 25000 = 125000 = subtotal; total = 0 + 125000 + 0.
TRUTH_A = StatementDoc(
    doc_id=DOC_A,
    dealer_id="dlr-meridian",
    doc_type=DocType.PARTS_STATEMENT,
    oem=Oem.MERIDIAN,
    statement_number="PS-2026-07-001",
    statement_date=date(2026, 7, 31),
    period_start=date(2026, 7, 1),
    period_end=date(2026, 7, 31),
    previous_balance_cents=None,
    subtotal_cents=125_000,
    total_cents=125_000,
    lines=(
        StatementLine(
            line_no=1,
            line_type=LineType.CHARGE,
            invoice_number="M1234567",
            line_date=date(2026, 7, 10),
            description="Brake pads",
            amount_cents=100_000,
        ),
        StatementLine(
            line_no=2,
            line_type=LineType.CHARGE,
            invoice_number="M7654321",
            line_date=date(2026, 7, 18),
            description="Oil filters",
            amount_cents=25_000,
        ),
    ),
)

# Doc B: warranty credit memo, tier clean_digital, split train, with previous balance.
# Line: 60000 = subtotal; total = 40000 + 60000 + 0 = 100000.
TRUTH_B = StatementDoc(
    doc_id=DOC_B,
    dealer_id="dlr-kaizen",
    doc_type=DocType.WARRANTY_CREDIT_MEMO,
    oem=Oem.KAIZEN,
    statement_number="WCM-88231",
    statement_date=date(2026, 7, 31),
    period_start=date(2026, 7, 1),
    period_end=date(2026, 7, 31),
    previous_balance_cents=40_000,
    subtotal_cents=60_000,
    total_cents=100_000,
    lines=(
        StatementLine(
            line_no=1,
            line_type=LineType.CHARGE,
            claim_number="K123-456789",
            ro_number="RO-000321",
            line_date=date(2026, 7, 12),
            description="Water pump replacement",
            amount_cents=60_000,
        ),
    ),
)

# Doc C: parts statement, tier csv, split TEST. Everything about it is extracted
# perfectly, so any leakage into train scoring is visible in every cell.
TRUTH_C = StatementDoc(
    doc_id=DOC_C,
    dealer_id="dlr-meridian",
    doc_type=DocType.PARTS_STATEMENT,
    oem=Oem.MERIDIAN,
    statement_number="PS-2026-07-002",
    statement_date=date(2026, 7, 31),
    period_start=date(2026, 7, 1),
    period_end=date(2026, 7, 31),
    previous_balance_cents=None,
    subtotal_cents=7_500,
    total_cents=7_500,
    lines=(
        StatementLine(
            line_no=1,
            line_type=LineType.CHARGE,
            invoice_number="M0000001",
            line_date=date(2026, 7, 2),
            description="Air filters",
            amount_cents=7_500,
        ),
    ),
)

RENDERED_A = {
    "header:statement_number": "PS-2026-07-001",
    "header:statement_date": "07/31/2026",
    "header:total": "1,250.00",
    "header:subtotal": "1,250.00",
    "1:invoice_number": "M1234567",
    "1:line_date": "07/10/2026",
    "1:description": "Brake pads",
    "1:line_amount": "1,000.00",
    "2:invoice_number": "M7654321",
    "2:line_date": "07/18/2026",
    "2:description": "Oil filters",
    "2:line_amount": "250.00",
}

RENDERED_B = {
    "header:statement_number": "WCM-88231",
    "header:statement_date": "July 31, 2026",
    "header:total": "$1,000.00",
    "header:subtotal": "$600.00",
    "header:previous_balance": "$400.00",
    "1:claim_number": "K123-456789",
    "1:ro_number": "RO-000321",
    "1:line_date": "07/12/2026",
    "1:description": "Water pump replacement",
    "1:line_amount": "$600.00",
}

RENDERED_C = {
    "header:statement_number": "PS-2026-07-002",
    "header:statement_date": "07/31/2026",
    "header:total": "75.00",
    "header:subtotal": "75.00",
    "1:invoice_number": "M0000001",
    "1:line_date": "07/02/2026",
    "1:description": "Air filters",
    "1:line_amount": "75.00",
}


def _field(
    doc_id: str,
    name: FieldName,
    tier: QualityTier,
    *,
    line_no: int | None = None,
    value: str | None = None,
    value_cents: int | None = None,
    value_date: date | None = None,
    raw_text: str | None = None,
) -> ExtractedField:
    return ExtractedField(
        field_id=f"fld-{doc_id}-{line_no}-{name}",
        doc_id=doc_id,
        line_no=line_no,
        name=name,
        family=FIELD_FAMILIES[name],
        raw_text=raw_text,
        value=value,
        value_cents=value_cents,
        value_date=value_date,
        source=FieldSource.DETERMINISTIC,
        signals=FieldSignals(quality_tier=tier),
        confidence=1.0,
        status=ReviewStatus.AUTO_ACCEPTED,
    )


CSV = QualityTier.CSV
CLEAN = QualityTier.CLEAN_DIGITAL

# Doc A extraction, worked by hand. Canonical / raw outcome per field:
#   statement_number  ok / ok          statement_date  WRONG (07/30) / wrong raw
#   total             ok / ok          subtotal        MISSING
#   1:invoice_number  ok / wrong raw   1:line_date     ok / ok
#   1:description     WRONG / ok raw   1:line_amount   ok / ok
#   2:invoice_number  MISSING          2:line_date     MISSING
#   2:description     ok / ok          2:line_amount   WRONG (24999) / wrong raw
#   1:vin extracted but truth has no vin, so it maps to no truth field.
EXTRACTED_A = ExtractedDocument(
    doc_id=DOC_A,
    file_path="files/doc-parts_statement-dlr-meridian-202607-01.csv",
    route=ExtractionRoute.CSV,
    doc_type=DocType.PARTS_STATEMENT,
    doc_type_confidence=1.0,
    header_fields=(
        _field(
            DOC_A,
            FieldName.STATEMENT_NUMBER,
            CSV,
            value="PS-2026-07-001",
            raw_text="PS-2026-07-001",
        ),
        _field(
            DOC_A,
            FieldName.STATEMENT_DATE,
            CSV,
            value_date=date(2026, 7, 30),
            raw_text="07/30/2026",
        ),
        _field(DOC_A, FieldName.TOTAL, CSV, value_cents=125_000, raw_text="1,250.00"),
    ),
    line_fields=(
        _field(
            DOC_A,
            FieldName.INVOICE_NUMBER,
            CSV,
            line_no=1,
            value="M1234567",
            raw_text="1234567",
        ),
        _field(
            DOC_A,
            FieldName.LINE_DATE,
            CSV,
            line_no=1,
            value_date=date(2026, 7, 10),
            raw_text="07/10/2026",
        ),
        _field(
            DOC_A,
            FieldName.DESCRIPTION,
            CSV,
            line_no=1,
            value="Brake padz",
            raw_text="Brake pads",
        ),
        _field(
            DOC_A,
            FieldName.LINE_AMOUNT,
            CSV,
            line_no=1,
            value_cents=100_000,
            raw_text="1,000.00",
        ),
        _field(
            DOC_A,
            FieldName.DESCRIPTION,
            CSV,
            line_no=2,
            value="Oil filters",
            raw_text="Oil filters",
        ),
        _field(
            DOC_A,
            FieldName.LINE_AMOUNT,
            CSV,
            line_no=2,
            value_cents=24_999,
            raw_text="250.0",
        ),
        _field(
            DOC_A,
            FieldName.VIN,
            CSV,
            line_no=1,
            value="1FTFW1ET9DFC10312",
            raw_text="1FTFW1ET9DFC10312",
        ),
    ),
)

# Doc B extraction, worked by hand. Canonical / raw outcome per field:
#   statement_number ok / ok   statement_date ok / ok   total ok / ok
#   subtotal ok / ok           previous_balance MISSING
#   1:claim_number ok / ok     1:ro_number WRONG / wrong raw
#   1:description ok / ok      1:line_amount ok / ok    1:line_date MISSING
EXTRACTED_B = ExtractedDocument(
    doc_id=DOC_B,
    file_path="files/doc-warranty_credit_memo-dlr-kaizen-202607-01.pdf",
    route=ExtractionRoute.DIGITAL_PDF,
    doc_type=DocType.WARRANTY_CREDIT_MEMO,
    doc_type_confidence=1.0,
    header_fields=(
        _field(
            DOC_B,
            FieldName.STATEMENT_NUMBER,
            CLEAN,
            value="WCM-88231",
            raw_text="WCM-88231",
        ),
        _field(
            DOC_B,
            FieldName.STATEMENT_DATE,
            CLEAN,
            value_date=date(2026, 7, 31),
            raw_text="July 31, 2026",
        ),
        _field(DOC_B, FieldName.TOTAL, CLEAN, value_cents=100_000, raw_text="$1,000.00"),
        _field(DOC_B, FieldName.SUBTOTAL, CLEAN, value_cents=60_000, raw_text="$600.00"),
    ),
    line_fields=(
        _field(
            DOC_B,
            FieldName.CLAIM_NUMBER,
            CLEAN,
            line_no=1,
            value="K123-456789",
            raw_text="K123-456789",
        ),
        _field(
            DOC_B,
            FieldName.RO_NUMBER,
            CLEAN,
            line_no=1,
            value="RO-000999",
            raw_text="RO-000999",
        ),
        _field(
            DOC_B,
            FieldName.DESCRIPTION,
            CLEAN,
            line_no=1,
            value="Water pump replacement",
            raw_text="Water pump replacement",
        ),
        _field(
            DOC_B,
            FieldName.LINE_AMOUNT,
            CLEAN,
            line_no=1,
            value_cents=60_000,
            raw_text="$600.00",
        ),
    ),
)

# Doc C extraction: every scoreable field extracted, canonical and raw correct.
EXTRACTED_C = ExtractedDocument(
    doc_id=DOC_C,
    file_path="files/doc-parts_statement-dlr-meridian-202607-02.csv",
    route=ExtractionRoute.CSV,
    doc_type=DocType.PARTS_STATEMENT,
    doc_type_confidence=1.0,
    header_fields=(
        _field(
            DOC_C,
            FieldName.STATEMENT_NUMBER,
            CSV,
            value="PS-2026-07-002",
            raw_text="PS-2026-07-002",
        ),
        _field(
            DOC_C,
            FieldName.STATEMENT_DATE,
            CSV,
            value_date=date(2026, 7, 31),
            raw_text="07/31/2026",
        ),
        _field(DOC_C, FieldName.TOTAL, CSV, value_cents=7_500, raw_text="75.00"),
        _field(DOC_C, FieldName.SUBTOTAL, CSV, value_cents=7_500, raw_text="75.00"),
    ),
    line_fields=(
        _field(
            DOC_C,
            FieldName.INVOICE_NUMBER,
            CSV,
            line_no=1,
            value="M0000001",
            raw_text="M0000001",
        ),
        _field(
            DOC_C,
            FieldName.LINE_DATE,
            CSV,
            line_no=1,
            value_date=date(2026, 7, 2),
            raw_text="07/02/2026",
        ),
        _field(
            DOC_C,
            FieldName.DESCRIPTION,
            CSV,
            line_no=1,
            value="Air filters",
            raw_text="Air filters",
        ),
        _field(
            DOC_C,
            FieldName.LINE_AMOUNT,
            CSV,
            line_no=1,
            value_cents=7_500,
            raw_text="75.00",
        ),
    ),
)


def _record(
    doc_id: str,
    tier: QualityTier,
    truth: StatementDoc,
    rendered: dict[str, str],
    split: SplitName,
    injected: tuple[InjectedDiscrepancy, ...] = (),
) -> ManifestRecord:
    return ManifestRecord(
        doc_id=doc_id,
        file_path=f"files/{doc_id}.dat",
        quality_tier=tier,
        template_id="contract/test/v1",
        render_seed=1,
        truth=truth,
        rendered_values=rendered,
        injected=injected,
        split=split,
    )


MANIFEST = DatasetManifest(
    master_seed=7,
    generator_version="0.1.0",
    config_hash="0" * 64,
    records=(
        _record(DOC_A, CSV, TRUTH_A, RENDERED_A, SplitName.TRAIN),
        _record(DOC_B, CLEAN, TRUTH_B, RENDERED_B, SplitName.TRAIN),
        _record(DOC_C, CSV, TRUTH_C, RENDERED_C, SplitName.TEST),
    ),
)

# Scoreable truth fields per doc, then per (family, tier) cell:
#
# Doc A (csv): header statement_number REF, statement_date DATE, total AMT,
#   subtotal AMT (previous balance absent); each line invoice REF, date DATE,
#   description TEXT, amount AMT.
# Doc B (clean_digital): header adds previous_balance AMT; line 1 has claim REF,
#   ro REF, date DATE, description TEXT, amount AMT.
#
#   cell                     expected  extracted  canonical  raw
#   (reference, csv)         3         2          2          1
#   (date, csv)              3         2          1          1
#   (amount, csv)            4         3          2          2
#   (text, csv)              2         2          1          2
#   (reference, clean)       3         3          2          2
#   (date, clean)            2         1          1          1
#   (amount, clean)          4         3          3          3
#   (text, clean)            1         1          1          1
EXPECTED_TRAIN_CELLS: dict[tuple[FieldFamily, QualityTier], tuple[int, int, int, int]] = {
    (FieldFamily.REFERENCE, CSV): (3, 2, 2, 1),
    (FieldFamily.DATE, CSV): (3, 2, 1, 1),
    (FieldFamily.AMOUNT, CSV): (4, 3, 2, 2),
    (FieldFamily.TEXT, CSV): (2, 2, 1, 2),
    (FieldFamily.REFERENCE, CLEAN): (3, 3, 2, 2),
    (FieldFamily.DATE, CLEAN): (2, 1, 1, 1),
    (FieldFamily.AMOUNT, CLEAN): (4, 3, 3, 3),
    (FieldFamily.TEXT, CLEAN): (1, 1, 1, 1),
}

# Doc C alone (test split): header REF 1 + DATE 1 + AMT 2, line REF 1 + DATE 1
# + TEXT 1 + AMT 1, everything extracted and correct.
EXPECTED_TEST_CELLS: dict[tuple[FieldFamily, QualityTier], tuple[int, int, int, int]] = {
    (FieldFamily.REFERENCE, CSV): (2, 2, 2, 2),
    (FieldFamily.DATE, CSV): (2, 2, 2, 2),
    (FieldFamily.AMOUNT, CSV): (3, 3, 3, 3),
    (FieldFamily.TEXT, CSV): (1, 1, 1, 1),
}


def _field_cells_by_key(
    cells: Iterable[object],
) -> dict[tuple[FieldFamily, QualityTier], FieldAccuracyCell]:
    out: dict[tuple[FieldFamily, QualityTier], FieldAccuracyCell] = {}
    for cell in cells:
        assert isinstance(cell, FieldAccuracyCell)
        key = (cell.field_family, cell.quality_tier)
        assert key not in out, f"duplicate cell for {key}"
        out[key] = cell
    return out


def _assert_field_cells(
    cells: Iterable[object],
    expected: dict[tuple[FieldFamily, QualityTier], tuple[int, int, int, int]],
) -> None:
    by_key = _field_cells_by_key(cells)
    for key, (n_expected, n_extracted, n_canonical, n_raw) in expected.items():
        assert key in by_key, f"missing cell for {key}"
        cell = by_key.pop(key)
        assert cell.fields_expected == n_expected, key
        assert cell.fields_extracted == n_extracted, key
        assert cell.correct_canonical == n_canonical, key
        assert cell.correct_raw == n_raw, key
    for key, cell in by_key.items():
        assert cell.fields_expected == 0, key
        assert cell.fields_extracted == 0, key
        assert cell.correct_canonical == 0, key
        assert cell.correct_raw == 0, key


def test_score_fields_exact_train_counts() -> None:
    cells = metrics.score_fields([EXTRACTED_A, EXTRACTED_B, EXTRACTED_C], MANIFEST, SplitName.TRAIN)
    assert isinstance(cells, tuple)
    _assert_field_cells(cells, EXPECTED_TRAIN_CELLS)


def test_doc_in_test_split_is_excluded_from_train_scoring() -> None:
    with_c = metrics.score_fields(
        [EXTRACTED_A, EXTRACTED_B, EXTRACTED_C], MANIFEST, SplitName.TRAIN
    )
    without_c = metrics.score_fields([EXTRACTED_A, EXTRACTED_B], MANIFEST, SplitName.TRAIN)
    assert _field_cells_by_key(with_c) == _field_cells_by_key(without_c)


def test_test_split_scores_only_the_test_doc() -> None:
    cells = metrics.score_fields([EXTRACTED_A, EXTRACTED_B, EXTRACTED_C], MANIFEST, SplitName.TEST)
    _assert_field_cells(cells, EXPECTED_TEST_CELLS)


def test_unextracted_train_doc_still_counts_expected_fields() -> None:
    # Doc B is in train but was never extracted: its truth fields stay in the
    # denominator with nothing extracted, matching the phase 1 baseline rule.
    cells = _field_cells_by_key(metrics.score_fields([EXTRACTED_A], MANIFEST, SplitName.TRAIN))
    cell = cells[(FieldFamily.AMOUNT, CLEAN)]
    assert cell.fields_expected == 4
    assert cell.fields_extracted == 0
    assert cell.correct_canonical == 0
    assert cell.correct_raw == 0


# score_recon: injections and detections, worked by hand.
#
#   inj1 amount_mismatch        doc A line 1  5000   caught exactly by exc1
#   inj2 missing_from_ledger    doc A line 2  25000  exc2 calls it duplicate: the
#        detection is a false positive under duplicate AND inj2 stays uncaught
#   inj3 missing_from_statement doc B ledger led-warranty_receivable-00042 40000
#        caught by exc3 via the ledger entry id
#   inj4 amount_mismatch        doc B line 1  12000  never detected
#   inj5 short_pay              doc C line 1  9999   doc C is split test: ignored
#   exc4 short_pay              doc B ledger led-warranty_receivable-00099 3000
#        matches nothing injected: pure false positive
#
#   type                    injected  det_true  det_false  inj_dollars      caught
#   amount_mismatch         2         1         0          17000=5000+12000 5000
#   missing_from_ledger     1         0         0          25000            0
#   missing_from_statement  1         1         0          40000            40000
#   duplicate               0         0         1          0                0
#   short_pay               0         0         1          0                0
RECON_MANIFEST = DatasetManifest(
    master_seed=7,
    generator_version="0.1.0",
    config_hash="1" * 64,
    records=(
        _record(
            DOC_A,
            CSV,
            TRUTH_A,
            RENDERED_A,
            SplitName.TRAIN,
            injected=(
                InjectedDiscrepancy(
                    discrepancy_id=f"dis-{DOC_A}-1",
                    expected_exception=ExceptionType.AMOUNT_MISMATCH,
                    doc_id=DOC_A,
                    statement_line_no=1,
                    dollar_impact_cents=5_000,
                    description="line 1 amount bumped by 5000 cents",
                ),
                InjectedDiscrepancy(
                    discrepancy_id=f"dis-{DOC_A}-2",
                    expected_exception=ExceptionType.MISSING_FROM_LEDGER,
                    doc_id=DOC_A,
                    statement_line_no=2,
                    dollar_impact_cents=25_000,
                    description="line 2 has no ledger counterpart",
                ),
            ),
        ),
        _record(
            DOC_B,
            CLEAN,
            TRUTH_B,
            RENDERED_B,
            SplitName.TRAIN,
            injected=(
                InjectedDiscrepancy(
                    discrepancy_id=f"dis-{DOC_B}-1",
                    expected_exception=ExceptionType.MISSING_FROM_STATEMENT,
                    doc_id=DOC_B,
                    ledger_entry_id="led-warranty_receivable-00042",
                    dollar_impact_cents=40_000,
                    description="ledger entry dropped from the statement",
                ),
                InjectedDiscrepancy(
                    discrepancy_id=f"dis-{DOC_B}-2",
                    expected_exception=ExceptionType.AMOUNT_MISMATCH,
                    doc_id=DOC_B,
                    statement_line_no=1,
                    dollar_impact_cents=12_000,
                    description="line 1 amount shaved by 12000 cents",
                ),
            ),
        ),
        _record(
            DOC_C,
            CSV,
            TRUTH_C,
            RENDERED_C,
            SplitName.TEST,
            injected=(
                InjectedDiscrepancy(
                    discrepancy_id=f"dis-{DOC_C}-1",
                    expected_exception=ExceptionType.SHORT_PAY,
                    doc_id=DOC_C,
                    statement_line_no=1,
                    dollar_impact_cents=9_999,
                    description="short pay in the test split, must not count",
                ),
            ),
        ),
    ),
)

DETECTED_AT = datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC)

EXCEPTIONS = (
    ExceptionRecord(
        exception_id="exc-01",
        run_id="run-contract-0001",
        exception_type=ExceptionType.AMOUNT_MISMATCH,
        doc_id=DOC_A,
        statement_line_no=1,
        statement_amount_cents=105_000,
        ledger_amount_cents=100_000,
        dollar_impact_cents=5_000,
        explanation="statement shows 5000 cents more than the ledger",
        detected_at=DETECTED_AT,
    ),
    ExceptionRecord(
        exception_id="exc-02",
        run_id="run-contract-0001",
        exception_type=ExceptionType.DUPLICATE,
        doc_id=DOC_A,
        statement_line_no=2,
        dollar_impact_cents=25_000,
        explanation="wrong call: the injected discrepancy is missing_from_ledger",
        detected_at=DETECTED_AT,
    ),
    ExceptionRecord(
        exception_id="exc-03",
        run_id="run-contract-0001",
        exception_type=ExceptionType.MISSING_FROM_STATEMENT,
        doc_id=DOC_B,
        ledger_entry_id="led-warranty_receivable-00042",
        dollar_impact_cents=40_000,
        explanation="ledger entry never appears on the statement",
        detected_at=DETECTED_AT,
    ),
    ExceptionRecord(
        exception_id="exc-04",
        run_id="run-contract-0001",
        exception_type=ExceptionType.SHORT_PAY,
        doc_id=DOC_B,
        ledger_entry_id="led-warranty_receivable-00099",
        dollar_impact_cents=3_000,
        explanation="phantom short pay, nothing was injected here",
        detected_at=DETECTED_AT,
    ),
)

EXPECTED_RECON_CELLS: dict[ExceptionType, tuple[int, int, int, int, int]] = {
    ExceptionType.AMOUNT_MISMATCH: (2, 1, 0, 17_000, 5_000),
    ExceptionType.MISSING_FROM_LEDGER: (1, 0, 0, 25_000, 0),
    ExceptionType.MISSING_FROM_STATEMENT: (1, 1, 0, 40_000, 40_000),
    ExceptionType.DUPLICATE: (0, 0, 1, 0, 0),
    ExceptionType.SHORT_PAY: (0, 0, 1, 0, 0),
}


def _recon_cells_by_type(cells: Iterable[object]) -> dict[ExceptionType, ReconCell]:
    out: dict[ExceptionType, ReconCell] = {}
    for cell in cells:
        assert isinstance(cell, ReconCell)
        assert cell.exception_type not in out, f"duplicate cell for {cell.exception_type}"
        out[cell.exception_type] = cell
    return out


def test_score_recon_exact_cells() -> None:
    cells = metrics.score_recon(EXCEPTIONS, RECON_MANIFEST, SplitName.TRAIN, ReconMode.ORACLE)
    assert isinstance(cells, tuple)
    by_type = _recon_cells_by_type(cells)
    for cell in by_type.values():
        assert cell.mode is ReconMode.ORACLE
    for etype, expected in EXPECTED_RECON_CELLS.items():
        injected, det_true, det_false, inj_dollars, caught_dollars = expected
        assert etype in by_type, f"missing cell for {etype}"
        cell = by_type.pop(etype)
        assert cell.injected == injected, etype
        assert cell.detected_true == det_true, etype
        assert cell.detected_false == det_false, etype
        assert cell.injected_dollar_cents == inj_dollars, etype
        assert cell.caught_dollar_cents == caught_dollars, etype
    for etype, cell in by_type.items():
        assert cell.injected == 0, etype
        assert cell.detected_true == 0, etype
        assert cell.detected_false == 0, etype
        assert cell.injected_dollar_cents == 0, etype
        assert cell.caught_dollar_cents == 0, etype
