"""The dealer's own books: the honest side of every reconciliation."""

from datetime import date

from pydantic import BaseModel, ConfigDict

from crossfoot.constants import Oem, ScheduleType


class Dealer(BaseModel):
    model_config = ConfigDict(frozen=True)

    dealer_id: str
    name: str
    oem: Oem


class LedgerEntry(BaseModel):
    """One schedule row. Amounts are signed cents: receivables positive, credits negative."""

    model_config = ConfigDict(frozen=True)

    entry_id: str
    dealer_id: str
    schedule: ScheduleType
    gl_account: str
    claim_number: str | None = None
    ro_number: str | None = None
    vin: str | None = None
    invoice_number: str | None = None
    program_code: str | None = None
    post_date: date
    amount_cents: int
    description: str
    counterparty: str


class LedgerBook(BaseModel):
    model_config = ConfigDict(frozen=True)

    dealers: tuple[Dealer, ...]
    entries: tuple[LedgerEntry, ...]
