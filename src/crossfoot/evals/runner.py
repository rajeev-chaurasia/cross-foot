"""Phase 1 baseline eval runner: deterministic CSV extraction scored against truth."""

import logging
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from crossfoot.constants import ExtractionRoute, QualityTier, SplitName
from crossfoot.evals.metrics import score_fields
from crossfoot.evals.paths import UnsafeDatasetPathError, resolve_dataset_path
from crossfoot.extraction.tabular import extract_csv
from crossfoot.models.extraction import ExtractedDocument
from crossfoot.models.ledger import LedgerBook
from crossfoot.models.manifest import DatasetManifest
from crossfoot.models.scorecard import Scorecard

_LOGGER = logging.getLogger(__name__)
_GIT_SHA_FALLBACK = "unknown"
_GIT_TIMEOUT_SECONDS = 10
_BASELINE_NOTES = (
    "Phase 1 baseline: deterministic CSV extraction only, every other quality tier"
    " is left unextracted."
)


def run_eval(dataset_dir: Path, split: SplitName) -> Scorecard:
    """Extract the split's CSV documents, score them, and assemble a scorecard."""
    manifest = DatasetManifest.model_validate_json((dataset_dir / "manifest.json").read_bytes())
    # The ledger is a legitimate pipeline input; validate it is present and well formed.
    LedgerBook.model_validate_json((dataset_dir / "ledger.json").read_bytes())
    split_records = [record for record in manifest.records if record.split is split]
    extracted: list[ExtractedDocument] = []
    unprocessable = 0
    for record in split_records:
        if record.quality_tier is not QualityTier.CSV:
            continue  # non-CSV tiers stay unextracted in this baseline
        try:
            file_path = resolve_dataset_path(dataset_dir, record.file_path)
        except UnsafeDatasetPathError as error:
            _LOGGER.warning("skipping %s: %s", record.doc_id, error)
            unprocessable += 1
            continue
        doc = extract_csv(file_path, record.doc_id)
        if doc.route is ExtractionRoute.UNPROCESSABLE:
            unprocessable += 1
        else:
            extracted.append(doc)
    now = datetime.now(UTC)
    git_sha = _git_short_sha()
    return Scorecard(
        run_id=f"{now:%Y%m%dT%H%M%S}-{git_sha}",
        created_at=now,
        git_sha=git_sha,
        dataset_config_hash=manifest.config_hash,
        master_seed=manifest.master_seed,
        split=split,
        models_used=(),
        documents_total=len(split_records),
        documents_processed=len(extracted),
        documents_unprocessable=unprocessable,
        field_accuracy=score_fields(extracted, manifest, split),
        notes=_BASELINE_NOTES,
    )


def _git_short_sha() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return _GIT_SHA_FALLBACK
    sha = result.stdout.strip()
    return sha if result.returncode == 0 and sha else _GIT_SHA_FALLBACK
