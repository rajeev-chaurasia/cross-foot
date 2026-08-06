"""Renderer protocol plus the per-marque conventions shared by all renderers."""

from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Protocol

from crossfoot.constants import DocType, FieldName, Oem
from crossfoot.models.statement import StatementDoc, StatementLine

CENTS_PER_DOLLAR = 100

# Atlas prints a zero-padded two-digit line ordinal; the others print it plain.
ATLAS_LINE_NO_WIDTH = 2

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
    net_terms_days: int


MARQUE_BRANDING: dict[Oem, MarqueBranding] = {
    Oem.MERIDIAN: MarqueBranding(
        name="Meridian Motor Company",
        tagline="Built Around the Drive",
        address="4200 Meridian Parkway, Grand Meadow, MI 48012",
        dealer_name="Grand Meadow Motors",
        dealer_code="GMM-2381",
        dealer_address="118 Commerce Drive, Grand Meadow, MI 48014",
        net_terms_days=30,
    ),
    Oem.NORTHSTAR: MarqueBranding(
        name="Northstar Automotive Group",
        tagline="Precision in Motion",
        address="One Northstar Plaza, Lakewood Heights, MI 48310",
        dealer_name="Lakewood Auto Center",
        dealer_code="064218",
        dealer_address="7450 Superior Route, Lakewood Heights, MI 48312",
        net_terms_days=20,
    ),
    Oem.KAIZEN: MarqueBranding(
        name="Kaizen Motors North America",
        tagline="Improvement in Every Mile",
        address="2100 Kaizen Boulevard, Cypress Plains, KY 40219",
        dealer_name="Cypress Plains Imports",
        dealer_code="KZ-7714",
        dealer_address="960 Delta Crossing, Cypress Plains, KY 40221",
        net_terms_days=25,
    ),
    # Atlas prints in the uppercase mainframe style its designed templates use.
    Oem.ATLAS: MarqueBranding(
        name="Atlas Motor Group",
        tagline="Engineered to Endure",
        address="ATLAS MOTOR GROUP LLC\n900 ATLAS CENTER DRIVE\nDETROIT, MI 48226",
        dealer_name="Iron Bend Motorplex",
        dealer_code="ATL-0446",
        dealer_address="2201 Foundry Road, Iron Bend, OH 44905",
        net_terms_days=10,
    ),
}

# Doc types that bill the dealer, so they print a due date and a lockbox.
PAYABLE_DOC_TYPES: frozenset[DocType] = frozenset(
    {DocType.PARTS_STATEMENT, DocType.FLOORPLAN_STATEMENT}
)

# Issuing division per (marque, doc type). Only Atlas splits its inbound mail by
# division; every other pair falls back to the single MarqueBranding.address.
ISSUER_ADDRESSES: dict[tuple[Oem, DocType], str] = {
    (Oem.ATLAS, DocType.WARRANTY_CREDIT_MEMO): (
        "DEALER FINANCIAL SERVICES\nP.O. BOX 218800\nDETROIT, MI 48226-8800"
    ),
    (Oem.ATLAS, DocType.PARTS_STATEMENT): (
        "PARTS DISTRIBUTION CENTER\nP.O. BOX 218400\nDETROIT, MI 48226-8400"
    ),
    (Oem.ATLAS, DocType.FLOORPLAN_STATEMENT): (
        "ATLAS FINANCIAL SERVICES\nA DIVISION OF ATLAS MOTOR GROUP\n"
        "P.O. BOX 219200\nDETROIT, MI 48226-9200"
    ),
    (Oem.ATLAS, DocType.INCENTIVE_STATEMENT): (
        "SALES OPERATIONS - INCENTIVES\nP.O. BOX 218600\nDETROIT, MI 48226-8600"
    ),
}

