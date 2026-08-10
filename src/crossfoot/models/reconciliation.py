"""Reconciliation outcomes: matches and the six-type exception taxonomy."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from crossfoot.constants import ExceptionStatus, ExceptionType


class MatchedLine(BaseModel):
    model_config = ConfigDict(frozen=True)

    doc_id: str
    statement_line_no: int
    ledger_entry_id: str
    match_key: str
    score: float  # 1.0 for exact-pass matches


class ExceptionRecord(BaseModel):
    """Every exception carries signed dollar impact; timing differences carry 0
    plus a memo amount so the dashboard can still show the money involved.

    `exception_id` is derived from what the finding is about rather than from the
    order it was emitted in, so re-reconciling a document names the same finding
    the same thing and a reviewer resolving by id closes what they were reading.
    """

    model_config = ConfigDict(frozen=True)

    exception_id: str
    run_id: str
    exception_type: ExceptionType
    doc_id: str | None = None
    statement_line_no: int | None = None
    ledger_entry_id: str | None = None
    statement_amount_cents: int | None = None
    ledger_amount_cents: int | None = None
    dollar_impact_cents: int
    memo_amount_cents: int = 0
    explanation: str
    status: ExceptionStatus = ExceptionStatus.OPEN
    detected_at: datetime


class ReconciliationDelta(BaseModel):
    """What re-reconciling one document did to the exceptions it owns.

    `dollars_at_risk_change_cents` is the change in the sum of absolute impact of
    the document's open exceptions, so a correction that clears risk reports a
    negative number.
    """

    model_config = ConfigDict(frozen=True)

    exceptions_removed: int
    exceptions_added: int
    dollars_at_risk_change_cents: int
