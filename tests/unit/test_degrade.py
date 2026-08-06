"""Scan degradation must leave zero extractable characters behind."""

from pathlib import Path

import pdfplumber
import pytest

from crossfoot.generator.corrupt import build_minimal_pdf
from crossfoot.generator.degrade import (
    SCAN_HEAVY_PROFILE,
    SCAN_LIGHT_PROFILE,
    degrade_to_scan,
)

DEGRADE_SEED = 7


def _char_count(path: Path) -> int:
    with pdfplumber.open(path) as pdf:
        return sum(len(page.chars) for page in pdf.pages)


@pytest.mark.parametrize("profile", [SCAN_LIGHT_PROFILE, SCAN_HEAVY_PROFILE])
def test_degrade_strips_the_text_layer(tmp_path: Path, profile: str) -> None:
    pdf_path = tmp_path / "statement.pdf"
    pdf_path.write_bytes(build_minimal_pdf("degrade fixture text"))
    assert _char_count(pdf_path) > 0, "fixture PDF should start with extractable text"

    degrade_to_scan(pdf_path, profile, DEGRADE_SEED)

    assert pdf_path.read_bytes().startswith(b"%PDF")
    assert _char_count(pdf_path) == 0


def test_degrade_is_deterministic_per_seed(tmp_path: Path) -> None:
    source = build_minimal_pdf("determinism fixture")
    first = tmp_path / "first.pdf"
    second = tmp_path / "second.pdf"
    for target in (first, second):
        target.write_bytes(source)
        degrade_to_scan(target, SCAN_LIGHT_PROFILE, DEGRADE_SEED)
    assert first.read_bytes() == second.read_bytes()


def test_degrade_rejects_unknown_profile(tmp_path: Path) -> None:
    pdf_path = tmp_path / "statement.pdf"
    pdf_path.write_bytes(build_minimal_pdf("unused"))
    with pytest.raises(ValueError, match="unknown scan profile"):
        degrade_to_scan(pdf_path, "scan_extreme", DEGRADE_SEED)
