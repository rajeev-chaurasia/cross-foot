"""Per-field evidence for the confidence model, assembled from the extraction itself.

FEATURES COME FROM THE ARTIFACT, LABELS COME FROM TRUTH. That split is the thing
the whole eval rests on, so it is worth naming here rather than leaving it to be
inferred. Everything this module produces is a feature, and a feature is only
honest if the pipeline could compute it for a document nobody holds an answer key
for. Fitting still needs truth, but only to say whether a field was read
correctly; that label lives with the caller and never reaches a `FieldSignals`.

Four inputs used to arrive from the dataset manifest and have been replaced:

- the generator's quality tier, by the route the router read off the file bytes;
- the true statement period, by the dates this same extraction produced;
- the true marque, by the marque this extraction's own references vote for;
- the true per-line type, by nothing, because line type is never extracted.

`SignalContext` carries the only evidence a field's own row cannot: measurements
the extractor made while reading the page.
"""

import re
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from crossfoot.constants import (
    FIELD_FAMILIES,
    REF_GRAMMARS,
    VIN_CHAR_VALUES,
    VIN_CHECK_DIGIT_INDEX,
    VIN_LENGTH,
    VIN_POSITION_WEIGHTS,
    ExtractionRoute,
    FieldFamily,
    FieldName,
    LineType,
    Oem,
)
from crossfoot.models.extraction import ExtractedDocument, ExtractedField, FieldSignals

# A date is plausible inside the statement period widened by this much either way.
PERIOD_GRACE_DAYS = 60

# Frozen confusable glyph classes {O0, I1l, S5, B8, Z2}. Lowercase o and i are
# excluded on purpose: only the glyphs the marque fonts actually confuse count.
CONFUSABLE_GLYPHS = frozenset("O0I1lS5B8Z2")

# A document foots when the printed total lands within one cent of its lines.
CROSSFOOT_TOLERANCE_CENTS = 1

VIN_CHECK_MODULUS = 11
VIN_CHECK_REMAINDER_TEN = 10
VIN_CHECK_DIGIT_TEN = "X"

SIGNAL_TRUE = 1.0
SIGNAL_FALSE = 0.0

# Sign a line type implies. Adjustments and payments legitimately run either
# way, so their amounts are checked for parsing only.
_EXPECTED_SIGNS: dict[LineType, int] = {LineType.CHARGE: 1, LineType.CREDIT: -1}

_GRAMMARS: dict[Oem, dict[FieldName, re.Pattern[str]]] = {
    oem: {name: re.compile(pattern) for name, pattern in grammars.items()}
    for oem, grammars in REF_GRAMMARS.items()
}

# Field names some marque defines a grammar for. A reference outside this set
# carries no grammar signal at all rather than a failed one.
_GRAMMAR_FIELDS: frozenset[FieldName] = frozenset(
    name for grammars in _GRAMMARS.values() for name in grammars
)


@dataclass(frozen=True, slots=True)
class SignalContext:
    """Upstream measurements no single field carries on its own row.

    Both mappings are keyed by field_id and hold something the extractor measured
    while reading the page: agreement across the k=2 vision samples, and agreement
    between the deterministic and the LLM reading of the same document. A field
    the mapping does not name keeps whatever its own row already carries, so
    rescoring never discards a measurement it cannot recompute.

    Nothing else belongs here, and in particular nothing from the dataset
    manifest. Every other signal is read off the artifact or the extraction inside
    `attach_signals`, which is what makes a confidence score something a document
    with no answer key could earn.
    """

    self_consistency: Mapping[str, float] = field(default_factory=dict)
    det_llm_agreement: Mapping[str, float] = field(default_factory=dict)


def vin_check_digit_ok(vin: str) -> bool:
    """ISO 3779 check digit; false on wrong length or non-VIN glyphs."""
    if len(vin) != VIN_LENGTH:
        return False
    total = 0
    for index, char in enumerate(vin):
        if index == VIN_CHECK_DIGIT_INDEX:
            continue  # the check digit itself may be X, which carries no value
        value = VIN_CHAR_VALUES.get(char)
        if value is None:
            return False
        total += value * VIN_POSITION_WEIGHTS[index]
    remainder = total % VIN_CHECK_MODULUS
    expected = VIN_CHECK_DIGIT_TEN if remainder == VIN_CHECK_REMAINDER_TEN else str(remainder)
    return vin[VIN_CHECK_DIGIT_INDEX] == expected


def grammar_matches(oem: Oem, name: FieldName, value: str) -> bool:
    """Fullmatch against the marque grammar; false when the marque defines none."""
    pattern = _GRAMMARS[oem].get(name)
    return pattern is not None and pattern.fullmatch(value) is not None


def date_within_period(value: date, period_start: date, period_end: date) -> bool:
    """Inside the statement period widened by the grace window, inclusive."""
    grace = timedelta(days=PERIOD_GRACE_DAYS)
    return period_start - grace <= value <= period_end + grace


