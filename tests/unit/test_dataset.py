"""Unit tests for dataset planning, splits, and manifest assembly with stub renderers.

The real renderers are exercised by the contract suite; these tests inject
stub hooks so planning logic stays verifiable without Chromium or Augraphy.
"""

from collections import Counter
from pathlib import Path

import pytest

from crossfoot.constants import CorruptionKind, DocType, QualityTier, SplitName
from crossfoot.generator.dataset import (
    CORRUPTED_MIX,
    DATASET_MIX,
    SPLIT_FRACTIONS,
    DatasetProfile,
    RenderHooks,
    _config_hash,
    _generate_dataset,
    split_quotas,
)
from crossfoot.generator.ledger_gen import record_seed
from crossfoot.models.manifest import DatasetManifest
from crossfoot.models.statement import StatementDoc

MASTER_SEED = 42
# The FULL plan is 210 documents, so a 50/25/25 split lands on 105 training docs.
EXPECTED_FULL_TRAIN = 105


CellQuotas = dict[tuple[DocType, QualityTier], dict[SplitName, int]]


def _planned_quotas(profile: DatasetProfile) -> CellQuotas:
    """Walk DATASET_MIX in generator order and return each cell's split quotas."""
    mix = DATASET_MIX[profile]
    carry = dict.fromkeys(SPLIT_FRACTIONS, 0.0)
    quotas: CellQuotas = {}
    for doc_type in DocType:
        for tier in QualityTier:
            count = mix.get((doc_type, tier))
            if not count:
                continue
            quotas[doc_type, tier], carry = split_quotas(count, carry)
    return quotas


class _StubPdfRenderer:
    def __enter__(self) -> "_StubPdfRenderer":
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def render(
        self, doc: StatementDoc, template_id: str, seed: int, out_path: Path
    ) -> dict[str, str]:
        out_path.write_bytes(b"%PDF-1.4 stub")
        return {"header:statement_number": doc.statement_number, "header:template": template_id}


def _stub_tabular(doc: StatementDoc, template_id: str, seed: int, out_path: Path) -> dict[str, str]:
    out_path.write_bytes(b"stub,tabular\n")
    return {"header:statement_number": doc.statement_number}


def _stub_degrade(pdf_path: Path, profile: str, seed: int) -> None:
    pdf_path.write_bytes(b"%PDF-1.4 degraded " + profile.encode("utf-8"))


def _stub_corrupt(kind: CorruptionKind, seed: int, out_path: Path) -> None:
    out_path.write_bytes(b"corrupted " + kind.value.encode("utf-8"))


_HOOKS = RenderHooks(
    pdf_renderer_factory=_StubPdfRenderer,
    render_csv=_stub_tabular,
    render_xlsx=_stub_tabular,
    degrade_to_scan=_stub_degrade,
    write_corrupted=_stub_corrupt,
)


@pytest.fixture(scope="module")
def small_run(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, DatasetManifest]:
    out_dir = tmp_path_factory.mktemp("small-stub")
    return out_dir, _generate_dataset(MASTER_SEED, out_dir, DatasetProfile.SMALL, _HOOKS)


@pytest.fixture(scope="module")
def full_run(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, DatasetManifest]:
    out_dir = tmp_path_factory.mktemp("full-stub")
    return out_dir, _generate_dataset(MASTER_SEED, out_dir, DatasetProfile.FULL, _HOOKS)


def test_mix_totals() -> None:
    assert sum(DATASET_MIX[DatasetProfile.FULL].values()) == 210
    assert sum(DATASET_MIX[DatasetProfile.SMALL].values()) == 10
    assert sum(CORRUPTED_MIX[DatasetProfile.FULL].values()) == 10
    assert sum(CORRUPTED_MIX[DatasetProfile.SMALL].values()) == 2


def test_small_run_counts_match_mix(small_run: tuple[Path, DatasetManifest]) -> None:
    _, manifest = small_run
    observed: Counter[tuple[DocType, QualityTier]] = Counter()
    corrupted: Counter[CorruptionKind] = Counter()
    for record in manifest.records:
        if record.truth is None:
            assert record.corruption is not None
            corrupted[record.corruption] += 1
        else:
            observed[record.truth.doc_type, record.quality_tier] += 1
    assert observed == Counter(DATASET_MIX[DatasetProfile.SMALL])
    assert corrupted == Counter(CORRUPTED_MIX[DatasetProfile.SMALL])


def test_full_run_counts_match_mix(full_run: tuple[Path, DatasetManifest]) -> None:
    _, manifest = full_run
    observed: Counter[tuple[DocType, QualityTier]] = Counter()
    for record in manifest.records:
        if record.truth is not None:
            observed[record.truth.doc_type, record.quality_tier] += 1
    assert observed == Counter(DATASET_MIX[DatasetProfile.FULL])
    assert len(manifest.records) == 220


def test_every_file_written_with_relative_posix_paths(
    small_run: tuple[Path, DatasetManifest],
) -> None:
    out_dir, manifest = small_run
    for record in manifest.records:
        assert "\\" not in record.file_path
        assert record.file_path.startswith("files/")
        assert (out_dir / record.file_path).is_file()
    assert (out_dir / "manifest.json").is_file()
    assert (out_dir / "ledger.json").is_file()


def test_doc_ids_unique_and_seeded(small_run: tuple[Path, DatasetManifest]) -> None:
    _, manifest = small_run
    doc_ids = [record.doc_id for record in manifest.records]
    assert len(doc_ids) == len(set(doc_ids))
    for record in manifest.records:
        assert record.render_seed == record_seed(MASTER_SEED, record.doc_id)
        if record.truth is not None:
            assert record.template_id == f"{record.truth.oem}-{record.truth.doc_type}-v1"


