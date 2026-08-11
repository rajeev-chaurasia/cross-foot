"""Determinism contracts: same seed means same bytes, different seed means different data.

The small profile carries no `scan_heavy` document, and that tier is where determinism was
lost once already: `DirtyRollers` runs as numba compiled code with its own generator, which
seeding the interpreter cannot reach. So the heavy tier is pinned directly against
`degrade_to_scan` rather than through a profile, because generating the full profile is far
too slow for CI and one heavy document in the small profile would exercise `DirtyRollers`
only on a coin flip.
"""

import hashlib
import shutil
from pathlib import Path

import pytest

from crossfoot.constants import QualityTier
from crossfoot.models.ledger import LedgerBook
from crossfoot.models.manifest import DatasetManifest

SCAN_HEAVY = "scan_heavy"
# _heavy_pipeline picks a degrader and a transport off this seed. These four cover both
# branches of each pick, so the compiled path is always among them.
BRANCH_SEEDS = [4, 10, 0, 1]
BRANCH_IDS = [
    "inkbleed and dirtyrollers",
    "badphotocopy and dirtyrollers",
    "badphotocopy and faxify",
    "inkbleed and faxify",
]


def _digests(root: Path) -> dict[Path, str]:
    return {
        path.relative_to(root): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_same_seed_reproduces_every_file_the_small_profile_writes(tmp_path: Path) -> None:
    dataset = pytest.importorskip("crossfoot.generator.dataset")
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    dataset.generate_dataset(master_seed=42, out_dir=first, profile=dataset.DatasetProfile.SMALL)
    dataset.generate_dataset(master_seed=42, out_dir=second, profile=dataset.DatasetProfile.SMALL)

    assert (first / "manifest.json").read_bytes() == (second / "manifest.json").read_bytes()
    assert (first / "ledger.json").read_bytes() == (second / "ledger.json").read_bytes()
    # Rendered artifacts too, not only the records read off them, so a renderer that
    # starts drawing from an unseeded source fails here rather than in a scorecard.
    assert _digests(first) == _digests(second)


@pytest.mark.parametrize("seed", BRANCH_SEEDS, ids=BRANCH_IDS)
def test_the_heavy_scan_profile_reproduces_byte_for_byte(
    tmp_path: Path, small_dataset: tuple[Path, DatasetManifest], seed: int
) -> None:
    """The tier that lost determinism once, pinned on both of its pipeline branches."""
    from crossfoot.generator import degrade

    root, manifest = small_dataset
    source = root / next(
        record.file_path
        for record in manifest.records
        if record.quality_tier is QualityTier.CLEAN_DIGITAL
    )
    rendered = []
    for name in ("first.pdf", "second.pdf"):
        target = tmp_path / name
        shutil.copy(source, target)
        degrade.degrade_to_scan(target, SCAN_HEAVY, seed)
        rendered.append(target.read_bytes())

    assert rendered[0] == rendered[1]


def test_a_different_seed_degrades_a_page_differently(
    tmp_path: Path, small_dataset: tuple[Path, DatasetManifest]
) -> None:
    """Guards the other direction: seeding must not pin the tier to one outcome."""
    from crossfoot.generator import degrade

    root, manifest = small_dataset
    source = root / next(
        record.file_path
        for record in manifest.records
        if record.quality_tier is QualityTier.CLEAN_DIGITAL
    )
    rendered = []
    for name, seed in (("four.pdf", BRANCH_SEEDS[0]), ("ten.pdf", BRANCH_SEEDS[1])):
        target = tmp_path / name
        shutil.copy(source, target)
        degrade.degrade_to_scan(target, SCAN_HEAVY, seed)
        rendered.append(target.read_bytes())

    assert rendered[0] != rendered[1]


def test_generate_ledger_is_deterministic() -> None:
    ledger_gen = pytest.importorskip("crossfoot.generator.ledger_gen")
    book_a = ledger_gen.generate_ledger(42)
    book_b = ledger_gen.generate_ledger(42)
    assert isinstance(book_a, LedgerBook)
    assert isinstance(book_b, LedgerBook)
    assert book_a == book_b


def test_different_seed_produces_different_records(
    tmp_path: Path, small_dataset: tuple[Path, DatasetManifest]
) -> None:
    dataset = pytest.importorskip("crossfoot.generator.dataset")
    _, manifest_42 = small_dataset
    other = tmp_path / "seed-43"
    other.mkdir()
    manifest_43 = dataset.generate_dataset(
        master_seed=43, out_dir=other, profile=dataset.DatasetProfile.SMALL
    )
    assert isinstance(manifest_43, DatasetManifest)
    assert manifest_43.records != manifest_42.records
