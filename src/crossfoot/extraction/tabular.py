"""Baseline deterministic extractor for delimited statement exports."""

import csv
import io
import logging
import re
from collections.abc import Iterator, Sequence
from datetime import date
from pathlib import Path

from charset_normalizer import from_bytes

from crossfoot.constants import (
    CSV_HEADER_SYNONYMS,
    FIELD_FAMILIES,
    REF_GRAMMARS,
    ExtractionRoute,
    FieldFamily,
    FieldName,
    FieldSource,
    IngestErrorKind,
    QualityTier,
    ReviewStatus,
)
from crossfoot.extraction.normalize import (
    CENTS_PER_DOLLAR,
    normalize_reference,
    parse_amount_to_cents,
    parse_date,
    strip_control_chars,
)
from crossfoot.models.extraction import (
    ExtractedDocument,
    ExtractedField,
    FieldSignals,
    IngestError,
)

_LOGGER = logging.getLogger(__name__)

# Resource ceilings. A dealer statement export is tens of kilobytes, so these
# are generous for real input while keeping hostile input from exhausting memory.
MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_DATA_ROWS = 5_000
MAX_CELL_CHARS = 64 * 1024
# Rows scanned for a header before the file is called unrecognizable.
MAX_HEADER_SCAN_ROWS = 100

# The csv parser keeps this limit process wide and defaults to effectively
# unbounded, which lets one quoted cell absorb an entire file.
csv.field_size_limit(MAX_CELL_CHARS)

_SYNONYM_TO_FIELD: dict[str, FieldName] = {
    synonym.casefold(): name
    for name, synonyms in CSV_HEADER_SYNONYMS.items()
    for synonym in synonyms
}

# The tabular renderer emits utf-8 and cp1252 only; isolating detection to
# these stops charset-normalizer from guessing exotic codepages for one byte.
_CANDIDATE_ENCODINGS = ["utf_8", "cp1252"]
# A utf-8 BOM, plus the way those same bytes read through cp1252. Either one
# would otherwise glue itself to the first header cell and lose that column.
_BOM_MARKERS = ("\ufeff", "\u00ef\u00bb\u00bf")

_CANDIDATE_DELIMITERS = ",|\t"
_DEFAULT_DELIMITER = ","
_SNIFF_SAMPLE_CHARS = 4096
# A row is the header when this share of its non-empty cells match a synonym.
_HEADER_MATCH_RATIO = 0.6
# Leading decoration on a header cell, dropped before synonym matching.
_HEADER_LEADING_JUNK = re.compile(r"^[^0-9A-Za-z]+")
# Rows whose first non-empty cell starts with this are summary rows, not data.
_TOTALS_PREFIX = "total"
# Dates outside this window fail the plausibility validator.
_PLAUSIBLE_YEAR_MIN = 1990
_PLAUSIBLE_YEAR_MAX = 2100


def _compile_grammars() -> dict[FieldName, tuple[re.Pattern[str], ...]]:
    by_field: dict[FieldName, list[re.Pattern[str]]] = {}
    for grammars in REF_GRAMMARS.values():
        for name, pattern in grammars.items():
            by_field.setdefault(name, []).append(re.compile(pattern))
    return {name: tuple(patterns) for name, patterns in by_field.items()}


_GRAMMARS_BY_FIELD = _compile_grammars()