# Lockbox each marque collects payments in, one per payable doc type.
REMIT_ADDRESSES: dict[tuple[Oem, DocType], str] = {
    (Oem.MERIDIAN, DocType.PARTS_STATEMENT): (
        "Meridian Motor Company\nParts Division Lockbox\nP.O. Box 74210\nChicago, IL 60675-4210"
    ),
    (Oem.MERIDIAN, DocType.FLOORPLAN_STATEMENT): (
        "Meridian Credit Company\nFloorplan Lockbox\nP.O. Box 74580\nChicago, IL 60675-4580"
    ),
    (Oem.NORTHSTAR, DocType.PARTS_STATEMENT): (
        "Northstar Automotive Group\nParts Lockbox 3120\nP.O. Box 92640\nDallas, TX 75392-2640"
    ),
    (Oem.NORTHSTAR, DocType.FLOORPLAN_STATEMENT): (
        "Northstar Financial Services\nFloorplan Lockbox 3155\n"
        "P.O. Box 92685\nDallas, TX 75392-2685"
    ),
    (Oem.KAIZEN, DocType.PARTS_STATEMENT): (
        "Kaizen Motors North America\nParts Remittance Center\n"
        "P.O. Box 60318\nAtlanta, GA 30353-0318"
    ),
    (Oem.KAIZEN, DocType.FLOORPLAN_STATEMENT): (
        "Kaizen Financial Services\nFloorplan Remittance Center\n"
        "P.O. Box 60742\nAtlanta, GA 30353-0742"
    ),
    (Oem.ATLAS, DocType.PARTS_STATEMENT): (
        "ATLAS MOTOR GROUP LLC\nPARTS DIVISION LOCKBOX\nP.O. BOX 88012\nCHICAGO, IL 60680-8012"
    ),
    (Oem.ATLAS, DocType.FLOORPLAN_STATEMENT): (
        "ATLAS FINANCIAL SERVICES\nFLOORPLAN LOCKBOX\nP.O. BOX 88440\nCHICAGO, IL 60680-8440"
    ),
}


def marque_address(oem: Oem, doc_type: DocType) -> str:
    """Issuing address for one doc type, defaulting to the marque address."""
    return ISSUER_ADDRESSES.get((oem, doc_type), MARQUE_BRANDING[oem].address)


def remit_address(oem: Oem, doc_type: DocType) -> str:
    """Lockbox a payable doc type directs payment to."""
    return REMIT_ADDRESSES[oem, doc_type]


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
    # meridian and atlas both print MM/DD/YYYY
    return f"{value.month:02d}/{value.day:02d}/{value.year}"


def format_due_date(oem: Oem, statement_date: date) -> str:
    """Statement date plus the marque's net terms, in the marque date format."""
    terms = timedelta(days=MARQUE_BRANDING[oem].net_terms_days)
    return format_marque_date(oem, statement_date + terms)


def format_marque_amount(oem: Oem, amount_cents: int, *, in_totals: bool = False) -> str:
    """Deterministic per-marque currency convention.

    meridian: leading minus, dollar sign everywhere. northstar: parentheses
    negatives, dollar sign only in totals. kaizen: trailing minus, dollar sign
    everywhere. atlas: parentheses negatives, never a dollar sign, because its
    designed templates carry the currency in the column captions instead.
    """
    magnitude = grouped_amount(amount_cents)
    negative = amount_cents < 0
    if oem is Oem.NORTHSTAR:
        body = f"${magnitude}" if in_totals else magnitude
        return f"({body})" if negative else body
    if oem is Oem.KAIZEN:
        return f"${magnitude}-" if negative else f"${magnitude}"
    if oem is Oem.ATLAS:
        return f"({magnitude})" if negative else magnitude
    return f"-${magnitude}" if negative else f"${magnitude}"


def format_marque_line_no(oem: Oem, line_no: int) -> str:
    """Deterministic per-marque line ordinal; atlas zero-pads to two digits."""
    if oem is Oem.ATLAS:
        return f"{line_no:0{ATLAS_LINE_NO_WIDTH}d}"
    return str(line_no)
