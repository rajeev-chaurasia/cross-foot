"""Hostile and malformed CSV input: limits, isolation, encodings, duplicates.

The contract suite covers the happy paths; these tests cover what a real inbox
delivers when someone is careless or hostile.
"""

from pathlib import Path

import pytest

from crossfoot.constants import (
    ExtractionRoute,
    FieldName,
    IngestErrorKind,
    ReviewStatus,
)
from crossfoot.extraction.tabular import (
    MAX_DATA_ROWS,
    MAX_FILE_BYTES,
    extract_csv,
)
from crossfoot.models.extraction import ExtractedDocument

DOC_ID = "doc-hardening-01"
HEADER = "Claim Number,Date,Description,Amount"
GOOD_ROW = "NS12345678,07/15/2026,Alpha brake kit,123.45"


def _write(path: Path, text: str, encoding: str = "utf-8") -> Path:
    path.write_bytes(text.encode(encoding))
    return path


def _amounts(doc: ExtractedDocument) -> list[str]:
    return [field.field_id for field in doc.line_fields if field.name is FieldName.LINE_AMOUNT]


# C2: two columns mapping to one field name.


def test_duplicate_header_columns_yield_one_field_per_name(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "duplicate_headers.csv",
        "Amount,Amt,Date,Description\n11.11,22.22,07/15/2026,Alpha\n",
    )
    doc = extract_csv(path, DOC_ID)
    assert doc.route is ExtractionRoute.CSV
    assert len(_amounts(doc)) == 1
    assert len({field.field_id for field in doc.line_fields}) == len(doc.line_fields)


def test_duplicate_header_columns_take_the_first_column(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "duplicate_first_wins.csv",
        "Amount,Amt,Date,Description\n11.11,22.22,07/15/2026,Alpha\n",
    )
    doc = extract_csv(path, DOC_ID)
    amount = next(field for field in doc.line_fields if field.name is FieldName.LINE_AMOUNT)
    assert amount.value_cents == 1_111


# S2: resource limits.


def test_oversize_file_is_rejected_by_stat(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "huge.csv"
    with path.open("wb") as handle:
        handle.truncate(MAX_FILE_BYTES + 1)

    def _forbidden(self: Path) -> bytes:
        raise AssertionError("oversize file must be rejected before it is read")

    monkeypatch.setattr(Path, "read_bytes", _forbidden)
    doc = extract_csv(path, DOC_ID)
    assert doc.route is ExtractionRoute.UNPROCESSABLE
    assert doc.error is not None
    assert doc.error.kind is IngestErrorKind.TOO_LARGE


def test_row_cap_is_enforced(tmp_path: Path) -> None:
    rows = "\n".join([HEADER, *[GOOD_ROW] * (MAX_DATA_ROWS + 1)])
    doc = extract_csv(_write(tmp_path / "too_many_rows.csv", rows), DOC_ID)
    assert doc.route is ExtractionRoute.UNPROCESSABLE
    assert doc.error is not None
    assert doc.error.kind is IngestErrorKind.TOO_LARGE
    assert str(MAX_DATA_ROWS) in doc.error.detail


def test_row_cap_boundary_still_extracts(tmp_path: Path) -> None:
    rows = "\n".join([HEADER, *[GOOD_ROW] * MAX_DATA_ROWS])
    doc = extract_csv(_write(tmp_path / "at_the_cap.csv", rows), DOC_ID)
    assert doc.route is ExtractionRoute.CSV
    assert len({field.line_no for field in doc.line_fields}) == MAX_DATA_ROWS


def test_one_giant_cell_is_a_typed_error_not_an_exception(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "giant_cell.csv",
        f'{HEADER}\nNS12345678,07/15/2026,"{"x" * 200_000}",123.45\n',
    )
    doc = extract_csv(path, DOC_ID)
    assert doc.route is ExtractionRoute.UNPROCESSABLE
    assert doc.error is not None


# S3: one bad cell must not void the document.


def test_absurd_amount_cell_needs_review_and_the_rest_survives(tmp_path: Path) -> None:
    good_rows = [GOOD_ROW] * 500
    good_rows[249] = f"NS12345678,07/15/2026,Bravo core return,{'9' * 5_000}"
    doc = extract_csv(
        _write(tmp_path / "one_bad_cell.csv", "\n".join([HEADER, *good_rows])), DOC_ID
    )
    assert doc.route is ExtractionRoute.CSV
    assert len({field.line_no for field in doc.line_fields}) == 500
    needs_review = [
        field
        for field in doc.line_fields
        if field.name is FieldName.LINE_AMOUNT and field.status is ReviewStatus.NEEDS_REVIEW
    ]
    assert len(needs_review) == 1
    assert needs_review[0].line_no == 250
    assert needs_review[0].confidence == 0.0
    assert needs_review[0].value_cents is None


# S4: byte order marks and mixed encodings.


def test_utf8_bom_keeps_the_first_column(tmp_path: Path) -> None:
    path = _write(tmp_path / "bom_utf8.csv", f"\ufeff{HEADER}\n{GOOD_ROW}\n")
    doc = extract_csv(path, DOC_ID)
    assert {field.name for field in doc.line_fields} == {
        FieldName.CLAIM_NUMBER,
        FieldName.LINE_DATE,
        FieldName.DESCRIPTION,
        FieldName.LINE_AMOUNT,
    }


def test_bom_bytes_over_cp1252_content_keep_the_first_column(tmp_path: Path) -> None:
    path = tmp_path / "bom_cp1252.csv"
    body = f"{HEADER}\nNS12345678,07/15/2026,Alpha 90° elbow,123.45\n"
    path.write_bytes(b"\xef\xbb\xbf" + body.encode("cp1252"))
    doc = extract_csv(path, DOC_ID)
    assert FieldName.CLAIM_NUMBER in {field.name for field in doc.line_fields}
    assert len({field.name for field in doc.line_fields}) == 4


def test_mixed_encoding_file_maps_every_column(tmp_path: Path) -> None:
    path = tmp_path / "mixed.csv"
    header = f"\ufeff{HEADER}\n".encode()
    path.write_bytes(header + "NS12345678,07/15/2026,Café filter,123.45\n".encode("cp1252"))
    doc = extract_csv(path, DOC_ID)
    assert doc.route is ExtractionRoute.CSV
    assert len({field.name for field in doc.line_fields}) == 4


# S5: control characters never reach a value.


def test_nul_bytes_are_stripped_from_values(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "nul_bytes.csv",
        f"{HEADER}\nNS\x0012345678,07/15/2026,Alpha\x00brake,12\x003.45\n",
    )
    doc = extract_csv(path, DOC_ID)
    for field in doc.line_fields:
        assert field.raw_text is not None
        assert "\x00" not in field.raw_text
        assert field.value is None or "\x00" not in field.value
    amount = next(field for field in doc.line_fields if field.name is FieldName.LINE_AMOUNT)
    assert amount.value_cents == 12_345