def extract_csv(path: Path, doc_id: str) -> ExtractedDocument:
    """Extract a CSV statement export into typed line fields; never raises."""
    size = _file_size(path)
    if size is None:
        return _unprocessable(path, doc_id, IngestErrorKind.UNRECOGNIZED, "unreadable file")
    if size > MAX_FILE_BYTES:
        # Checked by stat, so an oversize file is never read into memory.
        return _unprocessable(
            path,
            doc_id,
            IngestErrorKind.TOO_LARGE,
            f"file is {size} bytes, over the {MAX_FILE_BYTES} byte limit",
        )
    text = _decode(path)
    if text is None:
        return _unprocessable(
            path, doc_id, IngestErrorKind.UNRECOGNIZED, "undecodable or binary content"
        )
    rows = iter(csv.reader(io.StringIO(text, newline=""), delimiter=_detect_delimiter(text)))
    # Only whole-file failures land here; a single bad cell is handled per field.
    try:
        column_map = _find_header(rows)
        if column_map is None:
            return _unprocessable(
                path, doc_id, IngestErrorKind.UNRECOGNIZED, "no recognizable header row"
            )
        line_fields, over_row_cap = _extract_line_fields(rows, column_map, doc_id)
    except csv.Error as error:
        return _unprocessable(
            path, doc_id, IngestErrorKind.UNRECOGNIZED, f"malformed delimited text: {error}"
        )
    if over_row_cap:
        return _unprocessable(
            path, doc_id, IngestErrorKind.TOO_LARGE, f"more than {MAX_DATA_ROWS} data rows"
        )
    # Statement totals are not present in CSV exports, so no crossfoot check here.
    return ExtractedDocument(
        doc_id=doc_id,
        file_path=path.as_posix(),
        route=ExtractionRoute.CSV,
        line_fields=line_fields,
    )


def _file_size(path: Path) -> int | None:
    try:
        return path.stat().st_size
    except OSError as error:
        _LOGGER.warning("cannot stat %s: %s", path, error)
        return None


def _decode(path: Path) -> str | None:
    try:
        data = path.read_bytes()
    except OSError as error:
        _LOGGER.warning("cannot read %s: %s", path, error)
        return None
    if not data:
        return None
    best = from_bytes(data, cp_isolation=_CANDIDATE_ENCODINGS).best()
    return None if best is None else _strip_bom(str(best))


def _strip_bom(text: str) -> str:
    """Drop a leading utf-8 BOM, including the cp1252 spelling of those bytes."""
    for marker in _BOM_MARKERS:
        if text.startswith(marker):
            return text[len(marker) :]
    return text


def _detect_delimiter(text: str) -> str:
    sample = text[:_SNIFF_SAMPLE_CHARS]
    try:
        return csv.Sniffer().sniff(sample, delimiters=_CANDIDATE_DELIMITERS).delimiter
    except csv.Error:
        counts = {delimiter: sample.count(delimiter) for delimiter in _CANDIDATE_DELIMITERS}
        best_delimiter = max(counts, key=lambda delimiter: counts[delimiter])
        return best_delimiter if counts[best_delimiter] else _DEFAULT_DELIMITER


def _find_header(rows: Iterator[list[str]]) -> dict[int, FieldName] | None:
    """Consume rows up to the first synonym-bearing one and map its columns."""
    for _ in range(MAX_HEADER_SCAN_ROWS):
        row = next(rows, None)
        if row is None:
            return None
        header = [(cell.strip(), _header_key(cell)) for cell in row]
        non_empty = [key for cell, key in header if cell]
        if not non_empty:
            continue
        matched = sum(1 for key in non_empty if key in _SYNONYM_TO_FIELD)
        if matched / len(non_empty) < _HEADER_MATCH_RATIO:
            continue
        return _map_columns(header)
    return None


def _header_key(cell: str) -> str:
    """Match key for a header cell: no leading decoration, case insensitive."""
    return _HEADER_LEADING_JUNK.sub("", strip_control_chars(cell).strip()).casefold()


def _map_columns(header: Sequence[tuple[str, str]]) -> dict[int, FieldName]:
    """Map columns to field names; the first column wins per field name."""
    column_map: dict[int, FieldName] = {}
    taken: set[FieldName] = set()
    unmapped: list[str] = []
    for column, (cell, key) in enumerate(header):
        name = _SYNONYM_TO_FIELD.get(key)
        if name is None:
            if cell:
                unmapped.append(cell)
            continue
        if name in taken:
            # A second column claiming the same field would duplicate field ids.
            unmapped.append(cell)
            continue
        column_map[column] = name
        taken.add(name)
    if unmapped:
        _LOGGER.warning("header cells left unmapped: %s", ", ".join(unmapped))
    return column_map


