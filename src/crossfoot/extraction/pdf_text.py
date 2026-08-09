"""Deterministic extraction from born-digital PDFs, driven by word boxes.

Every value keeps the union of the word boxes that produced it, so the review
crop for this tier is an exact region rather than a guess. The table is found by
its column captions and the totals block by its labels, both matched against the
vocabulary the marque templates print.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import pdfplumber

from crossfoot.confidence.signals import crossfoot_delta_cents
from crossfoot.constants import (
    FIELD_FAMILIES,
    CropKind,
    ExtractionRoute,
    FieldFamily,
    FieldName,
    FieldSource,
    IngestErrorKind,
)
from crossfoot.extraction.normalize import (
    format_cents,
    parse_amount_to_cents,
    parse_date,
    strip_control_chars,
)
from crossfoot.models.extraction import (
    BBox,
    ExtractedDocument,
    ExtractedField,
    FieldSignals,
    IngestError,
)

_LOGGER = logging.getLogger(__name__)

# Resource ceilings. A dealer statement is a handful of pages; these keep a
# hostile file from turning extraction into a denial of service.
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_PAGES = 50
MAX_LINE_ROWS = 5_000

# Words share a visual row when their vertical extents overlap by this share of
# the shorter one's height. Overlap rather than a top-coordinate tolerance,
# because a caption too wide for its column prints on two lines that straddle
# the captions beside it, and those three lines are one header row.
MIN_ROW_OVERLAP_RATIO = 0.2
# Padding added to an exact crop so the glyphs are not flush against the edge.
CROP_PADDING_POINTS = 3.0
# Label and value are the same field when the value sits within this many points
# below the label and overlaps it horizontally, which is how boxed metadata
# cells print.
LABEL_BELOW_POINTS = 14.0
# Column captions a row needs before it is believed to be the table header.
MIN_HEADER_COLUMNS = 3
# Longest caption phrase, in words, worth trying to match.
MAX_LABEL_WORDS = 3

# Table column captions across the marque templates, matched case insensitively
# with punctuation and spacing removed.
COLUMN_LABELS: dict[FieldName, tuple[str, ...]] = {
    FieldName.LINE_DATE: ("date", "postdate", "transdate"),
    FieldName.CLAIM_NUMBER: ("claim", "claimno", "claimnumber"),
    FieldName.RO_NUMBER: ("ro", "rono", "ronumber", "repairorder"),
    FieldName.VIN: ("vin", "unitvin", "vehicleid"),
    FieldName.INVOICE_NUMBER: ("invoice", "invoiceno", "invoicenumber"),
    FieldName.PROGRAM_CODE: ("program", "programcode", "pgmcd"),
    FieldName.DESCRIPTION: ("description", "desc", "detail"),
    FieldName.LINE_AMOUNT: ("amount", "amt", "netamount"),
}

# Statement level labels, longest phrase first so "statement date" wins over
# "statement". Every phrase is normalized the same way as the page text.
HEADER_LABELS: dict[FieldName, tuple[str, ...]] = {
    FieldName.STATEMENT_DATE: ("statementdate", "stmtdate", "memodate", "issued", "dated"),
    FieldName.STATEMENT_NUMBER: (
        "statementno",
        "statementnumber",
        "stmtno",
        "memono",
        "statement",
        "memo",
    ),
    FieldName.PREVIOUS_BALANCE: ("previousbalance", "balanceforward"),
    FieldName.SUBTOTAL: ("subtotal", "purchasesthisperiod", "activitythisperiod"),
    FieldName.TOTAL: (
        "totaldue",
        "amountdue",
        "balancedue",
        "endingbalancedue",
        "memototal",
        "totalearned",
        "totalcredit",
    ),
}

_NON_LABEL_CHARS = re.compile(r"[^0-9a-z]+")


@dataclass(frozen=True, slots=True)
class Word:
    """One positioned word. Coordinates are page points, origin at the top left."""

    text: str
    x0: float
    x1: float
    top: float
    bottom: float
    page: int


@dataclass(frozen=True, slots=True)
class PageWords:
    page: int
    width: float
    height: float
    words: tuple[Word, ...]


@dataclass(frozen=True, slots=True)
class _Caption:
    """One column caption and its horizontal extent; unnamed when unrecognized."""

    name: FieldName | None
    x0: float
    x1: float


@dataclass(frozen=True, slots=True)
class _Column:
    """One captioned column. An unnamed column is a divider that owns no field."""

    name: FieldName | None
    left: float
    right: float


def extract_pdf(path: Path, doc_id: str) -> ExtractedDocument:
    """Extract a born-digital statement PDF; never raises."""
    size = _file_size(path)
    if size is None:
        return _unprocessable(path, doc_id, IngestErrorKind.UNRECOGNIZED, "unreadable file")
    if size > MAX_FILE_BYTES:
        return _unprocessable(
            path,
            doc_id,
            IngestErrorKind.TOO_LARGE,
            f"file is {size} bytes, over the {MAX_FILE_BYTES} byte limit",
        )
    try:
        pages = read_pages(path)
    except Exception as error:  # pdfminer raises a wide family on damaged input
        return _unprocessable(path, doc_id, IngestErrorKind.TRUNCATED, f"unreadable pdf: {error}")
    if not pages:
        return _unprocessable(path, doc_id, IngestErrorKind.TRUNCATED, "pdf carries no pages")
    header_fields = _header_fields(pages, doc_id)
    line_fields = _line_fields(pages, doc_id)
    doc = ExtractedDocument(
        doc_id=doc_id,
        file_path=path.as_posix(),
        route=ExtractionRoute.DIGITAL_PDF,
        header_fields=header_fields,
        line_fields=line_fields,
    )
    return doc.model_copy(update={"crossfoot_delta_cents": crossfoot_delta_cents(doc)})


def read_pages(path: Path) -> tuple[PageWords, ...]:
    """Positioned words per page, the only thing this module reads from a PDF."""
    with pdfplumber.open(path) as pdf:
        return tuple(
            PageWords(
                page=index,
                width=float(page.width),
                height=float(page.height),
                words=tuple(_word(entry, index) for entry in page.extract_words()),
            )
            for index, page in enumerate(pdf.pages[:MAX_PAGES])
        )


def rows_of(words: Iterable[Word]) -> tuple[tuple[Word, ...], ...]:
    """Words grouped into visual rows, top to bottom and left to right."""
    rows: list[list[Word]] = []
    spans: list[tuple[float, float]] = []
    for word in sorted(words, key=lambda word: (word.top, word.x0)):
        if rows and _shares_row(spans[-1], word):
            rows[-1].append(word)
            spans[-1] = (min(spans[-1][0], word.top), max(spans[-1][1], word.bottom))
        else:
            rows.append([word])
            spans.append((word.top, word.bottom))
    return tuple(tuple(sorted(row, key=lambda word: word.x0)) for row in rows)


def _shares_row(span: tuple[float, float], word: Word) -> bool:
    top, bottom = span
    overlap = min(bottom, word.bottom) - max(top, word.top)
    height = min(bottom - top, word.bottom - word.top)
    return height > 0 and overlap / height >= MIN_ROW_OVERLAP_RATIO


def union_bbox(words: Sequence[Word], page: PageWords) -> BBox | None:
    """Padded union of the contributing word boxes, normalized to 0 to 1."""
    if not words:
        return None
    return BBox(
        page=page.page,
        x0=max(0.0, min(word.x0 for word in words) - CROP_PADDING_POINTS) / page.width,
        y0=max(0.0, min(word.top for word in words) - CROP_PADDING_POINTS) / page.height,
        x1=min(page.width, max(word.x1 for word in words) + CROP_PADDING_POINTS) / page.width,
        y1=min(page.height, max(word.bottom for word in words) + CROP_PADDING_POINTS) / page.height,
    )


def _word(entry: dict[str, Any], page: int) -> Word:
    return Word(
        text=strip_control_chars(str(entry["text"])),
        x0=float(entry["x0"]),
        x1=float(entry["x1"]),
        top=float(entry["top"]),
        bottom=float(entry["bottom"]),
        page=page,
    )


def _label_key(text: str) -> str:
    return _NON_LABEL_CHARS.sub("", text.casefold())


def _phrase_key(words: Sequence[Word]) -> str:
    return "".join(_label_key(word.text) for word in words)


def _match_label(row: Sequence[Word], start: int, phrases: Sequence[str]) -> int | None:
    """Words consumed by a phrase match starting at start, or None."""
    for length in range(min(MAX_LABEL_WORDS, len(row) - start), 0, -1):
        if _phrase_key(row[start : start + length]) in phrases:
            return length
    return None


# ---------------------------------------------------------------------------
# Line table
# ---------------------------------------------------------------------------


def _line_fields(pages: Sequence[PageWords], doc_id: str) -> tuple[ExtractedField, ...]:
    fields: list[ExtractedField] = []
    line_no = 0
    for page in pages:
        rows = rows_of(page.words)
        found = _find_columns(rows)
        if found is None:
            continue
        header_index, columns = found
        for row in rows[header_index + 1 :]:
            cells = _row_cells(row, columns)
            if not _is_data_row(cells):
                continue
            if line_no >= MAX_LINE_ROWS:
                return tuple(fields)
            line_no += 1
            fields.extend(
                _build_field(doc_id, name, cell, page, line_no=line_no)
                for name, cell in cells.items()
            )
    return tuple(fields)


def _find_columns(
    rows: Sequence[tuple[Word, ...]],
) -> tuple[int, tuple[_Column, ...]] | None:
    """The table header row plus the x span owned by each of its captions."""
    for index, row in enumerate(rows):
        captions = _captions(row)
        if sum(1 for caption in captions if caption.name is not None) >= MIN_HEADER_COLUMNS:
            return index, _column_spans(captions)
    return None


def _captions(row: Sequence[Word]) -> list[_Caption]:
    """Every caption in the row, left to right, named when it is recognized.

    An unrecognized caption still divides the table, so a line-ordinal column
    that no field claims cannot spill its digits into the column beside it.
    """
    found: list[_Caption] = []
    taken: set[FieldName] = set()
    position = 0
    while position < len(row):
        match = _caption_at(row, position, taken)
        name, length = (None, 1) if match is None else match
        span = row[position : position + length]
        found.append(_Caption(name=name, x0=span[0].x0, x1=span[-1].x1))
        if name is not None:
            taken.add(name)
        position += length
    return found


def _caption_at(
    row: Sequence[Word], position: int, taken: set[FieldName]
) -> tuple[FieldName, int] | None:
    for name, phrases in COLUMN_LABELS.items():
        if name in taken:
            continue  # the first column claiming a field keeps it
        length = _match_label(row, position, phrases)
        if length is not None:
            return name, length
    return None


def _column_spans(captions: Sequence[_Caption]) -> tuple[_Column, ...]:
    """Boundaries in the whitespace between captions.

    Splitting on the gap rather than on caption centers keeps a wide caption
    from stealing the left-aligned cells of the column beside it, and still
    leaves right-aligned amounts inside their own column.
    """
    columns: list[_Column] = []
    for index, caption in enumerate(captions):
        left = float("-inf") if index == 0 else (captions[index - 1].x1 + caption.x0) / 2
        right = (
            float("inf")
            if index == len(captions) - 1
            else (caption.x1 + captions[index + 1].x0) / 2
        )
        columns.append(_Column(name=caption.name, left=left, right=right))
    return tuple(columns)


def _row_cells(
    row: Sequence[Word], columns: Sequence[_Column]
) -> dict[FieldName, tuple[Word, ...]]:
    """Words bucketed into the named columns; unnamed columns are dropped."""
    cells: dict[FieldName, list[Word]] = {
        column.name: [] for column in columns if column.name is not None
    }
    for word in row:
        center = (word.x0 + word.x1) / 2
        column = next((c for c in columns if c.left <= center < c.right), None)
        if column is not None and column.name is not None:
            cells[column.name].append(word)
    return {name: tuple(words) for name, words in cells.items() if words}


def _is_data_row(cells: dict[FieldName, tuple[Word, ...]]) -> bool:
    """A line row prints both a date and an amount; totals and footers do not."""
    return _parses(cells, FieldName.LINE_DATE) and _parses(cells, FieldName.LINE_AMOUNT)


def _parses(cells: dict[FieldName, tuple[Word, ...]], name: FieldName) -> bool:
    words = cells.get(name)
    if not words:
        return False
    return _canonical(FIELD_FAMILIES[name], _text_of(words))[0] is not None


# ---------------------------------------------------------------------------
# Statement level fields
# ---------------------------------------------------------------------------


def _header_fields(pages: Sequence[PageWords], doc_id: str) -> tuple[ExtractedField, ...]:
    fields: list[ExtractedField] = []
    seen: set[FieldName] = set()
    for page in pages:
        rows = rows_of(page.words)
        for name, words in _labelled_values(rows):
            if name in seen:
                continue
            field = _build_field(doc_id, name, words, page, line_no=None)
            if field.value is None:
                continue  # a label whose neighbour did not parse names nothing
            seen.add(name)
            fields.append(field)
    return tuple(fields)


def _labelled_values(
    rows: Sequence[tuple[Word, ...]],
) -> Iterator[tuple[FieldName, tuple[Word, ...]]]:
    """Every statement label paired with the words that answer it."""
    for index, row in enumerate(rows):
        labels = _labels_in(row)
        for position, (name, length) in labels.items():
            end = min(
                (start for start in labels if start > position), default=len(row)
            )  # a label ends where the next one begins, never inside it
            value = _value_for(name, row[position + length : end], row[position], rows, index)
            if value:
                yield name, value


def _labels_in(row: Sequence[Word]) -> dict[int, tuple[FieldName, int]]:
    """Statement labels found in the row, keyed by the word they start at."""
    labels: dict[int, tuple[FieldName, int]] = {}
    position = 0
    while position < len(row):
        match = _first_label(row, position)
        if match is None:
            position += 1
            continue
        labels[position] = match
        position += match[1]
    return labels


def _first_label(row: Sequence[Word], position: int) -> tuple[FieldName, int] | None:
    for name, phrases in HEADER_LABELS.items():
        length = _match_label(row, position, phrases)
        if length is not None:
            return name, length
    return None


def _value_for(
    name: FieldName,
    trailing: Sequence[Word],
    label: Word,
    rows: Sequence[tuple[Word, ...]],
    row_index: int,
) -> tuple[Word, ...]:
    """The first token beside or beneath the label that reads as this field."""
    candidates = (trailing, _below(label, rows, row_index))
    return next((picked for words in candidates if (picked := _pick(name, words))), ())


def _below(label: Word, rows: Sequence[tuple[Word, ...]], row_index: int) -> tuple[Word, ...]:
    """The next row's words sitting directly under the label, if any are close."""
    for row in rows[row_index + 1 : row_index + 2]:
        if row[0].top - label.bottom > LABEL_BELOW_POINTS:
            return ()
        return tuple(word for word in row if word.x1 > label.x0 and word.x0 < label.x1)
    return ()


