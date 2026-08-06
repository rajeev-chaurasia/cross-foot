"""Deterministic synthetic ledger: three dealers, four months, four schedules."""

import calendar
import hashlib
import random
from dataclasses import dataclass
from datetime import date

from crossfoot.constants import (
    REF_GRAMMARS,
    VIN_CHAR_VALUES,
    VIN_CHECK_DIGIT_INDEX,
    VIN_LENGTH,
    VIN_POSITION_WEIGHTS,
    FieldName,
    Oem,
    ScheduleType,
)
from crossfoot.models.ledger import Dealer, LedgerBook, LedgerEntry

# Months covered by the synthetic books: April through July 2026.
GENERATION_MONTHS: tuple[tuple[int, int], ...] = ((2026, 4), (2026, 5), (2026, 6), (2026, 7))

DEALERS: tuple[Dealer, ...] = (
    Dealer(dealer_id="dlr-meridian", name="Lakeshore Meridian of Columbus", oem=Oem.MERIDIAN),
    Dealer(dealer_id="dlr-northstar", name="Northstar Motors of Traverse City", oem=Oem.NORTHSTAR),
    Dealer(dealer_id="dlr-kaizen", name="Kaizen Auto Gallery of Bellevue", oem=Oem.KAIZEN),
)

# Signed-amount convention (kept everywhere downstream): every entry books the
# positive magnitude of its obligation; direction lives in the schedule type.
SCHEDULE_AMOUNT_RANGES: dict[ScheduleType, tuple[int, int]] = {
    ScheduleType.WARRANTY_RECEIVABLE: (8_000, 450_000),
    ScheduleType.PARTS_PAYABLE: (5_000, 900_000),
    ScheduleType.FLOORPLAN_LIABILITY: (2_200_000, 7_500_000),
    ScheduleType.INCENTIVE_RECEIVABLE: (50_000, 300_000),
}

SCHEDULE_REFERENCE_FIELDS: dict[ScheduleType, tuple[FieldName, ...]] = {
    ScheduleType.WARRANTY_RECEIVABLE: (FieldName.CLAIM_NUMBER, FieldName.RO_NUMBER, FieldName.VIN),
    ScheduleType.PARTS_PAYABLE: (FieldName.INVOICE_NUMBER,),
    ScheduleType.FLOORPLAN_LIABILITY: (FieldName.VIN,),
    ScheduleType.INCENTIVE_RECEIVABLE: (FieldName.PROGRAM_CODE, FieldName.VIN),
}

_ENTRY_COUNT_RANGES: dict[ScheduleType, tuple[int, int]] = {
    ScheduleType.WARRANTY_RECEIVABLE: (5, 12),
    ScheduleType.PARTS_PAYABLE: (6, 14),
    ScheduleType.FLOORPLAN_LIABILITY: (4, 8),
    ScheduleType.INCENTIVE_RECEIVABLE: (4, 9),
}

_GL_ACCOUNTS: dict[ScheduleType, str] = {
    ScheduleType.WARRANTY_RECEIVABLE: "1210",
    ScheduleType.PARTS_PAYABLE: "2010",
    ScheduleType.FLOORPLAN_LIABILITY: "2310",
    ScheduleType.INCENTIVE_RECEIVABLE: "1230",
}

_OEM_CORPORATE: dict[Oem, str] = {
    Oem.MERIDIAN: "Meridian Motor Company",
    Oem.NORTHSTAR: "Northstar Automotive Group",
    Oem.KAIZEN: "Kaizen Motor Corporation",
    Oem.ATLAS: "Atlas Motorwerke North America",
}

_OEM_CAPTIVE: dict[Oem, str] = {
    Oem.MERIDIAN: "Meridian Credit Company",
    Oem.NORTHSTAR: "Northstar Financial Services",
    Oem.KAIZEN: "Kaizen Financial Services",
    Oem.ATLAS: "Atlas Capital North America",
}

# WMI prefixes keep the first three VIN positions marque-stable and check-digit legal.
_OEM_WMI: dict[Oem, str] = {
    Oem.MERIDIAN: "1ME",
    Oem.NORTHSTAR: "1NS",
    Oem.KAIZEN: "JKZ",
    Oem.ATLAS: "3AT",
}

_DESCRIPTIONS: dict[ScheduleType, tuple[str, ...]] = {
    ScheduleType.WARRANTY_RECEIVABLE: (
        "Water pump replacement",
        "Transmission valve body",
        "Infotainment head unit exchange",
        "Turbocharger actuator",
        "Door latch recall remedy",
        "Oxygen sensor and harness",
        "HVAC blend door actuator",
        "Battery module exchange",
    ),
    ScheduleType.PARTS_PAYABLE: (
        "Weekly stock order",
        "Special order parts",
        "Collision parts order",
        "Emergency VOR order",
        "Accessory pre-load kit",
        "Maintenance parts restock",
    ),
    ScheduleType.FLOORPLAN_LIABILITY: (
        "New unit floorplan advance",
        "Demo unit floorplan advance",
        "Fleet unit floorplan advance",
    ),
    ScheduleType.INCENTIVE_RECEIVABLE: (
        "Volume growth bonus",
        "Customer loyalty program",
        "Conquest incentive",
        "Dealer cash program",
        "EV launch support",
    ),
}

_DIGITS = "0123456789"
_VIN_ALPHABET = "".join(sorted(VIN_CHAR_VALUES))


@dataclass(frozen=True)
class ReferenceSet:
    """Reference values for one record; only the schedule-relevant fields are set."""

    claim_number: str | None = None
    ro_number: str | None = None
    vin: str | None = None
    invoice_number: str | None = None
    program_code: str | None = None


