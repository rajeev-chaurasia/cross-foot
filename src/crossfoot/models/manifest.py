"""Eval-only dataset manifest.

The manifest is ground truth for scoring. Pipeline packages (extraction,
confidence, reconcile) must never import this module or read manifest.json;
an import-boundary contract test enforces it.
"""

from pydantic import BaseModel, ConfigDict, Field

from crossfoot.constants import CorruptionKind, ExceptionType, QualityTier, SplitName
from crossfoot.models.statement import StatementDoc


class InjectedDiscrepancy(BaseModel):
    model_config = ConfigDict(frozen=True)

    discrepancy_id: str
    expected_exception: ExceptionType
    doc_id: str
    statement_line_no: int | None = None
    ledger_entry_id: str | None = None
    dollar_impact_cents: int
    memo_amount_cents: int = 0
    description: str


class ManifestRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    doc_id: str
    file_path: str  # relative to the dataset root
    quality_tier: QualityTier
    template_id: str
    render_seed: int
    augraphy_profile: str | None = None
    corruption: CorruptionKind | None = None
    truth: StatementDoc | None = None  # None only for corrupted files
    # Text exactly as printed, keyed "header:{field_name}" or "{line_no}:{field_name}".
    rendered_values: dict[str, str] = Field(default_factory=dict)
    injected: tuple[InjectedDiscrepancy, ...] = ()
    split: SplitName | None = None  # None for corrupted files


class DatasetManifest(BaseModel):
    model_config = ConfigDict(frozen=True)

    master_seed: int
    generator_version: str
    config_hash: str
    records: tuple[ManifestRecord, ...]
