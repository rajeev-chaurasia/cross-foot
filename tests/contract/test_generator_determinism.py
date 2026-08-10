"""Determinism contracts: same seed means same bytes, different seed means different data.

Scope, stated because the obvious reading of the name is wrong. The full profile does not
reproduce byte for byte: its 17 `scan_heavy` renders differ on every run, because one
Augraphy augmentation in that profile draws from a global random source neither seed
reaches. That is a known defect, recorded under Limitations in the README. The small
profile carries no `scan_heavy` document, so what is pinned here is every other tier,
including the `scan_light` renders that go through Augraphy as well. If non determinism
ever spreads beyond the heavy tier, this is what catches it.
"""

import hashlib
from pathlib import Path

import pytest

from crossfoot.models.ledger import LedgerBook
from crossfoot.models.manifest import DatasetManifest


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
