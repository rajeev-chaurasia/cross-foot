"""Renderer protocol plus the per-marque conventions shared by all renderers."""

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Protocol

from crossfoot.constants import DocType, FieldName, Oem
from crossfoot.models.statement import StatementDoc, StatementLine

CENTS_PER_DOLLAR = 100

# Locale-independent month abbreviations for the kaizen DD-MMM-YYYY convention.
MONTH_ABBREVIATIONS: tuple[str, ...] = (
    "JAN",
    "FEB",
    "MAR",
    "APR",
    "MAY",
    "JUN",
    "JUL",
    "AUG",
    "SEP",
    "OCT",
    "NOV",
    "DEC",
)


class Renderer(Protocol):
    """Writes one artifact and reports every string it printed.

    The returned map is the manifest rendered_values contract: keys are
    "header:{field_name}" for statement-level fields and "{line_no}:{field_name}"
    for line fields; values are the exact strings printed into the artifact.
    """

    def render(
        self, doc: StatementDoc, template_id: str, seed: int, out_path: Path
    ) -> dict[str, str]: ...


def header_key(field: FieldName) -> str:
    return f"header:{field}"


def line_key(line_no: int, field: FieldName) -> str:
    return f"{line_no}:{field}"


# Line columns each doc type prints. Templates and the tabular renderers stay in
# sync with this table, and rendered_values covers exactly these fields per line.
DOC_LINE_FIELDS: dict[DocType, tuple[FieldName, ...]] = {
    DocType.PARTS_STATEMENT: (
        FieldName.LINE_DATE,
        FieldName.INVOICE_NUMBER,
        FieldName.DESCRIPTION,
        FieldName.LINE_AMOUNT,
    ),
    DocType.WARRANTY_CREDIT_MEMO: (
        FieldName.LINE_DATE,
        FieldName.CLAIM_NUMBER,
        FieldName.RO_NUMBER,
        FieldName.VIN,
        FieldName.DESCRIPTION,
        FieldName.LINE_AMOUNT,
    ),
    DocType.FLOORPLAN_STATEMENT: (
        FieldName.LINE_DATE,
        FieldName.VIN,
        FieldName.DESCRIPTION,
        FieldName.LINE_AMOUNT,
    ),
    DocType.INCENTIVE_STATEMENT: (
        FieldName.LINE_DATE,
        FieldName.PROGRAM_CODE,
        FieldName.VIN,
        FieldName.DESCRIPTION,
        FieldName.LINE_AMOUNT,
    ),
}

LINE_REFERENCE_FIELDS: tuple[FieldName, ...] = (
    FieldName.CLAIM_NUMBER,
    FieldName.RO_NUMBER,
    FieldName.VIN,
    FieldName.INVOICE_NUMBER,
    FieldName.PROGRAM_CODE,
)


def line_reference(line: StatementLine, field: FieldName) -> str | None:
    """Raw reference value for a line field; None when the line does not carry it."""
    if field is FieldName.CLAIM_NUMBER:
        return line.claim_number
    if field is FieldName.RO_NUMBER:
        return line.ro_number
    if field is FieldName.VIN:
        return line.vin
    if field is FieldName.INVOICE_NUMBER:
        return line.invoice_number
    if field is FieldName.PROGRAM_CODE:
        return line.program_code
    raise ValueError(f"{field} is not a line reference field")


@dataclass(frozen=True)
class MarqueBranding:
    """Fictional marque and dealer identity printed on artifacts.

    Every name, tagline, and address is invented; no real OEM names or
    trademarks appear anywhere in the rendered documents.
    """

    name: str
    tagline: str
    address: str
    dealer_name: str
    dealer_code: str
    dealer_address: str


MARQUE_BRANDING: dict[Oem, MarqueBranding] = {
    Oem.MERIDIAN: MarqueBranding(
        name="Meridian Motor Company",
        tagline="Built Around the Drive",
        address="4200 Meridian Parkway, Grand Meadow, MI 48012",
        dealer_name="Grand Meadow Motors",
        dealer_code="GMM-2381",
        dealer_address="118 Commerce Drive, Grand Meadow, MI 48014",
    ),
    Oem.NORTHSTAR: MarqueBranding(
        name="Northstar Automotive Group",
        tagline="Precision in Motion",
        address="One Northstar Plaza, Lakewood Heights, MI 48310",
        dealer_name="Lakewood Auto Center",
        dealer_code="064218",
        dealer_address="7450 Superior Route, Lakewood Heights, MI 48312",
    ),
    Oem.KAIZEN: MarqueBranding(
        name="Kaizen Motors North America",
        tagline="Improvement in Every Mile",
        address="2100 Kaizen Boulevard, Cypress Plains, KY 40219",
        dealer_name="Cypress Plains Imports",
        dealer_code="KZ-7714",
        dealer_address="960 Delta Crossing, Cypress Plains, KY 40221",
    ),
    # Atlas template variants land later; its branding and conventions are ready.
    Oem.ATLAS: MarqueBranding(
        name="Atlas Consolidated Motors",
        tagline="Carrying You Further",
        address="500 Atlas Gateway, Iron Bend, OH 44903",
        dealer_name="Iron Bend Motorplex",
        dealer_code="ATL-0446",
        dealer_address="2201 Foundry Road, Iron Bend, OH 44905",
    ),
}

DOC_TITLES: dict[DocType, str] = {
    DocType.PARTS_STATEMENT: "Parts Statement",
    DocType.WARRANTY_CREDIT_MEMO: "Warranty Credit Memo",
    DocType.FLOORPLAN_STATEMENT: "Floorplan Statement",
    DocType.INCENTIVE_STATEMENT: "Incentive Statement",
}


def grouped_amount(amount_cents: int) -> str:
    """Unsigned comma-grouped dollars and cents, e.g. 123456 -> '1,234.56'."""
    dollars, cents = divmod(abs(amount_cents), CENTS_PER_DOLLAR)
    return f"{dollars:,}.{cents:02d}"


def format_marque_date(oem: Oem, value: date) -> str:
    """Deterministic per-marque date convention."""
    if oem is Oem.NORTHSTAR:
        return value.isoformat()
    if oem is Oem.KAIZEN:
        return f"{value.day:02d}-{MONTH_ABBREVIATIONS[value.month - 1]}-{value.year}"
    # meridian, and atlas until its designed variants land
    return f"{value.month:02d}/{value.day:02d}/{value.year}"


def format_marque_amount(oem: Oem, amount_cents: int, *, in_totals: bool = False) -> str:
    """Deterministic per-marque currency convention.

    meridian: leading minus, dollar sign everywhere. northstar: parentheses
    negatives, dollar sign only in totals. kaizen: trailing minus, dollar sign
    everywhere. atlas follows meridian until its designed variants land.
    """
    magnitude = grouped_amount(amount_cents)
    negative = amount_cents < 0
    if oem is Oem.NORTHSTAR:
        body = f"${magnitude}" if in_totals else magnitude
        return f"({body})" if negative else body
    if oem is Oem.KAIZEN:
        return f"${magnitude}-" if negative else f"${magnitude}"
    return f"-${magnitude}" if negative else f"${magnitude}"
