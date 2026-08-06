"""Per-marque date and currency conventions are frozen dataset diversity."""

from datetime import date

from crossfoot.constants import Oem
from crossfoot.generator.renderers.base import format_marque_amount, format_marque_date

SAMPLE_DATE = date(2026, 7, 15)
SAMPLE_CENTS = 123_456


def test_meridian_dates_are_us_slash() -> None:
    assert format_marque_date(Oem.MERIDIAN, SAMPLE_DATE) == "07/15/2026"


def test_northstar_dates_are_iso() -> None:
    assert format_marque_date(Oem.NORTHSTAR, SAMPLE_DATE) == "2026-07-15"


def test_kaizen_dates_are_dd_mmm_yyyy() -> None:
    assert format_marque_date(Oem.KAIZEN, SAMPLE_DATE) == "15-JUL-2026"
    assert format_marque_date(Oem.KAIZEN, date(2026, 1, 3)) == "03-JAN-2026"


def test_meridian_amounts_use_leading_minus_and_dollar_everywhere() -> None:
    assert format_marque_amount(Oem.MERIDIAN, SAMPLE_CENTS) == "$1,234.56"
    assert format_marque_amount(Oem.MERIDIAN, -SAMPLE_CENTS) == "-$1,234.56"
    assert format_marque_amount(Oem.MERIDIAN, -SAMPLE_CENTS, in_totals=True) == "-$1,234.56"


def test_northstar_amounts_parenthesize_and_gate_dollar_on_totals() -> None:
    assert format_marque_amount(Oem.NORTHSTAR, SAMPLE_CENTS) == "1,234.56"
    assert format_marque_amount(Oem.NORTHSTAR, -SAMPLE_CENTS) == "(1,234.56)"
    assert format_marque_amount(Oem.NORTHSTAR, SAMPLE_CENTS, in_totals=True) == "$1,234.56"
    assert format_marque_amount(Oem.NORTHSTAR, -SAMPLE_CENTS, in_totals=True) == "($1,234.56)"


def test_kaizen_amounts_use_trailing_minus() -> None:
    assert format_marque_amount(Oem.KAIZEN, SAMPLE_CENTS) == "$1,234.56"
    assert format_marque_amount(Oem.KAIZEN, -SAMPLE_CENTS) == "$1,234.56-"


def test_small_amounts_pad_cents_to_two_digits() -> None:
    assert format_marque_amount(Oem.MERIDIAN, 500) == "$5.00"
    assert format_marque_amount(Oem.MERIDIAN, 7) == "$0.07"
