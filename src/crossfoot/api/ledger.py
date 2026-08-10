"""The dealer's books, read once per dataset and kept.

Every correction re-reconciles a document against the whole ledger, and the file
is megabytes, so it is parsed on the first correction and held. The cache key
carries the file's modification time, so a rebuilt dataset is never served from a
book that predates it.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import ValidationError

from crossfoot.models.ledger import LedgerBook

LEDGER_FILENAME = "ledger.json"

# One book per dataset directory in the process. The contract suite runs several
# apps at once, so this is a few entries rather than one.
CACHE_SIZE = 8


def ledger_book(dataset_dir: Path) -> LedgerBook | None:
    """The book under dataset_dir, or None when there is none to reconcile against."""
    path = dataset_dir / LEDGER_FILENAME
    try:
        modified_ns = path.stat().st_mtime_ns
    except OSError:
        return None
    return _load(path, modified_ns)


@lru_cache(maxsize=CACHE_SIZE)
def _load(path: Path, modified_ns: int) -> LedgerBook | None:
    """`modified_ns` is a cache key and nothing else; the parse ignores it."""
    del modified_ns
    try:
        return LedgerBook.model_validate_json(path.read_bytes())
    except (OSError, ValidationError):
        # A dataset directory with no usable ledger is a deployment fact, not a
        # bad request: the caller answers null rather than failing the correction.
        return None
