"""Route a file to an extractor by its bytes, never by its name.

The corrupted tier includes CSV rows saved under a .pdf name, so the extension
is evidence at best. Every decision here is a magic-byte read plus, for PDFs,
one cheap look for a text layer; nothing in this module trusts the manifest.
"""

from __future__ import annotations

import logging
import zipfile
from dataclasses import dataclass
from pathlib import Path

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from crossfoot.constants import ExtractionRoute, IngestErrorKind
from crossfoot.extraction.normalize import strip_control_chars
from crossfoot.models.extraction import IngestError

_LOGGER = logging.getLogger(__name__)

PDF_MAGIC = b"%PDF-"
ZIP_MAGIC = b"PK\x03\x04"
# Every xlsx is an OPC zip carrying this part; a plain zip is not a workbook.
XLSX_WORKBOOK_MEMBER = "xl/workbook.xml"

# Enough bytes to see any magic number and sniff delimited text.
SNIFF_BYTES = 8192
# Pages read while deciding whether a PDF carries a text layer.
TEXT_LAYER_PAGES = 2
# Characters of extractable text per page that make a PDF born-digital rather
# than a scan. A scan yields zero; a page of statement lines yields hundreds.
MIN_DIGITAL_CHARS = 200

# A text file is delimited data when a sniffed line splits on one of these.
DELIMITERS = (",", "|", "\t")
MIN_DELIMITED_FIELDS = 3
# Share of sniffed bytes that must decode as printable text.
MIN_PRINTABLE_RATIO = 0.9


@dataclass(frozen=True, slots=True)
class Routing:
    """Where a file goes, and why it goes nowhere when it does not."""

    route: ExtractionRoute
    error: IngestError | None = None


def route_file(path: Path) -> Routing:
    """Classify one file; never raises, whatever the bytes turn out to be."""
    head = _read_head(path)
    if head is None:
        return _unprocessable(IngestErrorKind.UNRECOGNIZED, f"unreadable file: {path.name}")
    if not head:
        return _unprocessable(IngestErrorKind.EMPTY, "file is empty")
    if head.startswith(PDF_MAGIC):
        return _route_pdf(path)
    if head.startswith(ZIP_MAGIC):
        return _route_zip(path)
    if _looks_delimited(head):
        return Routing(route=ExtractionRoute.CSV)
    return _unprocessable(IngestErrorKind.UNRECOGNIZED, "no recognized file signature")


def _read_head(path: Path) -> bytes | None:
    try:
        with path.open("rb") as handle:
            return handle.read(SNIFF_BYTES)
    except OSError as error:
        _LOGGER.warning("cannot read %s: %s", path, error)
        return None


def _route_pdf(path: Path) -> Routing:
    """Digital when a text layer is present, scanned when it is not."""
    try:
        reader = PdfReader(path)
        if reader.is_encrypted:
            # Classified, never decrypted: a locked statement is a human's problem.
            return _unprocessable(IngestErrorKind.ENCRYPTED, "pdf is password protected")
        pages = reader.pages[:TEXT_LAYER_PAGES]
        if not pages:
            return _unprocessable(IngestErrorKind.TRUNCATED, "pdf carries no pages")
        characters = sum(len(page.extract_text() or "") for page in pages)
    except (PdfReadError, OSError, ValueError, KeyError, IndexError) as error:
        return _unprocessable(IngestErrorKind.TRUNCATED, f"unreadable pdf: {error}")
    if characters >= MIN_DIGITAL_CHARS * len(pages):
        return Routing(route=ExtractionRoute.DIGITAL_PDF)
    return Routing(route=ExtractionRoute.SCANNED_PDF)


def _route_zip(path: Path) -> Routing:
    try:
        with zipfile.ZipFile(path) as archive:
            members = set(archive.namelist())
    except (zipfile.BadZipFile, OSError) as error:
        return _unprocessable(IngestErrorKind.TRUNCATED, f"unreadable zip container: {error}")
    if XLSX_WORKBOOK_MEMBER in members:
        return Routing(route=ExtractionRoute.XLSX)
    return _unprocessable(IngestErrorKind.UNRECOGNIZED, "zip container is not a workbook")


def _looks_delimited(head: bytes) -> bool:
    """Printable text whose first substantial line splits into several fields."""
    try:
        text = head.decode("utf-8")
    except UnicodeDecodeError:
        text = head.decode("cp1252", errors="replace")
        if text.count("�") > len(text) * (1 - MIN_PRINTABLE_RATIO):
            return False
    cleaned = strip_control_chars(text)
    if len(cleaned) < len(text) * MIN_PRINTABLE_RATIO:
        return False  # control bytes at this density mean binary, not text
    for line in cleaned.splitlines():
        if not line.strip():
            continue
        return any(len(line.split(delimiter)) >= MIN_DELIMITED_FIELDS for delimiter in DELIMITERS)
    return False


def _unprocessable(kind: IngestErrorKind, detail: str) -> Routing:
    return Routing(route=ExtractionRoute.UNPROCESSABLE, error=IngestError(kind=kind, detail=detail))
