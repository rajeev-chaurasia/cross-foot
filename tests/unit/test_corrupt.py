"""Each corruption kind carries its signature failure property."""

import csv
import io
from pathlib import Path

import pytest
from pypdf import PdfReader
from pypdf.errors import PdfReadError

from crossfoot.constants import CorruptionKind
from crossfoot.generator.corrupt import (
    JUNK_SIZE_BYTES,
    KNOWN_MAGIC_PREFIXES,
    build_minimal_pdf,
    write_corrupted,
)

SEED = 11


def test_truncated_pdf_keeps_header_but_is_unreadable(tmp_path: Path) -> None:
    out_path = tmp_path / "truncated.pdf"
    write_corrupted(CorruptionKind.TRUNCATED_PDF, SEED, out_path)
    data = out_path.read_bytes()
    assert data.startswith(b"%PDF")
    assert b"%%EOF" not in data
    assert 0 < len(data) < len(build_minimal_pdf(f"synthetic statement seed {SEED}"))
    with pytest.raises(PdfReadError):
        PdfReader(io.BytesIO(data))


def test_wrong_extension_is_csv_text_under_a_pdf_name(tmp_path: Path) -> None:
    out_path = tmp_path / "mislabeled.pdf"
    write_corrupted(CorruptionKind.WRONG_EXTENSION, SEED, out_path)
    data = out_path.read_bytes()
    assert not data.startswith(b"%PDF")
    rows = list(csv.reader(io.StringIO(data.decode("utf-8"))))
    assert rows[0] == ["Date", "Description", "Amount"]
    assert len(rows) > 1


def test_empty_file_is_zero_bytes(tmp_path: Path) -> None:
    out_path = tmp_path / "empty.pdf"
    write_corrupted(CorruptionKind.EMPTY_FILE, SEED, out_path)
    assert out_path.stat().st_size == 0


def test_encrypted_pdf_reports_encryption(tmp_path: Path) -> None:
    out_path = tmp_path / "locked.pdf"
    write_corrupted(CorruptionKind.ENCRYPTED_PDF, SEED, out_path)
    assert PdfReader(out_path).is_encrypted


def test_binary_junk_has_no_recognizable_magic_and_is_seed_stable(tmp_path: Path) -> None:
    first = tmp_path / "junk-a.bin"
    second = tmp_path / "junk-b.bin"
    other_seed = tmp_path / "junk-c.bin"
    write_corrupted(CorruptionKind.BINARY_JUNK, SEED, first)
    write_corrupted(CorruptionKind.BINARY_JUNK, SEED, second)
    write_corrupted(CorruptionKind.BINARY_JUNK, SEED + 1, other_seed)
    data = first.read_bytes()
    assert len(data) == JUNK_SIZE_BYTES
    assert not data.startswith(KNOWN_MAGIC_PREFIXES)
    assert data == second.read_bytes()
    assert data != other_seed.read_bytes()
