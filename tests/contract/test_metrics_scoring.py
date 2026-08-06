"""Contract tests for score_fields and score_recon in crossfoot.evals.metrics.

Re-frozen for the phase 2 scoring amendment in docs/contracts-phase2.md: a truth
field is expected only when the artifact actually printed it, which the manifest
records as a `rendered_values` key ("header:{field_name}" or "{line_no}:{field_name}").
The scorecard gains `fields_present_in_artifact` per cell.

models/scorecard.py does not carry `fields_present_in_artifact` yet, so every
assertion that depends on the amended denominator is gated on that field landing.
The cases below where the phase 1 rule and the amended rule agree stay ungated and
must pass against the current implementation.

Every expected count is worked out by hand in the comment tables.
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

# The amendment lands as one unit: the new per-cell field and the new denominator.
# Presence of the field is the marker that score_fields now follows the amended rule.
AMENDED_SCORING = "fields_present_in_artifact" in FieldAccuracyCell.model_fields

requires_amendment = pytest.mark.skipif(
    not AMENDED_SCORING,
    reason="scoring amendment pending: FieldAccuracyCell has no fields_present_in_artifact",
)

DOC_A = "doc-parts_statement-dlr-meridian-202607-01"
DOC_B = "doc-warranty_credit_memo-dlr-kaizen-202607-01"
DOC_C = "doc-parts_statement-dlr-meridian-202607-02"
DOC_D = "doc-incentive_statement-dlr-northstar-202607-01"

VIN_D1 = "1G1ZT53826F109149"
VIN_D2 = "JTDKN3DU8A1234567"

CSV = QualityTier.CSV
CLEAN = QualityTier.CLEAN_DIGITAL
XLSX = QualityTier.XLSX

# Doc A: parts statement, tier csv, split train, with a previous balance.
# Lines: 100000 + 25000 = 125000 = subtotal.
# Total: 20000 previous + 125000 lines + 0 adjustments = 145000, so the delta is 0.
TRUTH_A = StatementDoc(
    doc_id=DOC_A,
    dealer_id="dlr-meridian",
    doc_type=DocType.PARTS_STATEMENT,
    oem=Oem.MERIDIAN,
    statement_number="PS-2026-07-001",
    statement_date=date(2026, 7, 31),
    period_start=date(2026, 7, 1),
    period_end=date(2026, 7, 31),
    previous_balance_cents=20_000,
    subtotal_cents=125_000,
    total_cents=145_000,
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
# perfectly, so any leakage into train scoring is visible in every csv cell.
# One line of 7500 = subtotal; no previous balance, so total = 0 + 7500 + 0.
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

# Doc D: incentive statement, tier xlsx, split train. The xlsx export prints a
# statement number, a statement date, and a subtotal, but neither the total nor
# the previous balance, so those two truth amounts are never expected.
# Lines: 30000 + (-5000) = 25000 = subtotal.
# Total: 10000 previous + 25000 lines + 0 adjustments = 35000, so the delta is 0.
TRUTH_D = StatementDoc(
    doc_id=DOC_D,
    dealer_id="dlr-northstar",
    doc_type=DocType.INCENTIVE_STATEMENT,
    oem=Oem.NORTHSTAR,
    statement_number="INC-2026-07-014",
    statement_date=date(2026, 7, 31),
    period_start=date(2026, 7, 1),
    period_end=date(2026, 7, 31),
    previous_balance_cents=10_000,
    subtotal_cents=25_000,
    total_cents=35_000,
    lines=(
        StatementLine(
            line_no=1,
            line_type=LineType.CREDIT,
            program_code="NS-AB123",
            vin=VIN_D1,
            line_date=date(2026, 7, 8),
            description="Volume bonus Q3",
            amount_cents=30_000,
        ),
        StatementLine(
            line_no=2,
            line_type=LineType.ADJUSTMENT,
            program_code="NS-CD456",
            vin=VIN_D2,
            line_date=date(2026, 7, 22),
            description="Chargeback adjustment",
            amount_cents=-5_000,
        ),
    ),
)

# A csv export is rows and nothing else: no statement number, no dates in a
# header block, no totals. Under the amendment that is zero expected header
# fields; under the phase 1 rule it was five that no extractor could ever win.
RENDERED_A = {
    "1:invoice_number": "M1234567",
    "1:line_date": "07/10/2026",
    "1:description": "Brake pads",
    "1:line_amount": "1,000.00",
    "2:invoice_number": "M7654321",
    "2:line_date": "07/18/2026",
    "2:description": "Oil filters",
    "2:line_amount": "250.00",
}

# A rendered pdf prints every populated truth field, so the phase 1 rule and the
# amended rule agree on this document. It anchors the ungated assertions below.
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
    "1:invoice_number": "M0000001",
    "1:line_date": "07/02/2026",
    "1:description": "Air filters",
    "1:line_amount": "75.00",
}

RENDERED_D = {
    "header:statement_number": "INC-2026-07-014",
    "header:statement_date": "2026-07-31",
    "header:subtotal": "$250.00",
    "1:program_code": "NS-AB123",
    "1:vin": VIN_D1,
    "1:line_date": "07/08/2026",
    "1:description": "Volume bonus Q3",
    "1:line_amount": "$300.00",
    "2:program_code": "NS-CD456",
    "2:vin": VIN_D2,
    "2:line_date": "07/22/2026",
    "2:description": "Chargeback adjustment",
    "2:line_amount": "($50.00)",
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


# Doc A extraction, worked by hand. Canonical / raw outcome per field:
#   statement_number  ok / no rendered key, so raw scores nothing
#   total             ok / no rendered key, so raw scores nothing
#   1:invoice_number  ok / wrong raw   1:line_date     ok / ok
#   1:description     WRONG / ok raw   1:line_amount   ok / ok
#   2:invoice_number  MISSING          2:line_date     MISSING
#   2:description     ok / ok          2:line_amount   WRONG (24999) / wrong raw
#   1:vin extracted but truth has no vin, so it maps to no truth field at all.
#
# The two header fields are the deliberate edge: the csv printed no header, so
# neither is expected, but the amendment changes only the denominator ("everything
# else in score_fields is unchanged"), so both still count as extracted and
# canonical-correct. fields_extracted may therefore exceed fields_expected.
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
        _field(DOC_A, FieldName.TOTAL, CSV, value_cents=145_000, raw_text="1,450.00"),
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

# Doc C extraction: the csv printed rows only, so only line fields come back and
# every one of them is canonical and raw correct.
EXTRACTED_C = ExtractedDocument(
    doc_id=DOC_C,
    file_path="files/doc-parts_statement-dlr-meridian-202607-02.csv",
    route=ExtractionRoute.CSV,
    doc_type=DocType.PARTS_STATEMENT,
    doc_type_confidence=1.0,
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

# Doc D extraction, worked by hand. Canonical / raw outcome per field:
#   statement_number ok / ok   statement_date ok / ok   subtotal ok / ok
#   1:program_code ok / ok     1:vin ok / ok            1:line_date ok / ok
#   1:description ok / ok      1:line_amount ok / ok
#   2:program_code ok / wrong raw (S read for 5)        2:vin MISSING
#   2:line_date ok / ok        2:description ok / ok    2:line_amount ok / ok
EXTRACTED_D = ExtractedDocument(
    doc_id=DOC_D,
    file_path="files/doc-incentive_statement-dlr-northstar-202607-01.xlsx",
    route=ExtractionRoute.XLSX,
    doc_type=DocType.INCENTIVE_STATEMENT,
    doc_type_confidence=1.0,
    header_fields=(
        _field(
            DOC_D,
            FieldName.STATEMENT_NUMBER,
            XLSX,
            value="INC-2026-07-014",
            raw_text="INC-2026-07-014",
        ),
        _field(
            DOC_D,
            FieldName.STATEMENT_DATE,
            XLSX,
            value_date=date(2026, 7, 31),
            raw_text="2026-07-31",
        ),
        _field(DOC_D, FieldName.SUBTOTAL, XLSX, value_cents=25_000, raw_text="$250.00"),
    ),
    line_fields=(
        _field(
            DOC_D,
            FieldName.PROGRAM_CODE,
            XLSX,
            line_no=1,
            value="NS-AB123",
            raw_text="NS-AB123",
        ),
        _field(DOC_D, FieldName.VIN, XLSX, line_no=1, value=VIN_D1, raw_text=VIN_D1),
        _field(
            DOC_D,
            FieldName.LINE_DATE,
            XLSX,
            line_no=1,
            value_date=date(2026, 7, 8),
            raw_text="07/08/2026",
        ),
        _field(
            DOC_D,
            FieldName.DESCRIPTION,
            XLSX,
            line_no=1,
            value="Volume bonus Q3",
            raw_text="Volume bonus Q3",
        ),
        _field(
            DOC_D,
            FieldName.LINE_AMOUNT,
            XLSX,
            line_no=1,
            value_cents=30_000,
            raw_text="$300.00",
        ),
        _field(
            DOC_D,
            FieldName.PROGRAM_CODE,
            XLSX,
            line_no=2,
            value="NS-CD456",
            raw_text="NS-CD4S6",
        ),
        _field(
            DOC_D,
            FieldName.LINE_DATE,
            XLSX,
            line_no=2,
            value_date=date(2026, 7, 22),
            raw_text="07/22/2026",
        ),
        _field(
            DOC_D,
            FieldName.DESCRIPTION,
            XLSX,
            line_no=2,
            value="Chargeback adjustment",
            raw_text="Chargeback adjustment",
        ),
        _field(
            DOC_D,
            FieldName.LINE_AMOUNT,
            XLSX,
            line_no=2,
            value_cents=-5_000,
            raw_text="($50.00)",
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
        _record(DOC_D, XLSX, TRUTH_D, RENDERED_D, SplitName.TRAIN),
    ),
)

# Doc B alone. It printed every populated truth field, so the phase 1 rule and the
# amended rule return the same numbers and these expectations hold either way.
AGREEING_MANIFEST = DatasetManifest(
    master_seed=7,
    generator_version="0.1.0",
    config_hash="2" * 64,
    records=(_record(DOC_B, CLEAN, TRUTH_B, RENDERED_B, SplitName.TRAIN),),
)

# Expected fields per document under the amended rule, counted off the rendered
# keys and then bucketed by family.
#
# Doc A (csv), 8 rendered keys, all line keys:
#   reference 1:invoice + 2:invoice                      = 2
#   date      1:line_date + 2:line_date                  = 2
#   amount    1:line_amount + 2:line_amount              = 2
#   text      1:description + 2:description              = 2
#   header statement_number/date/total/subtotal/previous_balance are populated in
#   truth but never printed, so the phase 1 count of 5 drops to 0.
#
# Doc B (clean_digital), 10 rendered keys:
#   reference header:statement_number + 1:claim + 1:ro    = 3
#   date      header:statement_date + 1:line_date         = 2
#   amount    header:total + header:subtotal
#             + header:previous_balance + 1:line_amount   = 4
#   text      1:description                               = 1
#
# Doc D (xlsx), 13 rendered keys:
#   reference header:statement_number + 1:program + 1:vin
#             + 2:program + 2:vin                         = 5
#   date      header:statement_date + 1:line_date + 2:line_date = 3
#   amount    header:subtotal + 1:line_amount + 2:line_amount   = 3
#             header:total and header:previous_balance are populated but unprinted
#   text      1:description + 2:description               = 2
#
# Extraction outcomes per cell, read off the two extraction tables above:
#
#   cell                     expected  extracted  canonical  raw
#   (reference, csv)         2         2          2          0
#   (date, csv)              2         1          1          1
#   (amount, csv)            2         3          2          1
#   (text, csv)              2         2          1          2
#   (reference, clean)       3         3          2          2
#   (date, clean)            2         1          1          1
#   (amount, clean)          4         3          3          3
#   (text, clean)            1         1          1          1
#   (reference, xlsx)        5         4          4          3
#   (date, xlsx)             3         3          3          3
#   (amount, xlsx)           3         3          3          3
#   (text, xlsx)             2         2          2          2
EXPECTED_TRAIN_CELLS: dict[tuple[FieldFamily, QualityTier], tuple[int, int, int, int]] = {
    (FieldFamily.REFERENCE, CSV): (2, 2, 2, 0),
    (FieldFamily.DATE, CSV): (2, 1, 1, 1),
    (FieldFamily.AMOUNT, CSV): (2, 3, 2, 1),
    (FieldFamily.TEXT, CSV): (2, 2, 1, 2),
    (FieldFamily.REFERENCE, CLEAN): (3, 3, 2, 2),
    (FieldFamily.DATE, CLEAN): (2, 1, 1, 1),
    (FieldFamily.AMOUNT, CLEAN): (4, 3, 3, 3),
    (FieldFamily.TEXT, CLEAN): (1, 1, 1, 1),
    (FieldFamily.REFERENCE, XLSX): (5, 4, 4, 3),
    (FieldFamily.DATE, XLSX): (3, 3, 3, 3),
    (FieldFamily.AMOUNT, XLSX): (3, 3, 3, 3),
    (FieldFamily.TEXT, XLSX): (2, 2, 2, 2),
}

# Doc B alone: the (family, clean_digital) rows of the table above.
EXPECTED_AGREEING_CELLS: dict[tuple[FieldFamily, QualityTier], tuple[int, int, int, int]] = {
    (FieldFamily.REFERENCE, CLEAN): (3, 3, 2, 2),
    (FieldFamily.DATE, CLEAN): (2, 1, 1, 1),
    (FieldFamily.AMOUNT, CLEAN): (4, 3, 3, 3),
    (FieldFamily.TEXT, CLEAN): (1, 1, 1, 1),
}

# Doc C alone (test split), 4 rendered line keys, all extracted correctly:
#   reference 1:invoice 1, date 1:line_date 1, amount 1:line_amount 1,
#   text 1:description 1. Its header truth fields were never printed.
EXPECTED_TEST_CELLS: dict[tuple[FieldFamily, QualityTier], tuple[int, int, int, int]] = {
    (FieldFamily.REFERENCE, CSV): (1, 1, 1, 1),
    (FieldFamily.DATE, CSV): (1, 1, 1, 1),
    (FieldFamily.AMOUNT, CSV): (1, 1, 1, 1),
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


def test_truth_fixtures_satisfy_composer_invariants() -> None:
    for doc in (TRUTH_A, TRUTH_B, TRUTH_C, TRUTH_D):
        assert doc.subtotal_cents == sum(line.amount_cents for line in doc.lines)
        assert doc.crossfoot_delta_cents() == 0


_HEADER_ATTRS: dict[FieldName, str] = {
    FieldName.STATEMENT_NUMBER: "statement_number",
    FieldName.STATEMENT_DATE: "statement_date",
    FieldName.TOTAL: "total_cents",
    FieldName.SUBTOTAL: "subtotal_cents",
    FieldName.PREVIOUS_BALANCE: "previous_balance_cents",
}
_LINE_ATTRS: dict[FieldName, str] = {
    FieldName.CLAIM_NUMBER: "claim_number",
    FieldName.RO_NUMBER: "ro_number",
    FieldName.VIN: "vin",
    FieldName.INVOICE_NUMBER: "invoice_number",
    FieldName.PROGRAM_CODE: "program_code",
    FieldName.LINE_DATE: "line_date",
    FieldName.LINE_AMOUNT: "amount_cents",
    FieldName.DESCRIPTION: "description",
}


def test_every_rendered_key_names_a_populated_truth_field() -> None:
    # The fixtures never print a key the truth doc has no value for, which is why
    # fields_present_in_artifact equals fields_expected in every cell below.
    for record in MANIFEST.records:
        truth = record.truth
        assert truth is not None
        for key in record.rendered_values:
            prefix, _, name = key.partition(":")
            field_name = FieldName(name)
            if prefix == "header":
                assert getattr(truth, _HEADER_ATTRS[field_name]) is not None, key
                continue
            line = next(ln for ln in truth.lines if ln.line_no == int(prefix))
            assert getattr(line, _LINE_ATTRS[field_name]) is not None, key


def test_score_fields_returns_a_sorted_tuple() -> None:
    # Sorted by (field_family, quality_tier) enum definition order: FieldFamily is
    # amount, date, reference, text and QualityTier puts clean_digital before csv
    # before xlsx. Which cells exist does not depend on the amendment here, because
    # every (family, tier) combo has expected fields under either rule.
    cells = metrics.score_fields([EXTRACTED_A, EXTRACTED_B, EXTRACTED_D], MANIFEST, SplitName.TRAIN)
    assert isinstance(cells, tuple)
    keys = [(cell.field_family, cell.quality_tier) for cell in cells]
    assert keys == [
        (FieldFamily.AMOUNT, CLEAN),
        (FieldFamily.AMOUNT, CSV),
        (FieldFamily.AMOUNT, XLSX),
        (FieldFamily.DATE, CLEAN),
        (FieldFamily.DATE, CSV),
        (FieldFamily.DATE, XLSX),
        (FieldFamily.REFERENCE, CLEAN),
        (FieldFamily.REFERENCE, CSV),
        (FieldFamily.REFERENCE, XLSX),
        (FieldFamily.TEXT, CLEAN),
        (FieldFamily.TEXT, CSV),
        (FieldFamily.TEXT, XLSX),
    ]


def test_score_fields_counts_a_fully_printed_document() -> None:
    # Doc B printed every populated truth field, so this table is the same under
    # the phase 1 rule and the amendment.
    cells = metrics.score_fields([EXTRACTED_B], AGREEING_MANIFEST, SplitName.TRAIN)
    assert isinstance(cells, tuple)
    _assert_field_cells(cells, EXPECTED_AGREEING_CELLS)


def test_doc_in_test_split_is_excluded_from_train_scoring() -> None:
    docs = [EXTRACTED_A, EXTRACTED_B, EXTRACTED_D]
    with_c = metrics.score_fields([*docs, EXTRACTED_C], MANIFEST, SplitName.TRAIN)
    without_c = metrics.score_fields(docs, MANIFEST, SplitName.TRAIN)
    assert _field_cells_by_key(with_c) == _field_cells_by_key(without_c)


def test_unextracted_train_doc_still_counts_expected_fields() -> None:
    # Doc B is in train but was never extracted: its printed truth fields stay in
    # the denominator with nothing extracted. Doc B printed all four of its amount
    # fields, so the count is 4 under either rule.
    cells = _field_cells_by_key(metrics.score_fields([EXTRACTED_A], MANIFEST, SplitName.TRAIN))
    cell = cells[(FieldFamily.AMOUNT, CLEAN)]
    assert cell.fields_expected == 4
    assert cell.fields_extracted == 0
    assert cell.correct_canonical == 0
    assert cell.correct_raw == 0


@requires_amendment
def test_score_fields_exact_train_counts() -> None:
    cells = metrics.score_fields(
        [EXTRACTED_A, EXTRACTED_B, EXTRACTED_C, EXTRACTED_D], MANIFEST, SplitName.TRAIN
    )
    assert isinstance(cells, tuple)
    _assert_field_cells(cells, EXPECTED_TRAIN_CELLS)


@requires_amendment
def test_test_split_scores_only_the_test_doc() -> None:
    cells = metrics.score_fields(
        [EXTRACTED_A, EXTRACTED_B, EXTRACTED_C, EXTRACTED_D], MANIFEST, SplitName.TEST
    )
    _assert_field_cells(cells, EXPECTED_TEST_CELLS)


@requires_amendment
def test_csv_document_expects_no_header_fields() -> None:
    # Isolated statement of the amendment: doc A's rendered_values holds no
    # "header:" key, so all five populated header truth fields drop out of the
    # denominator. Only the eight printed line fields remain: 2 per family.
    csv_only = DatasetManifest(
        master_seed=7,
        generator_version="0.1.0",
        config_hash="3" * 64,
        records=(_record(DOC_A, CSV, TRUTH_A, RENDERED_A, SplitName.TRAIN),),
    )
    cells = _field_cells_by_key(metrics.score_fields([EXTRACTED_A], csv_only, SplitName.TRAIN))
    for family in FieldFamily:
        assert cells[(family, CSV)].fields_expected == 2, family


@requires_amendment
def test_xlsx_document_expects_only_the_header_fields_it_printed() -> None:
    # Doc D printed statement_number, statement_date, and subtotal. Its total and
    # previous_balance are populated in truth but unprinted, so the amount
    # denominator is subtotal + two line amounts = 3, not 5.
    xlsx_only = DatasetManifest(
        master_seed=7,
        generator_version="0.1.0",
        config_hash="4" * 64,
        records=(_record(DOC_D, XLSX, TRUTH_D, RENDERED_D, SplitName.TRAIN),),
    )
    cells = _field_cells_by_key(metrics.score_fields([EXTRACTED_D], xlsx_only, SplitName.TRAIN))
    assert cells[(FieldFamily.AMOUNT, XLSX)].fields_expected == 3
    assert cells[(FieldFamily.REFERENCE, XLSX)].fields_expected == 5
    assert cells[(FieldFamily.DATE, XLSX)].fields_expected == 3
    assert cells[(FieldFamily.TEXT, XLSX)].fields_expected == 2


@requires_amendment
def test_document_that_printed_nothing_produces_no_cells() -> None:
    # An artifact that printed nothing has an empty denominator everywhere, and
    # cells are returned only for combos with fields_expected > 0.
    blank = DatasetManifest(
        master_seed=7,
        generator_version="0.1.0",
        config_hash="5" * 64,
        records=(_record(DOC_A, CSV, TRUTH_A, {}, SplitName.TRAIN),),
    )
    assert metrics.score_fields([EXTRACTED_A], blank, SplitName.TRAIN) == ()


def _present_in_artifact(cell: FieldAccuracyCell) -> int:
    # Read through model_dump so the assertion also proves the count is published
    # in the scorecard JSON, and so this file type checks before the field lands.
    value = cell.model_dump()["fields_present_in_artifact"]
    assert isinstance(value, int)
    return value


@requires_amendment
def test_fields_present_in_artifact_matches_the_new_denominator() -> None:
    # Every rendered key in these fixtures names a populated truth field, so the
    # published "present in artifact" count equals the amended denominator and a
    # reader can see it against the phase 1 numbers in the README methodology note.
    cells = metrics.score_fields(
        [EXTRACTED_A, EXTRACTED_B, EXTRACTED_C, EXTRACTED_D], MANIFEST, SplitName.TRAIN
    )
    by_key = _field_cells_by_key(cells)
    for key, (n_expected, _extracted, _canonical, _raw) in EXPECTED_TRAIN_CELLS.items():
        cell = by_key[key]
        assert _present_in_artifact(cell) == n_expected, key
        assert cell.fields_expected == n_expected, key


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
