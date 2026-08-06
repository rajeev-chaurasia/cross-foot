"""Pure parsing and normalization helpers shared by deterministic extractors."""

import re
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

CENTS_PER_DOLLAR = 100

# Characters dropped from amount strings before numeric parsing.
_IGNORED_AMOUNT_CHARS = str.maketrans("", "", "$, ")
# Plain decimal number: no exponent, no NaN or Infinity spellings.
_DECIMAL_NUMBER = re.compile(r"\d+(\.\d*)?|\.\d+")

_MDY_DATE = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})")
_ISO_DATE = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})")
_DMY_DATE = re.compile(r"(\d{1,2})-([A-Za-z]{3})-(\d{4})")

_MONTH_ABBREVIATIONS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}

_REFERENCE_SEPARATORS = re.compile(r"[\- ]")


def parse_amount_to_cents(text: str) -> int | None:
    """Parse a currency string into integer cents; None for blank or garbage input.

    Handles "$1,234.56", parentheses negatives "(123.45)", trailing minus
    "123.45-", and leading minus. Decimal arithmetic only, never float.
    """
    cleaned = text.strip()
    negative = False
    if cleaned.startswith("(") and cleaned.endswith(")"):
        negative, cleaned = True, cleaned[1:-1]
    if cleaned.endswith("-"):
        negative, cleaned = True, cleaned[:-1]
    cleaned = cleaned.translate(_IGNORED_AMOUNT_CHARS)
    if cleaned.startswith("-"):
        negative, cleaned = True, cleaned[1:]
    if not _DECIMAL_NUMBER.fullmatch(cleaned):
        return None
    cents = (Decimal(cleaned) * CENTS_PER_DOLLAR).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    magnitude = int(cents)
    return -magnitude if negative else magnitude


def parse_date(text: str) -> date | None:
    """Parse MM/DD/YYYY, YYYY-MM-DD, or DD-MMM-YYYY (15-JUL-2026); None otherwise."""
    cleaned = text.strip()
    if match := _MDY_DATE.fullmatch(cleaned):
        month, day, year = (int(group) for group in match.groups())
        return _safe_date(year, month, day)
    if match := _ISO_DATE.fullmatch(cleaned):
        year, month, day = (int(group) for group in match.groups())
        return _safe_date(year, month, day)
    if match := _DMY_DATE.fullmatch(cleaned):
        day_text, month_text, year_text = match.groups()
        month_no = _MONTH_ABBREVIATIONS.get(month_text.casefold())
        if month_no is None:
            return None
        return _safe_date(int(year_text), month_no, int(day_text))
    return None


def normalize_reference(text: str) -> str:
    """Uppercase, drop "-" and " " separators, and strip leading zeros."""
    return _REFERENCE_SEPARATORS.sub("", text).upper().lstrip("0")


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None