def _pick(name: FieldName, words: Sequence[Word]) -> tuple[Word, ...]:
    """The one token in a candidate span that reads as this field, if any.

    A statement level value is a single printed token, so taking the whole span
    would swallow whatever shares the line. References are the loose case, since
    any text parses as one, so a printed reference is required to carry a digit;
    that rejects the caption of the next cell along, which is exactly what a
    boxed metadata block puts there.
    """
    family = FIELD_FAMILIES[name]
    if family is FieldFamily.AMOUNT:
        # Rightmost: a totals line prints its label, then its money.
        money = [word for word in words if parse_amount_to_cents(word.text) is not None]
        return (money[-1],) if money else ()
    if family is FieldFamily.DATE:
        return next(((word,) for word in words if parse_date(word.text) is not None), ())
    return next(
        ((word,) for word in words if any(char.isdigit() for char in word.text)),
        (),
    )


# ---------------------------------------------------------------------------
# Field construction
# ---------------------------------------------------------------------------


def _build_field(
    doc_id: str,
    name: FieldName,
    words: Sequence[Word],
    page: PageWords,
    *,
    line_no: int | None,
) -> ExtractedField:
    family = FIELD_FAMILIES[name]
    text = _text_of(words)
    value, cents, parsed = _canonical(family, text)
    position = "header" if line_no is None else f"{line_no:04d}"
    return ExtractedField(
        field_id=f"fld-{doc_id}-{position}-{name}",
        doc_id=doc_id,
        line_no=line_no,
        name=name,
        family=family,
        raw_text=text,
        value=value,
        value_cents=cents,
        value_date=parsed,
        source=FieldSource.DETERMINISTIC,
        bbox=union_bbox(words, page),
        crop_kind=CropKind.EXACT_BBOX,
        signals=FieldSignals(
            validator_pass=1.0 if value is not None else 0.0,
            route=ExtractionRoute.DIGITAL_PDF,
        ),
    )


def _text_of(words: Sequence[Word]) -> str:
    return " ".join(word.text for word in words)


def _canonical(family: FieldFamily, text: str) -> tuple[str | None, int | None, date | None]:
    cleaned = text.strip()
    if family is FieldFamily.AMOUNT:
        cents = parse_amount_to_cents(cleaned)
        return (None, None, None) if cents is None else (format_cents(cents), cents, None)
    if family is FieldFamily.DATE:
        parsed = parse_date(cleaned)
        return (None, None, None) if parsed is None else (parsed.isoformat(), None, parsed)
    return cleaned or None, None, None


def _file_size(path: Path) -> int | None:
    try:
        return path.stat().st_size
    except OSError as error:
        _LOGGER.warning("cannot stat %s: %s", path, error)
        return None


def _unprocessable(
    path: Path, doc_id: str, kind: IngestErrorKind, detail: str
) -> ExtractedDocument:
    return ExtractedDocument(
        doc_id=doc_id,
        file_path=path.as_posix(),
        route=ExtractionRoute.UNPROCESSABLE,
        error=IngestError(kind=kind, detail=detail),
    )