def test_scan_tiers_record_augraphy_profile(full_run: tuple[Path, DatasetManifest]) -> None:
    _, manifest = full_run
    scan_tiers = {QualityTier.SCAN_LIGHT, QualityTier.SCAN_HEAVY}
    for record in manifest.records:
        if record.quality_tier in scan_tiers:
            assert record.augraphy_profile == record.quality_tier.value
        else:
            assert record.augraphy_profile is None


def test_splits_are_stratified_50_25_25(full_run: tuple[Path, DatasetManifest]) -> None:
    _, manifest = full_run
    cells: dict[tuple[DocType, QualityTier], Counter[SplitName]] = {}
    for record in manifest.records:
        if record.truth is None:
            assert record.split is None
            continue
        assert record.split is not None
        key = (record.truth.doc_type, record.quality_tier)
        cells.setdefault(key, Counter())[record.split] += 1
    for key, counts in cells.items():
        total = sum(counts.values())
        assert total == DATASET_MIX[DatasetProfile.FULL][key]
        if total % 4 == 0:
            assert counts[SplitName.TRAIN] == total // 2, key
            assert counts[SplitName.CALIBRATION] == total // 4, key
            assert counts[SplitName.TEST] == total // 4, key


def test_full_plan_never_starves_a_split() -> None:
    # Render-free: quotas come straight from the plan, no dataset is generated.
    mix = DATASET_MIX[DatasetProfile.FULL]
    for cell, quotas in _planned_quotas(DatasetProfile.FULL).items():
        count = mix[cell]
        assert sum(quotas.values()) == count, cell
        if count >= 2:
            assert quotas[SplitName.TEST] >= 1, cell
        if count >= 3:
            assert all(quotas[split] >= 1 for split in SplitName), cell


def test_full_plan_quotas_hit_50_25_25() -> None:
    totals: Counter[SplitName] = Counter()
    for quotas in _planned_quotas(DatasetProfile.FULL).values():
        totals.update(quotas)
    assert sum(totals.values()) == sum(DATASET_MIX[DatasetProfile.FULL].values())
    assert totals[SplitName.TRAIN] == EXPECTED_FULL_TRAIN
    assert abs(totals[SplitName.CALIBRATION] - totals[SplitName.TEST]) <= 1


def test_full_run_split_counts_match_the_plan(full_run: tuple[Path, DatasetManifest]) -> None:
    _, manifest = full_run
    observed: Counter[SplitName] = Counter()
    for record in manifest.records:
        if record.split is not None:
            observed[record.split] += 1
    expected: Counter[SplitName] = Counter()
    for quotas in _planned_quotas(DatasetProfile.FULL).values():
        expected.update(quotas)
    assert observed == expected


def test_floorplan_xlsx_cell_reaches_every_split(full_run: tuple[Path, DatasetManifest]) -> None:
    # Three documents in the cell: the old ceil() rounding gave test none of them.
    _, manifest = full_run
    splits = {
        record.split
        for record in manifest.records
        if record.truth is not None
        and record.truth.doc_type is DocType.FLOORPLAN_STATEMENT
        and record.quality_tier is QualityTier.XLSX
    }
    assert splits == set(SplitName)


def test_roughly_two_thirds_of_docs_carry_injections(
    full_run: tuple[Path, DatasetManifest],
) -> None:
    _, manifest = full_run
    scored = [record for record in manifest.records if record.truth is not None]
    injected = [record for record in scored if record.injected]
    assert 0.45 <= len(injected) / len(scored) <= 0.85
    for record in injected:
        assert 1 <= len(record.injected) <= 3


def test_small_seed_42_carries_at_least_one_injection(
    small_run: tuple[Path, DatasetManifest],
) -> None:
    # The contract fixture asserts this on the real SMALL dataset at seed 42.
    _, manifest = small_run
    assert any(record.injected for record in manifest.records)


def test_same_seed_produces_byte_identical_json(
    tmp_path: Path, small_run: tuple[Path, DatasetManifest]
) -> None:
    first_dir, _ = small_run
    second_dir = tmp_path / "again"
    second_dir.mkdir()
    _generate_dataset(MASTER_SEED, second_dir, DatasetProfile.SMALL, _HOOKS)
    assert (first_dir / "manifest.json").read_bytes() == (second_dir / "manifest.json").read_bytes()
    assert (first_dir / "ledger.json").read_bytes() == (second_dir / "ledger.json").read_bytes()


def test_different_seed_changes_records(
    tmp_path: Path, small_run: tuple[Path, DatasetManifest]
) -> None:
    _, manifest_42 = small_run
    other_dir = tmp_path / "seed-43"
    other_dir.mkdir()
    manifest_43 = _generate_dataset(MASTER_SEED + 1, other_dir, DatasetProfile.SMALL, _HOOKS)
    assert manifest_43.records != manifest_42.records


def test_config_hash_is_stable_and_parameter_sensitive() -> None:
    assert _config_hash(42, DatasetProfile.SMALL) == _config_hash(42, DatasetProfile.SMALL)
    assert _config_hash(42, DatasetProfile.SMALL) != _config_hash(43, DatasetProfile.SMALL)
    assert _config_hash(42, DatasetProfile.SMALL) != _config_hash(42, DatasetProfile.FULL)


def test_corrupted_records_have_no_truth_split_or_rendered_values(
    small_run: tuple[Path, DatasetManifest],
) -> None:
    _, manifest = small_run
    corrupted = [
        record for record in manifest.records if record.quality_tier is QualityTier.CORRUPTED
    ]
    assert len(corrupted) == 2
    for record in corrupted:
        assert record.truth is None
        assert record.split is None
        assert record.corruption is not None
        assert record.rendered_values == {}
        assert record.injected == ()
