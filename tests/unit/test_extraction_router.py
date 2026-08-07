"""The router reads bytes, not names: a CSV called .pdf is still a CSV."""

import zipfile
from pathlib import Path

import pytest
from pdf_fixtures import TRUTH_DOC, minimal_pdf, statement_items

from crossfoot.constants import CorruptionKind, ExtractionRoute, IngestErrorKind
from crossfoot.extraction.router import route_file
from crossfoot.generator.corrupt import build_minimal_pdf, write_corrupted
from crossfoot.generator.renderers.tabular import render_xlsx

CSV_TEXT = "Claim Number,Date,Description,Amount\nNS12345678,07/15/2026,Alpha,123.45\n"


def _write(tmp_path: Path, name: str, data: bytes) -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


def test_delimited_text_routes_to_csv(tmp_path: Path) -> None:
    path = _write(tmp_path, "export.csv", CSV_TEXT.encode("utf-8"))
    assert route_file(path).route is ExtractionRoute.CSV


def test_csv_bytes_under_a_pdf_name_still_route_to_csv(tmp_path: Path) -> None:
    path = tmp_path / "misfiled.pdf"
    write_corrupted(CorruptionKind.WRONG_EXTENSION, 7, path)
    assert route_file(path).route is ExtractionRoute.CSV


def test_pdf_with_a_text_layer_routes_to_digital(tmp_path: Path) -> None:
    path = _write(tmp_path, "digital.pdf", minimal_pdf(statement_items(TRUTH_DOC)))
    assert route_file(path).route is ExtractionRoute.DIGITAL_PDF


def test_pdf_without_a_text_layer_routes_to_scanned(tmp_path: Path) -> None:
    path = _write(tmp_path, "scan.pdf", build_minimal_pdf("x"))
    assert route_file(path).route is ExtractionRoute.SCANNED_PDF


def test_workbook_routes_to_xlsx(tmp_path: Path) -> None:
    path = tmp_path / "book.xlsx"
    render_xlsx(TRUTH_DOC, "meridian-parts_statement-xlsx-v1", 3, path)
    assert route_file(path).route is ExtractionRoute.XLSX


def test_plain_zip_is_not_a_workbook(tmp_path: Path) -> None:
    path = tmp_path / "archive.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("notes.txt", "not a workbook")
    routing = route_file(path)
    assert routing.route is ExtractionRoute.UNPROCESSABLE
    assert routing.error is not None
    assert routing.error.kind is IngestErrorKind.UNRECOGNIZED


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        (CorruptionKind.EMPTY_FILE, IngestErrorKind.EMPTY),
        (CorruptionKind.TRUNCATED_PDF, IngestErrorKind.TRUNCATED),
        (CorruptionKind.ENCRYPTED_PDF, IngestErrorKind.ENCRYPTED),
        (CorruptionKind.BINARY_JUNK, IngestErrorKind.UNRECOGNIZED),
    ],
)
def test_corrupted_files_carry_a_typed_error(
    tmp_path: Path, kind: CorruptionKind, expected: IngestErrorKind
) -> None:
    path = tmp_path / f"{kind}.pdf"
    write_corrupted(kind, 11, path)
    routing = route_file(path)
    assert routing.route is ExtractionRoute.UNPROCESSABLE
    assert routing.error is not None
    assert routing.error.kind is expected


def test_missing_file_is_unprocessable_rather_than_an_exception(tmp_path: Path) -> None:
    routing = route_file(tmp_path / "absent.csv")
    assert routing.route is ExtractionRoute.UNPROCESSABLE
    assert routing.error is not None
    assert routing.error.kind is IngestErrorKind.UNRECOGNIZED
