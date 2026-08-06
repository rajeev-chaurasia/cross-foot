"""Contract tests for crossfoot.extraction.tabular.extract_csv.

Written against docs/contracts-phase1.md before the implementation exists.
Fixtures live in tests/fixtures/csv/ and are committed byte-exact: one clean
utf-8 file, one pipe-delimited cp1252 file with synonym headers, one file of
hostile amount formats, one with junk preamble rows, and one binary junk file
saved with a .csv extension.
"""

from datetime import date
from pathlib import Path

import pytest

from crossfoot.constants import ExtractionRoute, FieldName, ReviewStatus
from crossfoot.models.extraction import ExtractedDocument, ExtractedField, IngestError

tabular = pytest.importorskip("crossfoot.extraction.tabular")

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "csv"
DOC_ID = "doc-test-01"


def extract(name: str) -> ExtractedDocument:
    doc = tabular.extract_csv(FIXTURES / name, doc_id=DOC_ID)
    assert isinstance(doc, ExtractedDocument)
    return doc


def line_field(doc: ExtractedDocument, line_no: int, name: FieldName) -> ExtractedField:
    matches = [f for f in doc.line_fields if f.line_no == line_no and f.name == name]
    assert len(matches) == 1, f"expected exactly one {name} at line {line_no}"
    return matches[0]


# clean_standard.csv: utf-8, comma delimiter, canonical headers, 3 rows.


def test_clean_standard_routes_as_csv() -> None:
    doc = extract("clean_standard.csv")
    assert doc.doc_id == DOC_ID
    assert doc.route is ExtractionRoute.CSV
    assert doc.error is None


def test_clean_standard_has_three_lines() -> None:
    doc = extract("clean_standard.csv")
    assert {f.line_no for f in doc.line_fields} == {1, 2, 3}


def test_clean_standard_amounts_in_cents() -> None:
    doc = extract("clean_standard.csv")
    # 1234.56 -> 123456, 45.00 -> 4500, 89.99 -> 8999
    assert line_field(doc, 1, FieldName.LINE_AMOUNT).value_cents == 123_456
    assert line_field(doc, 2, FieldName.LINE_AMOUNT).value_cents == 4_500
    assert line_field(doc, 3, FieldName.LINE_AMOUNT).value_cents == 8_999


def test_clean_standard_parses_both_date_formats() -> None:
    doc = extract("clean_standard.csv")
    # row 1 is 07/15/2026, row 2 is ISO 2026-07-16
    assert line_field(doc, 1, FieldName.LINE_DATE).value_date == date(2026, 7, 15)
    assert line_field(doc, 2, FieldName.LINE_DATE).value_date == date(2026, 7, 16)


def test_clean_standard_references_land_in_the_right_fields() -> None:
    doc = extract("clean_standard.csv")
    assert line_field(doc, 1, FieldName.CLAIM_NUMBER).value == "4821A00551"
    assert line_field(doc, 1, FieldName.RO_NUMBER).value == "RO000123"
    assert line_field(doc, 1, FieldName.VIN).value == "1FTFW1ET9DFC10312"
    assert line_field(doc, 3, FieldName.CLAIM_NUMBER).value == "4821A00553"


# synonyms_pipe_cp1252.csv: pipe delimiter, cp1252 encoding, synonym headers
# CLAIM_NO | RepairOrder | Vin # | Post Dt | Desc | Amt.


def test_synonym_headers_map_to_field_names() -> None:
    doc = extract("synonyms_pipe_cp1252.csv")
    assert doc.route is ExtractionRoute.CSV
    assert line_field(doc, 1, FieldName.CLAIM_NUMBER).value == "NS12345678"
    assert line_field(doc, 1, FieldName.RO_NUMBER).value == "123456"
    assert line_field(doc, 1, FieldName.VIN).value == "1FTFW1ET9DFC10312"


def test_cp1252_description_decodes() -> None:
    doc = extract("synonyms_pipe_cp1252.csv")
    value = line_field(doc, 1, FieldName.DESCRIPTION).value
    assert value is not None
    # degree sign, stored as the single byte 0xB0 in cp1252
    assert "°" in value


def test_pipe_delimited_amounts_and_dates() -> None:
    doc = extract("synonyms_pipe_cp1252.csv")
    # 450.00 -> 45000, 89.10 -> 8910
    assert line_field(doc, 1, FieldName.LINE_AMOUNT).value_cents == 45_000
    assert line_field(doc, 2, FieldName.LINE_AMOUNT).value_cents == 8_910
    assert line_field(doc, 1, FieldName.LINE_DATE).value_date == date(2026, 7, 15)
    assert line_field(doc, 2, FieldName.LINE_DATE).value_date == date(2026, 7, 20)


# nasty_amounts.csv: currency symbols, thousands commas, two negative
# notations, and one blank amount cell.


def test_nasty_amounts_parse_to_signed_cents() -> None:
    doc = extract("nasty_amounts.csv")
    # "$1,234.56" -> 123456; "(123.45)" -> -12345; "123.45-" -> -12345
    assert line_field(doc, 1, FieldName.LINE_AMOUNT).value_cents == 123_456
    assert line_field(doc, 2, FieldName.LINE_AMOUNT).value_cents == -12_345
    assert line_field(doc, 3, FieldName.LINE_AMOUNT).value_cents == -12_345


def test_blank_amount_cell_needs_review_with_zero_confidence() -> None:
    doc = extract("nasty_amounts.csv")
    field = line_field(doc, 4, FieldName.LINE_AMOUNT)
    assert field.value_cents is None
    assert field.confidence == 0.0
    assert field.status is ReviewStatus.NEEDS_REVIEW


# junk_preamble.csv: two junk title rows before the real header row.


def test_junk_preamble_rows_are_not_data() -> None:
    doc = extract("junk_preamble.csv")
    assert doc.route is ExtractionRoute.CSV
    assert {f.line_no for f in doc.line_fields} == {1, 2}


def test_junk_preamble_values_come_from_real_rows() -> None:
    doc = extract("junk_preamble.csv")
    assert line_field(doc, 1, FieldName.CLAIM_NUMBER).value == "4821A00551"
    assert line_field(doc, 1, FieldName.LINE_AMOUNT).value_cents == 123_456
    assert line_field(doc, 2, FieldName.LINE_AMOUNT).value_cents == 8_999
    assert line_field(doc, 2, FieldName.LINE_DATE).value_date == date(2026, 7, 16)


# garbage.csv: non-tabular bytes behind a csv extension.


def test_garbage_returns_unprocessable_instead_of_raising() -> None:
    doc = extract("garbage.csv")
    assert doc.doc_id == DOC_ID
    assert doc.route is ExtractionRoute.UNPROCESSABLE
    assert isinstance(doc.error, IngestError)
