"""A statement the engine can match, assembled from stored field values.

The build and the review API hand over the same two things: the values read out
of the document, and the identity no extractor can read off a page. Both go
through here, so the exceptions a correction produces cannot drift from the
exceptions the build produced.

Identity is operational context rather than truth. In production a dealer, a
marque and a period are known at ingest because you know whose statement you are
processing; the engine blocks on all four, so they travel beside the extraction
instead of being guessed from it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date

from crossfoot.constants import DocType, FieldName, LineType, Oem
from crossfoot.models.statement import StatementDoc, StatementLine


@dataclass(frozen=True, slots=True)
class StatementIdentity:
    """Whose statement this is and for when: everything the blocking key needs."""

    dealer_id: str
    doc_type: DocType
    oem: Oem
    period_start: date
    period_end: date


@dataclass(frozen=True, slots=True)
class FieldValue:
    """One reading, stripped to what a statement line needs from it.

    `line_no` is None for a header field, matching the fields table and
    `ExtractedField`.
    """

    name: FieldName
    line_no: int | None
    value: str | None
    value_cents: int | None
    value_date: date | None


def statement_from_fields(
    doc_id: str, fields: Sequence[FieldValue], identity: StatementIdentity
) -> StatementDoc:
    """Values from the readings, dealer, marque and period from the identity."""
    header = {field.name: field for field in fields if field.line_no is None}
    by_line: dict[int, dict[FieldName, FieldValue]] = {}
    for field in fields:
        if field.line_no is not None:
            by_line.setdefault(field.line_no, {})[field.name] = field
    lines = tuple(
        line
        for line in (_line(line_no, by_line[line_no]) for line_no in sorted(by_line))
        if line is not None
    )
    subtotal = sum(line.amount_cents for line in lines)
    # A printed total of zero is a reading, not a missing reading. Falling back to
    # the sum of the lines here would repair the one case the crossfoot check
    # exists to catch, a total that contradicts the lines it sits under.
    total = _cents(header.get(FieldName.TOTAL))
    return StatementDoc(
        doc_id=doc_id,
        dealer_id=identity.dealer_id,
        doc_type=identity.doc_type,
        oem=identity.oem,
        # Neither of the next two reaches the matcher: the engine blocks on
        # dealer, doc type and period alone. A statement whose number or date went
        # unread is still reconcilable, so both fall back rather than refuse.
        statement_number=_text(header.get(FieldName.STATEMENT_NUMBER)) or doc_id,
        statement_date=_date(header.get(FieldName.STATEMENT_DATE)) or identity.period_end,
        period_start=identity.period_start,
        period_end=identity.period_end,
        previous_balance_cents=_cents(header.get(FieldName.PREVIOUS_BALANCE)),
        subtotal_cents=subtotal,
        total_cents=subtotal if total is None else total,
        lines=lines,
    )


def _line(line_no: int, fields: Mapping[FieldName, FieldValue]) -> StatementLine | None:
    """A line the engine can match, or None when the reading lacks its bones."""
    amount_cents = _cents(fields.get(FieldName.LINE_AMOUNT))
    line_date = _date(fields.get(FieldName.LINE_DATE))
    if amount_cents is None or line_date is None:
        return None
    return StatementLine(
        line_no=line_no,
        # Line type is never extracted; the sign carries the same information for
        # every rule the engine applies.
        line_type=LineType.CHARGE if amount_cents >= 0 else LineType.CREDIT,
        claim_number=_text(fields.get(FieldName.CLAIM_NUMBER)),
        ro_number=_text(fields.get(FieldName.RO_NUMBER)),
        vin=_text(fields.get(FieldName.VIN)),
        invoice_number=_text(fields.get(FieldName.INVOICE_NUMBER)),
        program_code=_text(fields.get(FieldName.PROGRAM_CODE)),
        line_date=line_date,
        description=_text(fields.get(FieldName.DESCRIPTION)) or "",
        amount_cents=amount_cents,
    )


def _text(field: FieldValue | None) -> str | None:
    return None if field is None else field.value


def _cents(field: FieldValue | None) -> int | None:
    return None if field is None else field.value_cents


def _date(field: FieldValue | None) -> date | None:
    return None if field is None else field.value_date
