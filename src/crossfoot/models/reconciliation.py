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
    plus a memo amount so the dashboard can still show the money involved."""

    model_config = ConfigDict(frozen=True)

    exception_id: str
    run_id: str
    exception_type: ExceptionType
    doc_id: str | None = None
    statement_line_no: int | None = None
    ledger_entry_id: str | None = None
    match_key: str | None = None
    statement_amount_cents: int | None = None
    ledger_amount_cents: int | None = None
    dollar_impact_cents: int
    memo_amount_cents: int = 0
    explanation: str
    status: ExceptionStatus = ExceptionStatus.OPEN
    detected_at: datetime
