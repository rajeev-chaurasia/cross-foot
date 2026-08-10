"""Shared fixtures for the contract suite.

Contract tests were written against docs/contracts-phase1.md before the
implementation existed. It exists now, so the generator is imported plainly: a
skip inside a shared fixture drops every test that asks for it and still reports
a green build.
"""

from pathlib import Path

import pytest

from crossfoot.generator import dataset
from crossfoot.models.manifest import DatasetManifest


@pytest.fixture(scope="session")
def small_dataset(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, DatasetManifest]:
    """Generate the SMALL profile once per session and share it across tests.

    Imported plainly rather than through importorskip. A skip inside a fixture
    silently drops every test that asks for it, which reads as a green build.
    """
    out_dir = tmp_path_factory.mktemp("small-dataset")
    manifest = dataset.generate_dataset(
        master_seed=42, out_dir=out_dir, profile=dataset.DatasetProfile.SMALL
    )
    assert isinstance(manifest, DatasetManifest)
    return out_dir, manifest
