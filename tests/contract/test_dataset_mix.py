"""DATASET_MIX is the plan as data; these tests never render anything themselves."""

from collections import Counter
from pathlib import Path

import pytest

from crossfoot.constants import DocType, QualityTier
from crossfoot.models.manifest import DatasetManifest

MixKey = tuple[DocType, QualityTier]

FULL_MIX: dict[MixKey, int] = {
    (DocType.PARTS_STATEMENT, QualityTier.CLEAN_DIGITAL): 24,
    (DocType.PARTS_STATEMENT, QualityTier.SCAN_LIGHT): 14,
    (DocType.PARTS_STATEMENT, QualityTier.SCAN_HEAVY): 8,
    (DocType.PARTS_STATEMENT, QualityTier.CSV): 8,
    (DocType.PARTS_STATEMENT, QualityTier.XLSX): 6,
    (DocType.WARRANTY_CREDIT_MEMO, QualityTier.CLEAN_DIGITAL): 24,
    (DocType.WARRANTY_CREDIT_MEMO, QualityTier.SCAN_LIGHT): 16,
    (DocType.WARRANTY_CREDIT_MEMO, QualityTier.SCAN_HEAVY): 10,
    (DocType.WARRANTY_CREDIT_MEMO, QualityTier.CSV): 6,
    (DocType.WARRANTY_CREDIT_MEMO, QualityTier.XLSX): 4,
    (DocType.INCENTIVE_STATEMENT, QualityTier.CLEAN_DIGITAL): 20,
    (DocType.INCENTIVE_STATEMENT, QualityTier.SCAN_LIGHT): 12,
    (DocType.INCENTIVE_STATEMENT, QualityTier.SCAN_HEAVY): 8,
    (DocType.INCENTIVE_STATEMENT, QualityTier.CSV): 6,
    (DocType.INCENTIVE_STATEMENT, QualityTier.XLSX): 4,
    (DocType.FLOORPLAN_STATEMENT, QualityTier.CLEAN_DIGITAL): 16,
    (DocType.FLOORPLAN_STATEMENT, QualityTier.SCAN_LIGHT): 10,
    (DocType.FLOORPLAN_STATEMENT, QualityTier.SCAN_HEAVY): 6,
    (DocType.FLOORPLAN_STATEMENT, QualityTier.CSV): 5,
    (DocType.FLOORPLAN_STATEMENT, QualityTier.XLSX): 3,
}

# The (doc_type, tier) combos the contract doc enumerates for SMALL. It fixes
# 12 files total (2 corrupted, so 10 non-corrupted) over these 9 combos, which
# forces exactly one combo to carry 2 documents without saying which.
SMALL_COMBOS: frozenset[MixKey] = frozenset(
    {
        (DocType.PARTS_STATEMENT, QualityTier.CLEAN_DIGITAL),
        (DocType.PARTS_STATEMENT, QualityTier.SCAN_LIGHT),
        (DocType.PARTS_STATEMENT, QualityTier.CSV),
        (DocType.PARTS_STATEMENT, QualityTier.XLSX),
        (DocType.WARRANTY_CREDIT_MEMO, QualityTier.CLEAN_DIGITAL),
        (DocType.WARRANTY_CREDIT_MEMO, QualityTier.CSV),
        (DocType.INCENTIVE_STATEMENT, QualityTier.CLEAN_DIGITAL),
        (DocType.INCENTIVE_STATEMENT, QualityTier.XLSX),
        (DocType.FLOORPLAN_STATEMENT, QualityTier.CLEAN_DIGITAL),
    }
)


def test_full_mix_matches_plan_exactly() -> None:
    dataset = pytest.importorskip("crossfoot.generator.dataset")
    full_mix: dict[MixKey, int] = dataset.DATASET_MIX[dataset.DatasetProfile.FULL]
    assert full_mix == FULL_MIX
    assert sum(full_mix.values()) == 210
    per_doc_type: Counter[DocType] = Counter()
    for (doc_type, _tier), count in full_mix.items():
        per_doc_type[doc_type] += count
    assert per_doc_type == {
        DocType.PARTS_STATEMENT: 60,
        DocType.WARRANTY_CREDIT_MEMO: 60,
        DocType.INCENTIVE_STATEMENT: 50,
        DocType.FLOORPLAN_STATEMENT: 40,
    }


def test_small_mix_matches_contract_doc() -> None:
    dataset = pytest.importorskip("crossfoot.generator.dataset")
    small_mix: dict[MixKey, int] = dataset.DATASET_MIX[dataset.DatasetProfile.SMALL]
    assert set(small_mix) == set(SMALL_COMBOS)
    assert all(count >= 1 for count in small_mix.values())
    assert sum(small_mix.values()) == 10


def test_small_dataset_counts_match_mix(small_dataset: tuple[Path, DatasetManifest]) -> None:
    dataset = pytest.importorskip("crossfoot.generator.dataset")
    _, manifest = small_dataset
    small_mix: dict[MixKey, int] = dataset.DATASET_MIX[dataset.DatasetProfile.SMALL]
    observed: Counter[MixKey] = Counter()
    corrupted = 0
    for record in manifest.records:
        if record.truth is None:
            corrupted += 1
            continue
        observed[record.truth.doc_type, record.quality_tier] += 1
    assert corrupted == 2
    assert observed == small_mix
