"""Edge cases for crossfoot.extraction.normalize beyond the contract fixtures."""

from datetime import date

import pytest

from crossfoot.extraction.normalize import (
    normalize_reference,
    parse_amount_to_cents,
    parse_date,
)


@pytest.mark.parametrize(
    ("text", "cents"),
    [
        ("0", 0),
        ("0.00", 0),
        ("$0.00", 0),
        ("(0.00)", 0),
        ("1,000,000.00", 100_000_000),
        ("$1,000,000.00", 100_000_000),
        ("1234.56", 123_456),
        ("$1,234.56", 123_456),
        ("(123.45)", -12_345),
        ("123.45-", -12_345),
        ("-123.45", -12_345),
        ("$-123.45", -12_345),
        ("  45.00  ", 4_500),
        ("123", 12_300),
        ("123.4", 12_340),
        ("1.005", 101),  # half-up rounding via Decimal, no float drift
    ],
)
def test_parse_amount_to_cents(text: str, cents: int) -> None:
    assert parse_amount_to_cents(text) == cents


@pytest.mark.parametrize(
    "text",
    ["", "   ", "abc", "12.34.56", "$", "-", "()", "1e5", "NaN", "Infinity", "12a.45"],
)
def test_parse_amount_rejects_garbage(text: str) -> None:
    assert parse_amount_to_cents(text) is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("07/15/2026", date(2026, 7, 15)),
        ("7/5/2026", date(2026, 7, 5)),
        ("2026-07-16", date(2026, 7, 16)),
        ("15-JUL-2026", date(2026, 7, 15)),
        ("15-jul-2026", date(2026, 7, 15)),
        ("01-Dec-1999", date(1999, 12, 1)),
    ],
)
def test_parse_date_formats(text: str, expected: date) -> None:
    assert parse_date(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "not a date",
        "13/40/2026",  # bad month and day
        "02/30/2026",  # bad day for the month
        "2026-13-01",  # bad month in ISO form
        "15-XXX-2026",  # unknown month abbreviation
        "15-JULY-2026",  # abbreviation must be three letters
        "07/15/26",  # two-digit year unsupported
    ],
)
def test_parse_date_rejects_garbage(text: str) -> None:
    assert parse_date(text) is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("ro-000123", "RO000123"),
        ("RO 000 123", "RO000123"),
        ("000123", "123"),
        ("0", ""),
        ("", ""),
        ("a b-c", "ABC"),
        ("K123-456789", "K123456789"),
    ],
)
def test_normalize_reference(text: str, expected: str) -> None:
    assert normalize_reference(text) == expected