def _extract_line_fields(
    data_rows: Iterator[list[str]], column_map: dict[int, FieldName], doc_id: str
) -> tuple[tuple[ExtractedField, ...], bool]:
    """Fields for every data row, plus a flag for the row cap being exceeded."""
    fields: list[ExtractedField] = []
    line_no = 0
    for row in data_rows:
        if _is_non_data(row):
            continue
        if line_no >= MAX_DATA_ROWS:
            return tuple(fields), True
        line_no += 1
        for column, name in column_map.items():
            if column < len(row):
                fields.append(_build_field(doc_id, line_no, name, row[column]))
    return tuple(fields), False


def _is_non_data(row: list[str]) -> bool:
    non_empty = [cell.strip() for cell in row if cell.strip()]
    if not non_empty:
        return True
    return non_empty[0].casefold().startswith(_TOTALS_PREFIX)


def _build_field(doc_id: str, line_no: int, name: FieldName, raw: str) -> ExtractedField:
    family = FIELD_FAMILIES[name]
    # Control characters are stripped before anything reads the cell, so NUL
    # bytes never reach a value or a raw-text comparison.
    text = strip_control_chars(raw)
    try:
        value, value_cents, value_date = _parse_value(family, text)
        grammar = _grammar_signal(name, text.strip()) if family is FieldFamily.REFERENCE else None
    except Exception as error:  # one hostile cell must not void the document
        _LOGGER.warning("%s line %d %s failed to parse: %s", doc_id, line_no, name, error)
        value, value_cents, value_date, grammar = None, None, None, None
    parsed = value is not None
    return ExtractedField(
        field_id=f"fld-{doc_id}-{line_no:04d}-{name}",
        doc_id=doc_id,
        line_no=line_no,
        name=name,
        family=family,
        raw_text=text,
        value=value,
        value_cents=value_cents,
        value_date=value_date,
        source=FieldSource.DETERMINISTIC,
        signals=FieldSignals(
            validator_pass=1.0 if parsed else 0.0,
            grammar_match=grammar,
            quality_tier=QualityTier.CSV,
        ),
        confidence=1.0 if parsed else 0.0,
        status=ReviewStatus.AUTO_ACCEPTED if parsed else ReviewStatus.NEEDS_REVIEW,
    )


def _parse_value(family: FieldFamily, raw: str) -> tuple[str | None, int | None, date | None]:
    """Canonical value plus the typed value slot for the family; Nones when unparseable."""
    text = raw.strip()
    if family is FieldFamily.AMOUNT:
        cents = parse_amount_to_cents(text)
        if cents is None:
            return None, None, None
        return _cents_to_decimal_string(cents), cents, None
    if family is FieldFamily.DATE:
        parsed = parse_date(text)
        if parsed is None or not _PLAUSIBLE_YEAR_MIN <= parsed.year <= _PLAUSIBLE_YEAR_MAX:
            return None, None, None
        return parsed.isoformat(), None, parsed
    if family is FieldFamily.REFERENCE:
        return normalize_reference(text) or None, None, None
    return text or None, None, None


def _grammar_signal(name: FieldName, text: str) -> float | None:
    patterns = _GRAMMARS_BY_FIELD.get(name)
    if not patterns or not text:
        return None
    return 1.0 if any(pattern.fullmatch(text) for pattern in patterns) else 0.0


def _cents_to_decimal_string(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    dollars, remainder = divmod(abs(cents), CENTS_PER_DOLLAR)
    return f"{sign}{dollars}.{remainder:02d}"


def _unprocessable(
    path: Path, doc_id: str, kind: IngestErrorKind, detail: str
) -> ExtractedDocument:
    return ExtractedDocument(
        doc_id=doc_id,
        file_path=path.as_posix(),
        route=ExtractionRoute.UNPROCESSABLE,
        error=IngestError(kind=kind, detail=detail),
    )
