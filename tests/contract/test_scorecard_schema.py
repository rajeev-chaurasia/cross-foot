"""Contract tests pinning the Scorecard JSON shape.

The syrupy snapshot freezes the pydantic JSON schema: any change to the
scorecard models shows up as a snapshot diff and routes through the
maintainer per docs/contracts-phase1.md.
"""

import json
from datetime import UTC, datetime

from syrupy.assertion import SnapshotAssertion

from crossfoot.constants import (
    ExceptionType,
    FieldFamily,
    Provider,
    QualityTier,
    ReconMode,
    SplitName,
)
from crossfoot.models.scorecard import (
    CalibrationBin,
    CostCell,
    FieldAccuracyCell,
    ReconCell,
    Scorecard,
    ThresholdPoint,
)


def build_scorecard() -> Scorecard:
    """Fully populated scorecard: every optional collection carries data."""
    return Scorecard(
        run_id="run-01J00000000000000000000000",
        created_at=datetime(2026, 8, 1, 12, 30, 0, tzinfo=UTC),
        git_sha="a" * 40,
        dataset_config_hash="b" * 64,
        master_seed=42,
        split=SplitName.CALIBRATION,
        models_used=("gemini-2.5-flash", "llama-3.3-70b-versatile"),
        documents_total=12,
        documents_processed=10,
        documents_unprocessable=2,
        field_accuracy=(
            FieldAccuracyCell(
                field_family=FieldFamily.AMOUNT,
                quality_tier=QualityTier.CSV,
                fields_expected=40,
                fields_extracted=38,
                correct_canonical=35,
                correct_raw=30,
            ),
            FieldAccuracyCell(
                field_family=FieldFamily.REFERENCE,
                quality_tier=QualityTier.SCAN_LIGHT,
                fields_expected=25,
                fields_extracted=20,
                correct_canonical=18,
                correct_raw=17,
            ),
        ),
        calibration=(
            CalibrationBin(
                field_family=FieldFamily.DATE,
                mean_confidence=0.91,
                empirical_accuracy=0.88,
                count=120,
            ),
        ),
        threshold_sweep=(
            ThresholdPoint(
                field_family=FieldFamily.TEXT,
                threshold=0.9,
                auto_accept_precision=0.97,
                review_rate=0.22,
            ),
        ),
        reconciliation=(
            ReconCell(
                mode=ReconMode.ORACLE,
                exception_type=ExceptionType.SHORT_PAY,
                injected=3,
                detected_true=2,
                detected_false=1,
                injected_dollar_cents=91_500,
                caught_dollar_cents=60_000,
            ),
        ),
        costs=(
            CostCell(
                provider=Provider.GEMINI,
                quality_tier=QualityTier.SCAN_HEAVY,
                calls=44,
                prompt_tokens=180_000,
                completion_tokens=9_500,
                list_price_microusd=13_500,
            ),
        ),
        notes="hand-built contract fixture",
    )


def test_scorecard_json_round_trip() -> None:
    card = build_scorecard()
    restored = Scorecard.model_validate_json(card.model_dump_json())
    assert restored == card


def test_scorecard_json_schema_is_pinned(snapshot: SnapshotAssertion) -> None:
    schema = json.dumps(Scorecard.model_json_schema(), sort_keys=True, indent=2)
    assert schema == snapshot
