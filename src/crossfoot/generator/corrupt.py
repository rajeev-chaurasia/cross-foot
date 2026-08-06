"""Deterministic corrupted-file fixtures for ingest robustness testing."""

import random
from collections.abc import Callable
from pathlib import Path

from pypdf import PdfWriter

from crossfoot.constants import CorruptionKind

TRUNCATION_RATIO = 0.6
JUNK_SIZE_BYTES = 512
WRONG_EXTENSION_ROW_COUNT = 3
ENCRYPTION_PASSWORD_PREFIX = "crossfoot-locked-"
LETTER_WIDTH_POINTS = 612.0
LETTER_HEIGHT_POINTS = 792.0

# File signatures ingest sniffers recognize; binary junk must not impersonate any.
KNOWN_MAGIC_PREFIXES: tuple[bytes, ...] = (
    b"%PDF",
    b"PK\x03\x04",
    b"\x89PNG",
    b"GIF8",
    b"\xff\xd8\xff",
    b"BM",
    b"II*\x00",
    b"MM\x00*",
    b"\xd0\xcf\x11\xe0",
    b"{\\rtf",
    b"%!PS",
)


def build_minimal_pdf(stamp: str) -> bytes:
    """Smallest well-formed one-page PDF with the stamp as its only text.

    Built by hand so truncation points are predictable and so tests can make a
    tiny text-bearing PDF without a browser. The stamp must not contain
    parentheses or backslashes (PDF string delimiters).
    """
    stream = f"BT /F1 12 Tf 72 720 Td ({stamp}) Tj ET".encode("ascii")
    objects: tuple[bytes, ...] = (
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R"
        b" /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    )
    buffer = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(buffer))
        buffer += b"%d 0 obj\n" % number
        buffer += body
        buffer += b"\nendobj\n"
    xref_offset = len(buffer)
    buffer += b"xref\n0 %d\n" % (len(objects) + 1)
    buffer += b"0000000000 65535 f \n"
    for offset in offsets:
        buffer += b"%010d 00000 n \n" % offset
    buffer += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref_offset,
    )
    return bytes(buffer)


def _write_truncated_pdf(seed: int, out_path: Path) -> None:
    """Valid PDF header, body cut off before the xref table."""
    complete = build_minimal_pdf(f"synthetic statement seed {seed}")
    out_path.write_bytes(complete[: int(len(complete) * TRUNCATION_RATIO)])


def _write_wrong_extension(seed: int, out_path: Path) -> None:
    """CSV bytes saved under a .pdf name; ingest must sniff past the extension."""
    rng = random.Random(seed)
    rows = ["Date,Description,Amount"]
    for index in range(1, WRONG_EXTENSION_ROW_COUNT + 1):
        amount_cents = rng.randrange(1_000, 250_000)
        rows.append(f"0{index}/15/2026,Misfiled export row {index},{amount_cents / 100:.2f}")
    out_path.write_text("\n".join(rows) + "\n", encoding="utf-8")


def _write_empty_file(seed: int, out_path: Path) -> None:
    del seed  # nothing to vary in zero bytes
    out_path.write_bytes(b"")


def _write_encrypted_pdf(seed: int, out_path: Path) -> None:
    """One blank password-protected page; ingest must classify, not decrypt."""
    writer = PdfWriter()
    writer.add_blank_page(width=LETTER_WIDTH_POINTS, height=LETTER_HEIGHT_POINTS)
    writer.encrypt(user_password=f"{ENCRYPTION_PASSWORD_PREFIX}{seed}")
    with out_path.open("wb") as handle:
        writer.write(handle)


def _write_binary_junk(seed: int, out_path: Path) -> None:
    """Seeded random bytes with no recognizable magic header."""
    rng = random.Random(seed)
    data = rng.randbytes(JUNK_SIZE_BYTES)
    while data.startswith(KNOWN_MAGIC_PREFIXES):
        data = rng.randbytes(JUNK_SIZE_BYTES)
    out_path.write_bytes(data)


_WRITERS: dict[CorruptionKind, Callable[[int, Path], None]] = {
    CorruptionKind.TRUNCATED_PDF: _write_truncated_pdf,
    CorruptionKind.WRONG_EXTENSION: _write_wrong_extension,
    CorruptionKind.EMPTY_FILE: _write_empty_file,
    CorruptionKind.ENCRYPTED_PDF: _write_encrypted_pdf,
    CorruptionKind.BINARY_JUNK: _write_binary_junk,
}


def write_corrupted(kind: CorruptionKind, seed: int, out_path: Path) -> None:
    """Write one deterministic corrupted artifact of the given kind."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    _WRITERS[kind](seed, out_path)
