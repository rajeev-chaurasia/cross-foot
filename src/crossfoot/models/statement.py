"""Format-agnostic statement truth. Renderers consume it, evals compare against it."""

from datetime import date

from pydantic import BaseModel, ConfigDict

from crossfoot.constants import DocType, LineType, Oem


class StatementLine(BaseModel):
    model_config = ConfigDict(frozen=True)

    line_no: int
    line_type: LineType
    claim_number: str | None = None
    ro_number: str | None = None
    vin: str | None = None
    invoice_number: str | None = None
    program_code: str | None = None
    line_date: date
    description: str
    amount_cents: int
    source_entry_id: str | None = None  # ledger provenance; None for injected orphans


class StatementDoc(BaseModel):
    """A statement is internally consistent even when it disagrees with the books.

    Composer invariants, asserted by the truth round-trip contract test:
    subtotal equals the sum of line amounts, and crossfoot_delta_cents() is zero.
    Discrepancy injectors must re-crossfoot printed totals after mutating lines.
    """

    model_config = ConfigDict(frozen=True)

    doc_id: str
    dealer_id: str
    doc_type: DocType
    oem: Oem
    statement_number: str
    statement_date: date
    period_start: date
    period_end: date
    previous_balance_cents: int | None = None
    subtotal_cents: int
    adjustments_cents: int = 0
    total_cents: int
    lines: tuple[StatementLine, ...]

    def crossfoot_delta_cents(self) -> int:
        """Zero when the printed totals agree with the line items."""
        carried = self.previous_balance_cents or 0
        lines_sum = sum(line.amount_cents for line in self.lines)
        return self.total_cents - (carried + lines_sum + self.adjustments_cents)