def record_seed(master_seed: int, record_id: str) -> int:
    """Contract rule: first 8 bytes of sha256(f"{master_seed}:{record_id}"), big-endian."""
    digest = hashlib.sha256(f"{master_seed}:{record_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big")


def expand_pattern(pattern: str, rng: random.Random) -> str:
    r"""Draw a string matching a REF_GRAMMARS pattern (literals, \d, [X-Y], {n})."""
    out: list[str] = []
    index = 0
    length = len(pattern)
    while index < length:
        char = pattern[index]
        if char == "\\":
            if pattern[index + 1] != "d":
                raise ValueError(f"unsupported escape in pattern {pattern!r}")
            choices, is_class = _DIGITS, True
            index += 2
        elif char == "[":
            end = pattern.index("]", index)
            choices, is_class = _expand_class(pattern[index + 1 : end]), True
            index = end + 1
        else:
            choices, is_class = char, False
            index += 1
        repeat = 1
        if index < length and pattern[index] == "{":
            end = pattern.index("}", index)
            repeat = int(pattern[index + 1 : end])
            index = end + 1
        for _ in range(repeat):
            out.append(rng.choice(choices) if is_class else choices)
    return "".join(out)


def _expand_class(body: str) -> str:
    chars: list[str] = []
    index = 0
    while index < len(body):
        if index + 2 < len(body) and body[index + 1] == "-":
            chars.extend(chr(code) for code in range(ord(body[index]), ord(body[index + 2]) + 1))
            index += 3
        else:
            chars.append(body[index])
            index += 1
    return "".join(chars)


def make_vin(rng: random.Random, oem: Oem) -> str:
    """ISO 3779 VIN: marque WMI prefix, random body, computed check digit."""
    chars = list(_OEM_WMI[oem])
    chars.extend(rng.choice(_VIN_ALPHABET) for _ in range(VIN_LENGTH - len(chars)))
    chars[VIN_CHECK_DIGIT_INDEX] = _vin_check_digit(chars)
    return "".join(chars)


def _vin_check_digit(chars: list[str]) -> str:
    weighted = sum(
        VIN_CHAR_VALUES[char] * weight
        for char, weight in zip(chars, VIN_POSITION_WEIGHTS, strict=True)
    )
    remainder = weighted % 11
    return "X" if remainder == 10 else str(remainder)


def make_references(rng: random.Random, oem: Oem, schedule: ScheduleType) -> ReferenceSet:
    values: dict[FieldName, str] = {}
    for field_name in SCHEDULE_REFERENCE_FIELDS[schedule]:
        if field_name is FieldName.VIN:
            values[field_name] = make_vin(rng, oem)
        else:
            values[field_name] = expand_pattern(REF_GRAMMARS[oem][field_name], rng)
    return ReferenceSet(
        claim_number=values.get(FieldName.CLAIM_NUMBER),
        ro_number=values.get(FieldName.RO_NUMBER),
        vin=values.get(FieldName.VIN),
        invoice_number=values.get(FieldName.INVOICE_NUMBER),
        program_code=values.get(FieldName.PROGRAM_CODE),
    )


def draw_amount(rng: random.Random, schedule: ScheduleType) -> int:
    low, high = SCHEDULE_AMOUNT_RANGES[schedule]
    if schedule is ScheduleType.WARRANTY_RECEIVABLE:
        # Cubing the uniform draw skews claim values toward the low end.
        return low + int((high - low) * rng.random() ** 3)
    return rng.randint(low, high)


def make_description(rng: random.Random, schedule: ScheduleType) -> str:
    return rng.choice(_DESCRIPTIONS[schedule])


def generate_ledger(master_seed: int) -> LedgerBook:
    """Three dealers, four months of entries per schedule, all seeded per record."""
    entries: list[LedgerEntry] = []
    sequence = dict.fromkeys(ScheduleType, 0)
    for dealer in DEALERS:
        for schedule in ScheduleType:
            for year, month in GENERATION_MONTHS:
                count_id = f"count:{dealer.dealer_id}:{schedule}:{year:04d}{month:02d}"
                count_rng = random.Random(record_seed(master_seed, count_id))
                low, high = _ENTRY_COUNT_RANGES[schedule]
                for _ in range(count_rng.randint(low, high)):
                    sequence[schedule] += 1
                    entry_id = f"led-{schedule}-{sequence[schedule]:05d}"
                    rng = random.Random(record_seed(master_seed, entry_id))
                    entries.append(_build_entry(entry_id, dealer, schedule, year, month, rng))
    return LedgerBook(dealers=DEALERS, entries=tuple(entries))


def _build_entry(
    entry_id: str,
    dealer: Dealer,
    schedule: ScheduleType,
    year: int,
    month: int,
    rng: random.Random,
) -> LedgerEntry:
    references = make_references(rng, dealer.oem, schedule)
    post_day = rng.randint(1, calendar.monthrange(year, month)[1])
    counterparty = (
        _OEM_CAPTIVE[dealer.oem]
        if schedule is ScheduleType.FLOORPLAN_LIABILITY
        else _OEM_CORPORATE[dealer.oem]
    )
    return LedgerEntry(
        entry_id=entry_id,
        dealer_id=dealer.dealer_id,
        schedule=schedule,
        gl_account=_GL_ACCOUNTS[schedule],
        claim_number=references.claim_number,
        ro_number=references.ro_number,
        vin=references.vin,
        invoice_number=references.invoice_number,
        program_code=references.program_code,
        post_date=date(year, month, post_day),
        amount_cents=draw_amount(rng, schedule),
        description=make_description(rng, schedule),
        counterparty=counterparty,
    )
