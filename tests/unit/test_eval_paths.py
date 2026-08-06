"""Manifest paths are untrusted data: containment is the only thing standing between
a hostile record and an arbitrary file read."""

from pathlib import Path

import pytest

from crossfoot.evals.paths import UnsafeDatasetPathError, resolve_dataset_path

HOSTILE_PATHS = (
    "../../../../Windows/win.ini",  # relative escape
    "files/../../secrets.txt",  # escape hidden mid-path
    "C:/Windows/win.ini",  # absolute, and pathlib would discard the base
    "C:\\Windows\\win.ini",
    "//./C:/Windows/win.ini",  # device path form
    "\\\\?\\C:\\Windows\\win.ini",
    "\\\\server\\share\\secret.txt",  # UNC
    "/etc/passwd",
    "C:secret.txt",  # drive relative
    "",
    "   ",
)


@pytest.fixture
def dataset_dir(tmp_path: Path) -> Path:
    (tmp_path / "files" / "nested").mkdir(parents=True)
    (tmp_path / "files" / "nested" / "statement.csv").write_bytes(b"Amount\n1.00\n")
    return tmp_path


@pytest.mark.parametrize("hostile", HOSTILE_PATHS)
def test_hostile_paths_are_rejected(dataset_dir: Path, hostile: str) -> None:
    with pytest.raises(UnsafeDatasetPathError):
        resolve_dataset_path(dataset_dir, hostile)


def test_legitimate_nested_relative_path_resolves(dataset_dir: Path) -> None:
    resolved = resolve_dataset_path(dataset_dir, "files/nested/statement.csv")
    assert resolved == (dataset_dir / "files" / "nested" / "statement.csv").resolve()
    assert resolved.read_bytes() == b"Amount\n1.00\n"


def test_resolved_path_stays_inside_the_dataset(dataset_dir: Path) -> None:
    resolved = resolve_dataset_path(dataset_dir, "files/nested/statement.csv")
    assert resolved.is_relative_to(dataset_dir.resolve())
