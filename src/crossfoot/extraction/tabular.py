"""Baseline deterministic extractor for delimited statement exports."""

import csv
import io
import re
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
)
from crossfoot.models.extraction import (
    ExtractedDocument,
    ExtractedField,
    FieldSignals,
    IngestError,
)

_SYNONYM_TO_FIELD: dict[str, FieldName] = {
    synonym.casefold(): name
    for name, synonyms in CSV_HEADER_SYNONYMS.items()
    for synonym in synonyms
}

# The tabular renderer emits utf-8 and cp1252 only; isolating detection to
# these stops charset-normalizer from guessing exotic codepages for one byte.
_CANDIDATE_ENCODINGS = ["utf_8", "cp1252"]

_CANDIDATE_DELIMITERS = ",|\t"
_DEFAULT_DELIMITER = ","
_SNIFF_SAMPLE_CHARS = 4096
# A row is the header when this share of its non-empty cells match a synonym.
_HEADER_MATCH_RATIO = 0.6
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
    try:
        text = _decode(path)
        if text is None:
            return _unprocessable(path, doc_id, "undecodable or binary content")
        reader = csv.reader(io.StringIO(text, newline=""), delimiter=_detect_delimiter(text))
        rows = list(reader)
        header = _find_header(rows)
        if header is None:
            return _unprocessable(path, doc_id, "no recognizable header row")
        header_index, column_map = header
        line_fields = _extract_line_fields(rows[header_index + 1 :], column_map, doc_id)
    except Exception as error:  # the contract requires unprocessable, not a raise
        return _unprocessable(path, doc_id, f"unexpected failure: {error}")
    # Statement totals are not present in CSV exports, so no crossfoot check here.
    return ExtractedDocument(
        doc_id=doc_id,
        file_path=path.as_posix(),
        route=ExtractionRoute.CSV,
        line_fields=line_fields,
    )


def _decode(path: Path) -> str | None:
    data = path.read_bytes()
    if not data:
        return None
    best = from_bytes(data, cp_isolation=_CANDIDATE_ENCODINGS).best()
    return None if best is None else str(best)


def _detect_delimiter(text: str) -> str:
    sample = text[:_SNIFF_SAMPLE_CHARS]
    try:
        return csv.Sniffer().sniff(sample, delimiters=_CANDIDATE_DELIMITERS).delimiter
    except csv.Error:
        counts = {delimiter: sample.count(delimiter) for delimiter in _CANDIDATE_DELIMITERS}
        best_delimiter = max(counts, key=lambda delimiter: counts[delimiter])
        return best_delimiter if counts[best_delimiter] else _DEFAULT_DELIMITER


def _find_header(rows: list[list[str]]) -> tuple[int, dict[int, FieldName]] | None:
    """Locate the first synonym-bearing row and map its columns to field names."""
    for index, row in enumerate(rows):
        cells = [cell.strip() for cell in row]
        non_empty = [cell for cell in cells if cell]
        if not non_empty:
            continue
        matched = sum(1 for cell in non_empty if cell.casefold() in _SYNONYM_TO_FIELD)
        if matched / len(non_empty) < _HEADER_MATCH_RATIO:
            continue
        column_map = {
            column: _SYNONYM_TO_FIELD[cell.casefold()]
            for column, cell in enumerate(cells)
            if cell.casefold() in _SYNONYM_TO_FIELD
        }
        return index, column_map
    return None


def _extract_line_fields(
    data_rows: list[list[str]], column_map: dict[int, FieldName], doc_id: str
) -> tuple[ExtractedField, ...]:
    fields: list[ExtractedField] = []
    line_no = 0
    for row in data_rows:
        if _is_non_data(row):
            continue
        line_no += 1
        for column, name in column_map.items():
            if column < len(row):
                fields.append(_build_field(doc_id, line_no, name, row[column]))
    return tuple(fields)


def _is_non_data(row: list[str]) -> bool:
    non_empty = [cell.strip() for cell in row if cell.strip()]
    if not non_empty:
        return True
    return non_empty[0].casefold().startswith(_TOTALS_PREFIX)


def _build_field(doc_id: str, line_no: int, name: FieldName, raw: str) -> ExtractedField:
    family = FIELD_FAMILIES[name]
    value, value_cents, value_date = _parse_value(family, raw)
    grammar = _grammar_signal(name, raw.strip()) if family is FieldFamily.REFERENCE else None
    parsed = value is not None
    return ExtractedField(
        field_id=f"fld-{doc_id}-{line_no:04d}-{name}",
        doc_id=doc_id,
        line_no=line_no,
        name=name,
        family=family,
        raw_text=raw,
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


def _unprocessable(path: Path, doc_id: str, detail: str) -> ExtractedDocument:
    return ExtractedDocument(
        doc_id=doc_id,
        file_path=path.as_posix(),
        route=ExtractionRoute.UNPROCESSABLE,
        error=IngestError(kind=IngestErrorKind.UNRECOGNIZED, detail=detail),
    )
