"""The counts behind the summary tile, in one round trip.

Two of them are opinions the contract fixes rather than obvious sums. Dollars at
risk covers OPEN exceptions only, since a resolved one is no longer at risk. Cost
per document divides the ledger's list price by the documents that were actually
processed, so a free tier run still reports what the work would have cost and an
unreadable file, which cost nothing to extract, is not in the denominator.
"""

from __future__ import annotations

import sqlite3

from crossfoot.constants import ExceptionStatus, ExtractionRoute, ReviewStatus

_SUMMARY = """
SELECT
    (SELECT COUNT(*) FROM documents WHERE route <> :unprocessable) AS documents_processed,
    (SELECT COUNT(*) FROM fields) AS fields_extracted,
    (SELECT COUNT(*) FROM fields WHERE status = :auto_accepted) AS auto_accepted,
    (SELECT COUNT(*) FROM fields WHERE status = :needs_review) AS review_queue_depth,
    (SELECT COUNT(*) FROM exceptions WHERE status = :open) AS open_exception_count,
    (
        SELECT COALESCE(SUM(ABS(dollar_impact_cents)), 0)
        FROM exceptions WHERE status = :open
    ) AS gross_dollars_at_risk_cents,
    (SELECT COALESCE(SUM(list_price_microusd), 0) FROM llm_calls) AS list_price_microusd
"""


def summary(connection: sqlite3.Connection) -> sqlite3.Row:
    row: sqlite3.Row = connection.execute(
        _SUMMARY,
        {
            "unprocessable": ExtractionRoute.UNPROCESSABLE.value,
            "auto_accepted": ReviewStatus.AUTO_ACCEPTED.value,
            "needs_review": ReviewStatus.NEEDS_REVIEW.value,
            "open": ExceptionStatus.OPEN.value,
        },
    ).fetchone()
    return row
