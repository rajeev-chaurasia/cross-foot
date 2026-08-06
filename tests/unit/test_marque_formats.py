"""Per-marque date and currency conventions are frozen dataset diversity."""

from datetime import date

import pytest

from crossfoot.constants import DocType, Oem
from crossfoot.generator.renderers.base import (
    MARQUE_BRANDING,
    PAYABLE_DOC_TYPES,
    format_due_date,
    format_marque_amount,
    format_marque_date,
    format_marque_line_no,
    marque_address,
    remit_address,
)

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


def test_atlas_dates_are_us_slash() -> None:
    assert format_marque_date(Oem.ATLAS, SAMPLE_DATE) == "07/15/2026"


def test_atlas_amounts_parenthesize_and_never_print_a_dollar_sign() -> None:
    assert format_marque_amount(Oem.ATLAS, SAMPLE_CENTS) == "1,234.56"
    assert format_marque_amount(Oem.ATLAS, -SAMPLE_CENTS) == "(1,234.56)"
    assert format_marque_amount(Oem.ATLAS, SAMPLE_CENTS, in_totals=True) == "1,234.56"
    assert format_marque_amount(Oem.ATLAS, -SAMPLE_CENTS, in_totals=True) == "(1,234.56)"


def test_atlas_line_numbers_are_zero_padded_to_two_digits() -> None:
    assert format_marque_line_no(Oem.ATLAS, 1) == "01"
    assert format_marque_line_no(Oem.ATLAS, 12) == "12"
    assert format_marque_line_no(Oem.ATLAS, 100) == "100"


@pytest.mark.parametrize("oem", (Oem.MERIDIAN, Oem.NORTHSTAR, Oem.KAIZEN))
def test_other_marques_print_plain_line_numbers(oem: Oem) -> None:
    assert format_marque_line_no(oem, 1) == "1"


def test_due_date_is_statement_date_plus_marque_net_terms() -> None:
    # Atlas nets 10 days and crosses the month boundary from a 07/31 statement.
    assert format_due_date(Oem.ATLAS, date(2026, 7, 31)) == "08/10/2026"
    assert format_due_date(Oem.NORTHSTAR, date(2026, 7, 31)) == "2026-08-20"


def test_atlas_issues_each_doc_type_from_its_own_division() -> None:
    addresses = {marque_address(Oem.ATLAS, doc_type) for doc_type in DocType}
    assert len(addresses) == len(DocType)
    assert all("DETROIT, MI" in address for address in addresses)
    assert marque_address(Oem.ATLAS, DocType.PARTS_STATEMENT).startswith(
        "PARTS DISTRIBUTION CENTER"
    )


@pytest.mark.parametrize("oem", (Oem.MERIDIAN, Oem.NORTHSTAR, Oem.KAIZEN))
def test_other_marques_issue_every_doc_type_from_one_address(oem: Oem) -> None:
    for doc_type in DocType:
        assert marque_address(oem, doc_type) == MARQUE_BRANDING[oem].address


@pytest.mark.parametrize("oem", tuple(Oem))
def test_every_marque_has_a_lockbox_per_payable_doc_type(oem: Oem) -> None:
    lockboxes = {remit_address(oem, doc_type) for doc_type in PAYABLE_DOC_TYPES}
    assert len(lockboxes) == len(PAYABLE_DOC_TYPES)
    assert all("P.O. B" in lockbox.upper() for lockbox in lockboxes)
