"""The digital-pdf extractor: row geometry, column spans, and word-box crops."""

from datetime import date
from pathlib import Path

from pdf_fixtures import PAGE_HEIGHT, PAGE_WIDTH, TRUTH_DOC, minimal_pdf, statement_items

from crossfoot.constants import CropKind, ExtractionRoute, FieldName, IngestErrorKind
from crossfoot.extraction.pdf_text import (
    PageWords,
    Word,
    extract_pdf,
    read_pages,
    rows_of,
    union_bbox,
)
from crossfoot.models.extraction import ExtractedDocument

PAGE = PageWords(page=0, width=100.0, height=200.0, words=())


def _word(text: str, x0: float, top: float, height: float = 10.0) -> Word:
    return Word(text=text, x0=x0, x1=x0 + 20.0, top=top, bottom=top + height, page=0)


def _extract(tmp_path: Path) -> ExtractedDocument:
    path = tmp_path / "statement.pdf"
    path.write_bytes(minimal_pdf(statement_items(TRUTH_DOC)))
    return extract_pdf(path, TRUTH_DOC.doc_id)


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------


def test_separated_lines_stay_separate_rows() -> None:
    rows = rows_of([_word("a", 0, 0), _word("b", 0, 40)])
    assert [[word.text for word in row] for row in rows] == [["a"], ["b"]]


def test_a_row_is_ordered_left_to_right() -> None:
    rows = rows_of([_word("right", 50, 0), _word("left", 10, 0)])
    assert [word.text for word in rows[0]] == ["left", "right"]


def test_a_wrapped_caption_joins_the_row_it_straddles() -> None:
    # "PROGRAM" prints above the single-line captions and "CODE" below them, so
    # all three lines are one header row and the caption reads as one phrase.
    rows = rows_of(
        [
            _word("PROGRAM", 60, 244, height=6.3),
            _word("LN", 40, 248.5, height=6.3),
            _word("AMOUNT", 500, 248.5, height=6.3),
            _word("CODE", 60, 252.3, height=6.3),
        ]
    )
    assert len(rows) == 1
    assert [word.text for word in rows[0]] == ["LN", "PROGRAM", "CODE", "AMOUNT"]


def test_union_bbox_is_padded_and_normalized() -> None:
    box = union_bbox([_word("x", 10, 20), _word("y", 40, 20)], PAGE)
    assert box is not None
    assert 0.0 < box.x0 < 10 / PAGE.width
    assert box.x1 > 60 / PAGE.width
    assert box.y0 < 20 / PAGE.height < box.y1


def test_union_bbox_of_nothing_is_none() -> None:
    assert union_bbox([], PAGE) is None


# ---------------------------------------------------------------------------
# End to end over a hand-built PDF
# ---------------------------------------------------------------------------


def test_read_pages_reports_page_geometry(tmp_path: Path) -> None:
    path = tmp_path / "statement.pdf"
    path.write_bytes(minimal_pdf(statement_items(TRUTH_DOC)))
    pages = read_pages(path)
    assert len(pages) == 1
    assert (pages[0].width, pages[0].height) == (PAGE_WIDTH, PAGE_HEIGHT)
    assert pages[0].words


def test_header_fields_come_off_their_labels(tmp_path: Path) -> None:
    doc = _extract(tmp_path)
    header = {field.name: field for field in doc.header_fields}
    assert header[FieldName.STATEMENT_NUMBER].value == "PS-2026-07-001"
    assert header[FieldName.STATEMENT_DATE].value_date == date(2026, 7, 31)
    assert header[FieldName.PREVIOUS_BALANCE].value_cents == 20_000
    assert header[FieldName.SUBTOTAL].value_cents == 125_000
    assert header[FieldName.TOTAL].value_cents == 145_000


def test_line_rows_are_numbered_in_reading_order(tmp_path: Path) -> None:
    doc = _extract(tmp_path)
    amounts = {
        field.line_no: field.value_cents
        for field in doc.line_fields
        if field.name is FieldName.LINE_AMOUNT
    }
    assert amounts == {1: 100_000, 2: 25_000}


def test_line_references_land_in_their_own_column(tmp_path: Path) -> None:
    doc = _extract(tmp_path)
    invoices = {
        field.line_no: field.value
        for field in doc.line_fields
        if field.name is FieldName.INVOICE_NUMBER
    }
    assert invoices == {1: "M1234567", 2: "M7654321"}


def test_totals_block_is_not_read_as_a_line_row(tmp_path: Path) -> None:
    doc = _extract(tmp_path)
    line_numbers = {field.line_no for field in doc.line_fields}
    assert line_numbers == {1, 2}


def test_document_crossfoots(tmp_path: Path) -> None:
    doc = _extract(tmp_path)
    assert doc.crossfoot_delta_cents == 0
    assert doc.route is ExtractionRoute.DIGITAL_PDF


def test_every_field_carries_an_exact_crop(tmp_path: Path) -> None:
    doc = _extract(tmp_path)
    for field in (*doc.header_fields, *doc.line_fields):
        assert field.crop_kind is CropKind.EXACT_BBOX, field.field_id
        assert field.bbox is not None, field.field_id
        assert 0.0 <= field.bbox.x0 < field.bbox.x1 <= 1.0
        assert 0.0 <= field.bbox.y0 < field.bbox.y1 <= 1.0


def test_a_damaged_pdf_is_unprocessable_rather_than_an_exception(tmp_path: Path) -> None:
    path = tmp_path / "broken.pdf"
    whole = minimal_pdf(statement_items(TRUTH_DOC))
    path.write_bytes(whole[: len(whole) // 2])
    doc = extract_pdf(path, "doc-broken")
    assert doc.route is ExtractionRoute.UNPROCESSABLE
    assert doc.error is not None
    assert doc.error.kind is IngestErrorKind.TRUNCATED
