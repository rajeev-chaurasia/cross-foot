"""Rendered-values completeness and template health for every renderer."""

from datetime import date
from pathlib import Path

import pdfplumber
import pytest
from openpyxl import load_workbook  # type: ignore[import-untyped]

from crossfoot.constants import CSV_HEADER_SYNONYMS, DocType, FieldName, LineType, Oem
from crossfoot.generator.renderers.base import (
    DOC_LINE_FIELDS,
    MARQUE_BRANDING,
    header_key,
    line_key,
)
from crossfoot.generator.renderers.chromium import ChromiumPdfRenderer, render_html
from crossfoot.generator.renderers.tabular import render_csv, render_xlsx
from crossfoot.models.statement import StatementDoc, StatementLine

TEMPLATE_OEMS = (Oem.MERIDIAN, Oem.NORTHSTAR, Oem.KAIZEN)
RENDER_SEED = 11

# Known-good ISO 3779 VIN (check digit X at position 9).
SAMPLE_VIN = "1M8GDM9AXKP042788"


def make_doc(oem: Oem, doc_type: DocType) -> StatementDoc:
    """Two-line statement respecting the crossfoot invariant, all references set."""
    lines = (
        StatementLine(
            line_no=1,
            line_type=LineType.CHARGE,
            claim_number="1234A56789",
            ro_number="RO123456",
            vin=SAMPLE_VIN,
            invoice_number="M1234567",
            program_code="PGM-0001",
            line_date=date(2026, 7, 6),
            description="Alpha brake kit restock",
            amount_cents=123_456,
            source_entry_id="led-parts_payable-00001",
        ),
        StatementLine(
            line_no=2,
            line_type=LineType.CREDIT,
            claim_number="1234A56790",
            ro_number="RO123457",
            vin=SAMPLE_VIN,
            invoice_number="M1234568",
            program_code="PGM-0002",
            line_date=date(2026, 7, 21),
            description="Bravo core return credit",
            amount_cents=-2_050,
            source_entry_id="led-parts_payable-00002",
        ),
    )
    subtotal_cents = sum(line.amount_cents for line in lines)
    previous_balance_cents = 55_000
    return StatementDoc(
        doc_id=f"doc-{doc_type}-dlr-{oem}-202607-01",
        dealer_id=f"dlr-{oem}",
        doc_type=doc_type,
        oem=oem,
        statement_number="STMT-202607-01",
        statement_date=date(2026, 7, 31),
        period_start=date(2026, 7, 1),
        period_end=date(2026, 7, 31),
        previous_balance_cents=previous_balance_cents,
        subtotal_cents=subtotal_cents,
        adjustments_cents=0,
        total_cents=previous_balance_cents + subtotal_cents,
        lines=lines,
    )


def _expected_line_keys(doc: StatementDoc) -> set[str]:
    return {
        line_key(line.line_no, field)
        for line in doc.lines
        for field in DOC_LINE_FIELDS[doc.doc_type]
    }


def _expected_pdf_keys(doc: StatementDoc) -> set[str]:
    return _expected_line_keys(doc) | {
        header_key(FieldName.STATEMENT_NUMBER),
        header_key(FieldName.STATEMENT_DATE),
        header_key(FieldName.SUBTOTAL),
        header_key(FieldName.TOTAL),
        header_key(FieldName.PREVIOUS_BALANCE),  # fixture always carries one
    }


@pytest.mark.parametrize("oem", TEMPLATE_OEMS)
@pytest.mark.parametrize("doc_type", tuple(DocType))
def test_every_template_renders_all_values(oem: Oem, doc_type: DocType) -> None:
    doc = make_doc(oem, doc_type)
    html, rendered = render_html(doc, f"{oem}-{doc_type}-v1")
    assert set(rendered) == _expected_pdf_keys(doc)
    for key, value in rendered.items():
        assert value in html, f"{key} value {value!r} missing from HTML"
    assert MARQUE_BRANDING[oem].name in html


def test_chromium_pdf_carries_text_layer_and_rendered_values(tmp_path: Path) -> None:
    doc = make_doc(Oem.KAIZEN, DocType.WARRANTY_CREDIT_MEMO)
    out_path = tmp_path / "warranty.pdf"
    with ChromiumPdfRenderer() as renderer:
        rendered = renderer.render(doc, "kaizen-warranty_credit_memo-v1", RENDER_SEED, out_path)
    assert set(rendered) == _expected_pdf_keys(doc)
    with pdfplumber.open(out_path) as pdf:
        text = "\n".join(page.extract_text() or "" for page in pdf.pages)
    assert doc.statement_number in text
    assert doc.lines[0].claim_number is not None
    assert doc.lines[0].claim_number in text
    assert rendered[header_key(FieldName.TOTAL)] in text


def test_render_csv_covers_lines_in_truth_order(tmp_path: Path) -> None:
    doc = make_doc(Oem.MERIDIAN, DocType.PARTS_STATEMENT)
    out_path = tmp_path / "parts.csv"
    rendered = render_csv(doc, "meridian-parts_statement-csv-v1", RENDER_SEED, out_path)
    assert set(rendered) == _expected_line_keys(doc)
    # All content is ASCII, so both permitted encodings decode identically.
    text = out_path.read_bytes().decode("utf-8")
    first_line = text.splitlines()[0]
    assert any(synonym in first_line for synonym in CSV_HEADER_SYNONYMS[FieldName.INVOICE_NUMBER])
    for key, value in rendered.items():
        assert value in text, f"{key} value {value!r} missing from CSV"
    assert text.index("Alpha brake kit restock") < text.index("Bravo core return credit")


def test_render_csv_is_deterministic_per_seed(tmp_path: Path) -> None:
    doc = make_doc(Oem.NORTHSTAR, DocType.WARRANTY_CREDIT_MEMO)
    first = tmp_path / "first.csv"
    second = tmp_path / "second.csv"
    render_csv(doc, "northstar-warranty_credit_memo-csv-v1", RENDER_SEED, first)
    render_csv(doc, "northstar-warranty_credit_memo-csv-v1", RENDER_SEED, second)
    assert first.read_bytes() == second.read_bytes()


def test_render_xlsx_covers_values_and_layout(tmp_path: Path) -> None:
    doc = make_doc(Oem.NORTHSTAR, DocType.INCENTIVE_STATEMENT)
    out_path = tmp_path / "incentive.xlsx"
    rendered = render_xlsx(doc, "northstar-incentive_statement-xlsx-v1", RENDER_SEED, out_path)
    expected = _expected_line_keys(doc) | {
        header_key(FieldName.STATEMENT_NUMBER),
        header_key(FieldName.STATEMENT_DATE),
        header_key(FieldName.SUBTOTAL),
    }
    assert set(rendered) == expected

    book = load_workbook(out_path)
    sheet = book.active
    assert sheet["A1"].value == MARQUE_BRANDING[Oem.NORTHSTAR].name
    assert sheet["B3"].value == doc.statement_number
    # XLSX orders reference columns first; incentive leads with the program code.
    assert sheet["A6"].value in CSV_HEADER_SYNONYMS[FieldName.PROGRAM_CODE]
    first_column = [row[0].value for row in sheet.iter_rows(min_col=1, max_col=1)]
    assert "TOTALS" in first_column
    all_values = {cell.value for row in sheet.iter_rows() for cell in row if cell.value is not None}
    assert "Alpha brake kit restock" in all_values
