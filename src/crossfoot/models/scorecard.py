"""Eval scorecard: every published number traces to one of these JSON files."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from crossfoot.constants import (
    ExceptionType,
    FieldFamily,
    Provider,
    QualityTier,
    ReconMode,
    SplitName,
)


class FieldAccuracyCell(BaseModel):
    model_config = ConfigDict(frozen=True)

    field_family: FieldFamily
    quality_tier: QualityTier
    fields_expected: int
    fields_extracted: int
    correct_canonical: int  # canonical-value match (cents, ISO date, normalized ref)
    correct_raw: int  # verbatim string match, reported for transparency


class CalibrationBin(BaseModel):
    model_config = ConfigDict(frozen=True)

    field_family: FieldFamily
    mean_confidence: float
    empirical_accuracy: float
    count: int


class ThresholdPoint(BaseModel):
    model_config = ConfigDict(frozen=True)

    field_family: FieldFamily
    threshold: float
    auto_accept_precision: float
    review_rate: float


class ReconCell(BaseModel):
    model_config = ConfigDict(frozen=True)

    mode: ReconMode
    exception_type: ExceptionType
    injected: int
    detected_true: int  # detections matching an injected discrepancy
    detected_false: int  # detections matching nothing injected
    injected_dollar_cents: int
    caught_dollar_cents: int


class CostCell(BaseModel):
    model_config = ConfigDict(frozen=True)

    provider: Provider
    quality_tier: QualityTier | None = None
    calls: int
    prompt_tokens: int
    completion_tokens: int
    list_price_microusd: int  # cost at provider list price even when the tier is free


class Scorecard(BaseModel):
    model_config = ConfigDict(frozen=True)

    run_id: str
    created_at: datetime
    git_sha: str
    dataset_config_hash: str
    master_seed: int
    split: SplitName
    models_used: tuple[str, ...]
    documents_total: int
    documents_processed: int
    documents_unprocessable: int
    field_accuracy: tuple[FieldAccuracyCell, ...]
    calibration: tuple[CalibrationBin, ...] = ()
    threshold_sweep: tuple[ThresholdPoint, ...] = ()
    reconciliation: tuple[ReconCell, ...] = ()
    costs: tuple[CostCell, ...] = ()
    notes: str = ""
