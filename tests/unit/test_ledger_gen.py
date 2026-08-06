"""Unit tests for the deterministic ledger generator."""

import hashlib
import random
import re
from collections import defaultdict

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
from crossfoot.generator.ledger_gen import (
    DEALERS,
    GENERATION_MONTHS,
    SCHEDULE_AMOUNT_RANGES,
    SCHEDULE_REFERENCE_FIELDS,
    expand_pattern,
    generate_ledger,
    make_vin,
    record_seed,
)
from crossfoot.models.ledger import LedgerEntry

MASTER_SEED = 42

_REFERENCE_FIELDS = (
    FieldName.CLAIM_NUMBER,
    FieldName.RO_NUMBER,
    FieldName.VIN,
    FieldName.INVOICE_NUMBER,
    FieldName.PROGRAM_CODE,
)


def _reference_value(entry: LedgerEntry, field_name: FieldName) -> str | None:
    values: dict[FieldName, str | None] = {
        FieldName.CLAIM_NUMBER: entry.claim_number,
        FieldName.RO_NUMBER: entry.ro_number,
        FieldName.VIN: entry.vin,
        FieldName.INVOICE_NUMBER: entry.invoice_number,
        FieldName.PROGRAM_CODE: entry.program_code,
    }
    return values[field_name]


def _expected_check_digit(vin: str) -> str:
    weighted = sum(
        VIN_CHAR_VALUES[char] * weight
        for char, weight in zip(vin, VIN_POSITION_WEIGHTS, strict=True)
    )
    remainder = weighted % 11
    return "X" if remainder == 10 else str(remainder)


def test_record_seed_matches_contract_formula() -> None:
    digest = hashlib.sha256(b"42:led-parts_payable-00001").digest()
    expected = int.from_bytes(digest[:8], "big")
    assert record_seed(42, "led-parts_payable-00001") == expected


def test_generate_ledger_is_deterministic() -> None:
    assert generate_ledger(MASTER_SEED) == generate_ledger(MASTER_SEED)


def test_different_seeds_produce_different_entries() -> None:
    assert generate_ledger(MASTER_SEED).entries != generate_ledger(MASTER_SEED + 1).entries


def test_three_dealers_with_slug_ids() -> None:
    book = generate_ledger(MASTER_SEED)
    assert book.dealers == DEALERS
    assert [dealer.dealer_id for dealer in book.dealers] == [
        "dlr-meridian",
        "dlr-northstar",
        "dlr-kaizen",
    ]
    assert {dealer.oem for dealer in book.dealers} == {Oem.MERIDIAN, Oem.NORTHSTAR, Oem.KAIZEN}


def test_entries_cover_every_dealer_schedule_month() -> None:
    book = generate_ledger(MASTER_SEED)
    covered = {
        (entry.dealer_id, entry.schedule, entry.post_date.year, entry.post_date.month)
        for entry in book.entries
    }
    for dealer in book.dealers:
        for schedule in ScheduleType:
            for year, month in GENERATION_MONTHS:
                assert (dealer.dealer_id, schedule, year, month) in covered


def test_entry_ids_are_sequential_slugs_per_schedule() -> None:
    book = generate_ledger(MASTER_SEED)
    by_schedule: dict[ScheduleType, list[str]] = defaultdict(list)
    for entry in book.entries:
        by_schedule[entry.schedule].append(entry.entry_id)
    for schedule, entry_ids in by_schedule.items():
        expected = [f"led-{schedule}-{seq:05d}" for seq in range(1, len(entry_ids) + 1)]
        assert sorted(entry_ids) == expected


def test_amounts_positive_and_within_schedule_ranges() -> None:
    book = generate_ledger(MASTER_SEED)
    for entry in book.entries:
        low, high = SCHEDULE_AMOUNT_RANGES[entry.schedule]
        assert low <= entry.amount_cents <= high, entry.entry_id
        assert entry.amount_cents > 0


def test_warranty_amounts_skew_low() -> None:
    book = generate_ledger(MASTER_SEED)
    amounts = sorted(
        entry.amount_cents
        for entry in book.entries
        if entry.schedule is ScheduleType.WARRANTY_RECEIVABLE
    )
    low, high = SCHEDULE_AMOUNT_RANGES[ScheduleType.WARRANTY_RECEIVABLE]
    median = amounts[len(amounts) // 2]
    assert median < (low + high) / 2


def test_reference_population_matches_schedule() -> None:
    book = generate_ledger(MASTER_SEED)
    for entry in book.entries:
        expected = set(SCHEDULE_REFERENCE_FIELDS[entry.schedule])
        for field_name in _REFERENCE_FIELDS:
            value = _reference_value(entry, field_name)
            if field_name in expected:
                assert value, f"{entry.entry_id}: {field_name} missing"
            else:
                assert value is None, f"{entry.entry_id}: unexpected {field_name}"


def test_references_match_oem_grammar() -> None:
    book = generate_ledger(MASTER_SEED)
    oem_by_dealer = {dealer.dealer_id: dealer.oem for dealer in book.dealers}
    for entry in book.entries:
        grammar = REF_GRAMMARS[oem_by_dealer[entry.dealer_id]]
        for field_name in (FieldName.CLAIM_NUMBER, FieldName.RO_NUMBER, FieldName.INVOICE_NUMBER):
            value = _reference_value(entry, field_name)
            if value is not None:
                assert re.fullmatch(grammar[field_name], value), f"{entry.entry_id}: {value!r}"
        if entry.program_code is not None:
            assert re.fullmatch(grammar[FieldName.PROGRAM_CODE], entry.program_code)


def test_vins_pass_iso3779() -> None:
    book = generate_ledger(MASTER_SEED)
    vins = [entry.vin for entry in book.entries if entry.vin]
    assert vins
    for vin in vins:
        assert len(vin) == VIN_LENGTH
        assert all(char in VIN_CHAR_VALUES for char in vin)
        assert vin[VIN_CHECK_DIGIT_INDEX] == _expected_check_digit(vin)


def test_make_vin_check_digit_over_many_draws() -> None:
    rng = random.Random(7)
    for oem in Oem:
        for _ in range(50):
            vin = make_vin(rng, oem)
            assert vin[VIN_CHECK_DIGIT_INDEX] == _expected_check_digit(vin)


def test_expand_pattern_matches_every_grammar() -> None:
    rng = random.Random(11)
    for oem in Oem:
        for field_name, pattern in REF_GRAMMARS[oem].items():
            for _ in range(25):
                value = expand_pattern(pattern, rng)
                assert re.fullmatch(pattern, value), f"{oem} {field_name}: {value!r}"


def test_post_dates_stay_inside_their_month() -> None:
    book = generate_ledger(MASTER_SEED)
    valid_months = set(GENERATION_MONTHS)
    for entry in book.entries:
        assert (entry.post_date.year, entry.post_date.month) in valid_months