def amount_sign_consistent(amount_cents: int, line_type: LineType) -> bool:
    """Charges are positive and credits negative; other line types are unconstrained.

    No signal calls this any more. Line type is never extracted from a document,
    so gating a confidence feature on it would score an extraction against a fact
    only the generator holds. Kept because it is the documented arithmetic and the
    reconciliation engine still reasons in these terms, and because naming why it
    is unused is worth more than deleting it quietly.
    """
    expected = _EXPECTED_SIGNS.get(line_type)
    return expected is None or amount_cents * expected > 0


def char_ambiguity(text: str) -> float:
    """Fraction of characters drawn from the confusable glyph classes."""
    if not text:
        return 0.0
    return sum(1 for char in text if char in CONFUSABLE_GLYPHS) / len(text)


def infer_oem(doc: ExtractedDocument) -> Oem | None:
    """The marque whose grammars this extraction's own references fit best.

    One vote per extracted reference value that fullmatches, counted across every
    marque; the most voted marque wins outright. A tie, or a document with no
    matching reference at all, yields None, and the grammar signal then falls back
    to asking whether any marque would recognize a value.

    A document votes with everything it printed, so a single misread reference is
    one vote among a page of them and cannot elect its own marque.
    """
    votes: dict[Oem, int] = dict.fromkeys(Oem, 0)
    for extracted in _reference_values(doc):
        for oem in Oem:
            if grammar_matches(oem, extracted.name, extracted.value or ""):
                votes[oem] += 1
    ranked = sorted(votes.items(), key=lambda item: item[1], reverse=True)
    if ranked[0][1] == 0:
        return None  # nothing matched, so there is nothing to elect
    if ranked[0][1] == ranked[1][1]:
        return None  # nothing to choose between them, so choose nothing
    return ranked[0][0]


def date_windows(doc: ExtractedDocument) -> dict[str, tuple[date, date]]:
    """Per field_id, the window that field's date is plausible inside.

    Anchored on the statement date this same extraction produced, which is the
    only period marker the pipeline reads off a page; the true period is a
    manifest fact and is not available. No date is ever checked against a window
    it helped define, so the statement date is judged against the middle of the
    document's other dates instead. The middle rather than the span is what keeps
    one wild misread from stretching the window until nothing can fall outside it.

    A field with no other dated field beside it gets no entry at all, and the
    signal is then absent rather than vacuously true.
    """
    dated = [
        (extracted.field_id, extracted.value_date)
        for extracted in _all_fields(doc)
        if extracted.value_date is not None
    ]
    anchor_id, anchor = _statement_anchor(doc)
    windows: dict[str, tuple[date, date]] = {}
    for field_id, _ in dated:
        if anchor is not None and field_id != anchor_id:
            centre = anchor
        else:
            others = [value for other_id, value in dated if other_id != field_id]
            if not others:
                continue
            centre = _middle_date(others)
        windows[field_id] = (centre, centre)
    return windows


def attach_signals(
    doc: ExtractedDocument, context: SignalContext | None = None
) -> ExtractedDocument:
    """Copy of doc whose every field carries freshly computed signals.

    The document is the only evidence: the route it was read by, the dates and
    references it yielded, and the arithmetic over its own amounts. Nothing is
    passed in from a manifest, so this is exactly the computation a production
    document with no answer key would get.
    """
    context = context or SignalContext()
    delta = crossfoot_delta_cents(doc)
    suspects = _residual_suspects(doc, delta)
    oem = infer_oem(doc)
    windows = date_windows(doc)
    update: dict[str, Any] = {
        "header_fields": tuple(
            _rescored(f, context, doc.route, oem, windows, delta, suspects)
            for f in doc.header_fields
        ),
        "line_fields": tuple(
            _rescored(f, context, doc.route, oem, windows, delta, suspects) for f in doc.line_fields
        ),
    }
    if delta is not None:
        update["crossfoot_delta_cents"] = delta
    return doc.model_copy(update=update)


def crossfoot_delta_cents(doc: ExtractedDocument) -> int | None:
    """StatementDoc.crossfoot_delta_cents() arithmetic over extracted values.

    None when no total was extracted, since there is then nothing to foot against.
    """
    total = _header_cents(doc, FieldName.TOTAL)
    if total is None:
        return None
    carried = _header_cents(doc, FieldName.PREVIOUS_BALANCE) or 0
    return total - (carried + sum(cents for _, cents in _line_amounts(doc)))


def _statement_anchor(doc: ExtractedDocument) -> tuple[str | None, date | None]:
    """The extracted statement date and the field it came from, or (None, None)."""
    for extracted in doc.header_fields:
        if extracted.name is FieldName.STATEMENT_DATE and extracted.value_date is not None:
            return extracted.field_id, extracted.value_date
    return None, None


def _middle_date(values: Sequence[date]) -> date:
    """Median date, taking the later of the two middles on an even count."""
    ordinals = sorted(value.toordinal() for value in values)
    return date.fromordinal(int(statistics.median_high(ordinals)))


def _all_fields(doc: ExtractedDocument) -> tuple[ExtractedField, ...]:
    return (*doc.header_fields, *doc.line_fields)


