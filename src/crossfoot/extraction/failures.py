"""Whether a failed document is finished, or merely unfinished.

Infrastructure failing is a property of the run: every provider refused, an
allowance is spent, a socket died. The document never got a fair attempt, so the
checkpoint keeps it pending and `--resume` owes it another one. A document that
failed on its own bytes, or whose route no extractor here serves, is finished: a
rerun burns quota to reach the same answer, so the checkpoint records it done and
keeps the result.

A live 105 document run is why this exists. Thirty three scans spilled over to a
text only model that answered 400 for every one of them, each became an
UNPROCESSABLE result, and each was checkpointed DONE. `--resume` then skipped
them forever, and the only cure was deleting the run state, which would also have
discarded the 64 documents that had succeeded.
"""

from __future__ import annotations

from enum import StrEnum

from crossfoot.constants import ExtractionRoute, IngestErrorKind
from crossfoot.models.extraction import ExtractedDocument


class FailureClass(StrEnum):
    """Who owns a failure, and therefore what the checkpoint must record."""

    TRANSIENT = "transient"  # the run's fault: stays pending, retried on resume
    PERMANENT = "permanent"  # the document's fault: recorded done, never retried


# The classification, as data. Call sites read this table instead of an error
# message, so one kind means one thing in every layer.
FAILURE_CLASSES: dict[IngestErrorKind, FailureClass] = {
    IngestErrorKind.TRUNCATED: FailureClass.PERMANENT,
    IngestErrorKind.ENCRYPTED: FailureClass.PERMANENT,
    IngestErrorKind.EMPTY: FailureClass.PERMANENT,
    IngestErrorKind.UNRECOGNIZED: FailureClass.PERMANENT,
    IngestErrorKind.TOO_LARGE: FailureClass.PERMANENT,
    IngestErrorKind.PROVIDER_UNAVAILABLE: FailureClass.TRANSIENT,
}

# A failure nobody classified is unfinished work rather than a verdict: one
# wasted retry is cheap, while a wrong DONE loses the document until the run
# state database is deleted.
UNCLASSIFIED_FAILURE = FailureClass.TRANSIENT

# The detail the vision extractor writes when the provider chain is spent. It is
# also the only mark on the rows the live run checkpointed DONE before
# PROVIDER_UNAVAILABLE existed, which is what makes those rows recoverable.
PROVIDER_FAILURE_DETAIL = "every provider failed the extraction call"


def failure_class_of(document: ExtractedDocument | None) -> FailureClass | None:
    """How a checkpoint must treat one outcome; None when it really extracted."""
    if document is None:
        return UNCLASSIFIED_FAILURE  # an exception that reached no classifier
    if document.route is not ExtractionRoute.UNPROCESSABLE:
        return None
    if document.error is None:
        return UNCLASSIFIED_FAILURE
    return FAILURE_CLASSES.get(document.error.kind, UNCLASSIFIED_FAILURE)


def is_provider_failure(document: ExtractedDocument) -> bool:
    """True for a provider failure, including one stored before it had a kind."""
    error = document.error
    if error is None:
        return False
    if error.kind is IngestErrorKind.PROVIDER_UNAVAILABLE:
        return True
    # Legacy shape: UNRECOGNIZED, with the cause named only in the detail.
    return error.kind is IngestErrorKind.UNRECOGNIZED and error.detail.startswith(
        PROVIDER_FAILURE_DETAIL
    )
