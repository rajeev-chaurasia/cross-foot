"""The counts behind the summary tile, in one round trip.

Three of them are opinions the contract fixes rather than obvious sums. Dollars at
risk covers OPEN exceptions only, since a resolved one is no longer at risk. Cost
per document divides the ledger's list price by the documents that were actually
processed, so a free tier run still reports what the work would have cost and an
unreadable file, which cost nothing to extract, is not in the denominator. That
list price is repriced row by row through `crossfoot.costs.ledger`: a row stored
before its model had a price table entry holds a zero that was never true, and a
stale zero must not become the published number. A stored price the table did
know stands, because it is the record of what the run believed it was spending.
"""

from __future__ import annotations

import sqlite3

from crossfoot.constants import ExceptionStatus, ExtractionRoute, ReviewStatus
from crossfoot.costs.ledger import effective_list_price_microusd

# Repricing needs the price table, which lives in Python, so the sum goes through
# a scalar function registered on the connection rather than a CASE over patterns.
_LIST_PRICE_FUNCTION = "effective_list_price_microusd"
_LIST_PRICE_ARITY = 4

_SUMMARY = f"""
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
    (
        SELECT COALESCE(
            SUM({_LIST_PRICE_FUNCTION}(
                model, prompt_tokens, completion_tokens, list_price_microusd
            )),
            0
        )
        FROM llm_calls
    ) AS list_price_microusd
"""


def summary(connection: sqlite3.Connection) -> sqlite3.Row:
    connection.create_function(
        _LIST_PRICE_FUNCTION, _LIST_PRICE_ARITY, _repriced, deterministic=True
    )
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


def _repriced(model: str, prompt_tokens: int, completion_tokens: int, stored: int) -> int:
    """One llm_calls row, priced at today's table when its stored price is a stale zero."""
    return effective_list_price_microusd(
        model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        stored_microusd=stored,
    )