def _reference_values(doc: ExtractedDocument) -> list[ExtractedField]:
    return [
        extracted
        for extracted in _all_fields(doc)
        if extracted.name in _GRAMMAR_FIELDS and extracted.value is not None
    ]


def _residual_suspects(doc: ExtractedDocument, delta: int | None) -> frozenset[int]:
    """Line numbers whose amount alone explains a failed crossfoot.

    The residual for a line is the total minus every other line, which is the
    document delta plus that line's own amount. Flagging happens only when
    exactly one line is plausible, so a broken page is never blamed wholesale.
    """
    if delta is None or abs(delta) <= CROSSFOOT_TOLERANCE_CENTS:
        return frozenset()
    flagged = [
        line_no
        for line_no, cents in _line_amounts(doc)
        if _plausible_residual(delta + cents, cents)
    ]
    return frozenset(flagged) if len(flagged) == 1 else frozenset()


def _plausible_residual(residual: int, extracted_cents: int) -> bool:
    """A misread changes digits, not the sign, so a sign flip is not this line's value."""
    return residual != 0 and (residual > 0) == (extracted_cents > 0)


def _line_amounts(doc: ExtractedDocument) -> list[tuple[int, int]]:
    return [
        (extracted.line_no, extracted.value_cents)
        for extracted in doc.line_fields
        if extracted.name is FieldName.LINE_AMOUNT
        and extracted.line_no is not None
        and extracted.value_cents is not None
    ]


def _header_cents(doc: ExtractedDocument, name: FieldName) -> int | None:
    return next(
        (f.value_cents for f in doc.header_fields if f.name is name and f.value_cents is not None),
        None,
    )


def _rescored(
    extracted: ExtractedField,
    context: SignalContext,
    route: ExtractionRoute,
    oem: Oem | None,
    windows: Mapping[str, tuple[date, date]],
    delta: int | None,
    suspects: frozenset[int],
) -> ExtractedField:
    family = FIELD_FAMILIES[extracted.name]
    carried = extracted.signals
    signals = FieldSignals(
        # An extractor that measured agreement while reading keeps it: rescoring
        # recomputes what it can see and must not erase what it cannot.
        self_consistency=context.self_consistency.get(extracted.field_id, carried.self_consistency),
        det_llm_agreement=context.det_llm_agreement.get(
            extracted.field_id, carried.det_llm_agreement
        ),
        validator_pass=_validator_pass(extracted, family, windows),
        grammar_match=_grammar_signal(extracted, family, oem),
        crossfoot_ok=_crossfoot_ok(delta) if family is FieldFamily.AMOUNT else None,
        crossfoot_residual_suspect=(
            extracted.name is FieldName.LINE_AMOUNT and extracted.line_no in suspects
        ),
        char_ambiguity=char_ambiguity(extracted.raw_text or ""),
        route=route,
    )
    return extracted.model_copy(update={"signals": signals})


def _crossfoot_ok(delta: int | None) -> float | None:
    if delta is None:
        return None
    return SIGNAL_TRUE if abs(delta) <= CROSSFOOT_TOLERANCE_CENTS else SIGNAL_FALSE


def _validator_pass(
    extracted: ExtractedField, family: FieldFamily, windows: Mapping[str, tuple[date, date]]
) -> float | None:
    """Typed validators only: references other than the VIN carry no arithmetic check."""
    if extracted.name is FieldName.VIN:
        return _signal(extracted.value is not None and vin_check_digit_ok(extracted.value))
    if family is FieldFamily.DATE:
        return _date_signal(extracted, windows)
    if family is FieldFamily.AMOUNT:
        # A parse, and only a parse. The sign check phase 2 described needed the
        # line type, which no extractor produces; the arithmetic that stands in
        # for it is crossfoot_ok and the residual suspect beside it.
        return _signal(extracted.value_cents is not None)
    return None


def _date_signal(
    extracted: ExtractedField, windows: Mapping[str, tuple[date, date]]
) -> float | None:
    """False when the text did not parse, absent when the extraction anchors no window."""
    if extracted.value_date is None:
        return SIGNAL_FALSE  # visible from the text alone, so it is always known
    window = windows.get(extracted.field_id)
    if window is None:
        return None
    return _signal(date_within_period(extracted.value_date, *window))


def _grammar_signal(
    extracted: ExtractedField, family: FieldFamily, oem: Oem | None
) -> float | None:
    """Match against the marque the document voted for, or against all of them.

    The true marque is a manifest fact. When the extraction elects one, the check
    is that marque's grammar; when it cannot, the value is asked only whether some
    marque would recognize it, which is weaker and honest.
    """
    if family is not FieldFamily.REFERENCE or extracted.name not in _GRAMMAR_FIELDS:
        return None
    if extracted.value is None:
        return SIGNAL_FALSE
    if oem is not None:
        return _signal(grammar_matches(oem, extracted.name, extracted.value))
    return _signal(any(grammar_matches(each, extracted.name, extracted.value) for each in Oem))


def _signal(passed: bool) -> float:
    return SIGNAL_TRUE if passed else SIGNAL_FALSE
