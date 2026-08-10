"""What a workbook may cost once it is unzipped, and once a row is read.

A stat call sees the compressed size, which says nothing about what a container
holds. Measured against the extractor before these checks: a 747 kB workbook
whose shared strings inflate to 256 MB, a ratio of 343, took the process to
850 MB over 67 seconds and was then reported as merely unrecognizable. A 46 kB
sheet holding one row of 400,000 cells reached 425 MB the same way.

Both fixtures are written here as raw parts rather than through a writer, since
no writer emits either shape. The workbook skeleton is the smallest set of parts
openpyxl will open: the content types, the package relationships, the workbook
and its relationships, and one sheet.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest
from openpyxl import Workbook  # type: ignore[import-untyped]

from crossfoot.constants import ExtractionRoute, IngestErrorKind
from crossfoot.extraction import xlsx as xlsx_module
from crossfoot.extraction.tabular import MAX_FILE_BYTES
from crossfoot.extraction.xlsx import MAX_ROW_COLUMNS, extract_xlsx

DOC_ID = "doc-xlsx-hardening-01"
HEADER: tuple[object, ...] = ("Claim Number", "Date", "Description", "Amount")
DATA_ROW: tuple[object, ...] = ("NS12345678", "07/15/2026", "Alpha brake kit", "123.45")

_NAMESPACE = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_PACKAGE_RELS = "http://schemas.openxmlformats.org/package/2006/relationships"
_DOCUMENT_RELS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_DECLARATION = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'

_CONTENT_TYPES = (
    f'{_DECLARATION}<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.'
    'relationships+xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.'
    'openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
    '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.'
    'openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>'
)
_ROOT_RELS = (
    f'{_DECLARATION}<Relationships xmlns="{_PACKAGE_RELS}"><Relationship Id="rId1"'
    f' Type="{_DOCUMENT_RELS}/officeDocument" Target="xl/workbook.xml"/></Relationships>'
)
_WORKBOOK = (
    f'{_DECLARATION}<workbook xmlns="{_NAMESPACE}" xmlns:r="{_DOCUMENT_RELS}">'
    '<sheets><sheet name="S1" sheetId="1" r:id="rId1"/></sheets></workbook>'
)
_WORKBOOK_RELS = (
    f'{_DECLARATION}<Relationships xmlns="{_PACKAGE_RELS}"><Relationship Id="rId1"'
    f' Type="{_DOCUMENT_RELS}/worksheet" Target="worksheets/sheet1.xml"/></Relationships>'
)
_SHEET = (
    f'{_DECLARATION}<worksheet xmlns="{_NAMESPACE}"><sheetData>{{rows}}</sheetData></worksheet>'
)

# Written in chunks so the fixture never holds the inflated payload in memory.
_PAD_CHUNK = b"\0" * (1024 * 1024)


def _write_parts(path: Path, sheet_rows: str, *, padding_bytes: int = 0) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _CONTENT_TYPES)
        archive.writestr("_rels/.rels", _ROOT_RELS)
        archive.writestr("xl/workbook.xml", _WORKBOOK)
        archive.writestr("xl/_rels/workbook.xml.rels", _WORKBOOK_RELS)
        archive.writestr("xl/worksheets/sheet1.xml", _SHEET.format(rows=sheet_rows))
        if padding_bytes:
            with archive.open("xl/media/pad.bin", "w") as handle:
                for _ in range(padding_bytes // len(_PAD_CHUNK)):
                    handle.write(_PAD_CHUNK)
    return path


def _row_xml(cells: tuple[object, ...]) -> str:
    """One row of inline strings, which needs no shared string table to be valid."""
    inline = "".join(f'<c t="inlineStr"><is><t>{cell}</t></is></c>' for cell in cells)
    return f"<row>{inline}</row>"


def _workbook_with(path: Path, rows: list[tuple[object, ...]]) -> Path:
    book = Workbook()
    sheet = book.active
    for row in rows:
        sheet.append(list(row))
    book.save(path)
    return path


# ---------------------------------------------------------------------------
# The container: compressed size is not the size that matters
# ---------------------------------------------------------------------------


def test_a_container_that_inflates_past_the_budget_never_reaches_openpyxl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write_parts(
        tmp_path / "bomb.xlsx", _row_xml(HEADER), padding_bytes=MAX_FILE_BYTES + len(_PAD_CHUNK)
    )
    assert path.stat().st_size < MAX_FILE_BYTES  # a stat check alone would admit it

    def _forbidden(*args: object, **kwargs: object) -> object:
        raise AssertionError("a container over the inflated budget must not be opened")

    monkeypatch.setattr(xlsx_module, "load_workbook", _forbidden)
    doc = extract_xlsx(path, DOC_ID)

    assert doc.route is ExtractionRoute.UNPROCESSABLE
    assert doc.error is not None
    assert doc.error.kind is IngestErrorKind.TOO_LARGE
    assert "inflates" in doc.error.detail


def test_an_ordinary_workbook_is_not_refused_by_the_inflated_check(tmp_path: Path) -> None:
    path = _workbook_with(tmp_path / "ordinary.xlsx", [HEADER, DATA_ROW])
    doc = extract_xlsx(path, DOC_ID)
    assert doc.route is ExtractionRoute.XLSX
    assert doc.error is None
    assert doc.line_fields


def test_bytes_that_are_not_a_zip_are_still_unrecognizable(tmp_path: Path) -> None:
    """The inflated check cannot read them, and must not swallow the verdict either."""
    path = tmp_path / "not_a_zip.xlsx"
    path.write_bytes(b"Claim Number,Date,Description,Amount\nNS12345678,07/15/2026,Alpha,1.00\n")
    doc = extract_xlsx(path, DOC_ID)
    assert doc.route is ExtractionRoute.UNPROCESSABLE
    assert doc.error is not None
    assert doc.error.kind is IngestErrorKind.UNRECOGNIZED


# ---------------------------------------------------------------------------
# The row: rows and cell characters were capped, columns were not
# ---------------------------------------------------------------------------


def test_a_row_wider_than_the_column_cap_is_a_typed_error(tmp_path: Path) -> None:
    wide = tuple("x" for _ in range(MAX_ROW_COLUMNS + 1))
    path = _write_parts(tmp_path / "wide.xlsx", _row_xml(wide))
    doc = extract_xlsx(path, DOC_ID)

    assert doc.route is ExtractionRoute.UNPROCESSABLE
    assert doc.error is not None
    assert doc.error.kind is IngestErrorKind.TOO_LARGE
    assert str(MAX_ROW_COLUMNS) in doc.error.detail


def test_the_wide_row_refusal_is_cheap_to_reach(tmp_path: Path) -> None:
    """The attack is small on disk: the cost is what reading it would have been."""
    wide = tuple("x" for _ in range(MAX_ROW_COLUMNS * 8))
    path = _write_parts(tmp_path / "wider.xlsx", _row_xml(wide))
    assert path.stat().st_size < MAX_FILE_BYTES
    doc = extract_xlsx(path, DOC_ID)
    assert doc.error is not None
    assert doc.error.kind is IngestErrorKind.TOO_LARGE


def test_a_row_at_the_column_cap_is_read(tmp_path: Path) -> None:
    padding = tuple("" for _ in range(MAX_ROW_COLUMNS - len(HEADER)))
    path = _write_parts(
        tmp_path / "at_cap.xlsx", _row_xml(HEADER + padding) + _row_xml(DATA_ROW + padding)
    )
    doc = extract_xlsx(path, DOC_ID)
    assert doc.route is ExtractionRoute.XLSX
    assert doc.error is None
    assert doc.line_fields
