"""Per-field evidence for the confidence model, assembled from the extraction itself."""

import re
from collections.abc import Mapping
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
    FieldFamily,
    FieldName,
    LineType,
    Oem,
    QualityTier,
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


@dataclass(frozen=True, slots=True)
class SignalContext:
    """What a single extracted document cannot know about itself.

    The three mappings carry upstream measurements keyed by field_id or line_no:
    sampling agreement, deterministic-versus-LLM agreement, and the line types
    that give amount signs their meaning. Empty means the signal is unavailable.
    """

    oem: Oem
    period_start: date
    period_end: date
    quality_tier: QualityTier
    self_consistency: Mapping[str, float] = field(default_factory=dict)
    det_llm_agreement: Mapping[str, float] = field(default_factory=dict)
    line_types: Mapping[int, LineType] = field(default_factory=dict)


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
    """Charges are positive and credits negative; other line types are unconstrained."""
    expected = _EXPECTED_SIGNS.get(line_type)
    return expected is None or amount_cents * expected > 0


def char_ambiguity(text: str) -> float:
    """Fraction of characters drawn from the confusable glyph classes."""
    if not text:
        return 0.0
    return sum(1 for char in text if char in CONFUSABLE_GLYPHS) / len(text)


def attach_signals(doc: ExtractedDocument, context: SignalContext) -> ExtractedDocument:
    """Copy of doc whose every field carries freshly computed signals."""
    delta = crossfoot_delta_cents(doc)
    suspects = _residual_suspects(doc, delta)
    update: dict[str, Any] = {
        "header_fields": tuple(_rescored(f, context, delta, suspects) for f in doc.header_fields),
        "line_fields": tuple(_rescored(f, context, delta, suspects) for f in doc.line_fields),
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
    delta: int | None,
    suspects: frozenset[int],
) -> ExtractedField:
    family = FIELD_FAMILIES[extracted.name]
    signals = FieldSignals(
        self_consistency=context.self_consistency.get(extracted.field_id),
        det_llm_agreement=context.det_llm_agreement.get(extracted.field_id),
        validator_pass=_validator_pass(extracted, family, context),
        grammar_match=_grammar_signal(extracted, family, context),
        crossfoot_ok=_crossfoot_ok(delta) if family is FieldFamily.AMOUNT else None,
        crossfoot_residual_suspect=(
            extracted.name is FieldName.LINE_AMOUNT and extracted.line_no in suspects
        ),
        char_ambiguity=char_ambiguity(extracted.raw_text or ""),
        quality_tier=context.quality_tier,
    )
    return extracted.model_copy(update={"signals": signals})


def _crossfoot_ok(delta: int | None) -> float | None:
    if delta is None:
        return None
    return SIGNAL_TRUE if abs(delta) <= CROSSFOOT_TOLERANCE_CENTS else SIGNAL_FALSE


def _validator_pass(
    extracted: ExtractedField, family: FieldFamily, context: SignalContext
) -> float | None:
    """Typed validators only: references other than the VIN carry no arithmetic check."""
    if extracted.name is FieldName.VIN:
        return _signal(extracted.value is not None and vin_check_digit_ok(extracted.value))
    if family is FieldFamily.DATE:
        return _signal(
            extracted.value_date is not None
            and date_within_period(extracted.value_date, context.period_start, context.period_end)
        )
    if family is FieldFamily.AMOUNT:
        return _signal(extracted.value_cents is not None and _amount_sign_ok(extracted, context))
    return None


def _amount_sign_ok(extracted: ExtractedField, context: SignalContext) -> bool:
    line_no, cents = extracted.line_no, extracted.value_cents
    line_type = context.line_types.get(line_no) if line_no is not None else None
    if line_type is None or cents is None:
        return True  # no line type known, so parsing is the whole check
    return amount_sign_consistent(cents, line_type)


def _grammar_signal(
    extracted: ExtractedField, family: FieldFamily, context: SignalContext
) -> float | None:
    if family is not FieldFamily.REFERENCE or extracted.name not in _GRAMMARS[context.oem]:
        return None
    if extracted.value is None:
        return SIGNAL_FALSE
    return _signal(grammar_matches(context.oem, extracted.name, extracted.value))


def _signal(passed: bool) -> float:
    return SIGNAL_TRUE if passed else SIGNAL_FALSE
