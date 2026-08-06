"""Shared fixtures for the contract suite.

Contract tests are written against docs/contracts-phase1.md before the
implementation exists; importorskip keeps collection green until it lands.
"""

from pathlib import Path

import pytest

from crossfoot.models.manifest import DatasetManifest


@pytest.fixture(scope="session")
def small_dataset(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, DatasetManifest]:
    """Generate the SMALL profile once per session and share it across tests."""
    dataset = pytest.importorskip("crossfoot.generator.dataset")
    out_dir = tmp_path_factory.mktemp("small-dataset")
    manifest = dataset.generate_dataset(
        master_seed=42, out_dir=out_dir, profile=dataset.DatasetProfile.SMALL
    )
    assert isinstance(manifest, DatasetManifest)
    return out_dir, manifest
