"""A correction is validated by the family it belongs to, and a crop link cannot climb out."""

import pytest

from crossfoot.api.dto import CorrectionRequest, crop_url
from crossfoot.constants import FieldFamily

REVIEWER = "rc"


def correction(value: str) -> CorrectionRequest:
    return CorrectionRequest(value=value, reviewer=REVIEWER)


@pytest.mark.parametrize(
    ("typed", "canonical"),
    [
        ("1999.99", "1999.99"),
        ("$1,234.56", "1234.56"),
        ("(123.45)", "-123.45"),
        ("123.45-", "-123.45"),
        ("  42  ", "42.00"),
    ],
)
def test_an_amount_is_stored_the_way_the_pipeline_stores_one(typed: str, canonical: str) -> None:
    # The reviewer types what is printed; the row keeps what the extractor would have.
    assert correction(typed).canonical_for(FieldFamily.AMOUNT) == canonical


@pytest.mark.parametrize("typed", ["not a number", "", "   ", "1.2.3", "12e5"])
def test_an_amount_the_family_cannot_parse_is_rejected(typed: str) -> None:
    assert correction(typed).canonical_for(FieldFamily.AMOUNT) is None


@pytest.mark.parametrize(
    ("typed", "canonical"),
    [("07/15/2026", "2026-07-15"), ("2026-07-15", "2026-07-15"), ("15-JUL-2026", "2026-07-15")],
)
def test_every_date_form_the_extractor_reads_canonicalizes_the_same_way(
    typed: str, canonical: str
) -> None:
    assert correction(typed).canonical_for(FieldFamily.DATE) == canonical


@pytest.mark.parametrize("typed", ["13/45/2026", "2026-02-30", "tomorrow", ""])
def test_a_date_the_family_cannot_parse_is_rejected(typed: str) -> None:
    assert correction(typed).canonical_for(FieldFamily.DATE) is None


@pytest.mark.parametrize("family", [FieldFamily.REFERENCE, FieldFamily.TEXT])
def test_a_reference_or_text_value_is_kept_verbatim_apart_from_padding(
    family: FieldFamily,
) -> None:
    # Normalizing a reference is a matching rule, not a storage rule: the row
    # holds what is printed on the statement.
    assert correction("  NS12345678 ").canonical_for(family) == "NS12345678"
    assert correction("   ").canonical_for(family) is None


def test_a_crop_link_keeps_each_identifier_in_one_path_segment() -> None:
    # An ordinary id is untouched; a separator inside one is encoded, so the
    # crop route still sees exactly two segments and can validate both.
    assert crop_url("doc-a", "fld-0001") == "/api/crops/doc-a/fld-0001.png"
    assert crop_url("doc/a", "fld-0001") == "/api/crops/doc%2Fa/fld-0001.png"
