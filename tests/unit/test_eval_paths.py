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

# A null byte is not a traversal and every check above lets it through, so it
# reaches the filesystem call inside resolve() and comes back out as a bare
# ValueError. Containment has to answer it the same way it answers the rest.
NULL_BYTE_PATHS = (
    "\x00",
    "files/\x00",
    "files/nested/\x00statement.csv",
    "files/nested/statement.csv\x00",
    "files/nested/statement.csv\x00.png",
    "\x00/files/nested/statement.csv",
    "..\x00/secrets.txt",
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


@pytest.mark.parametrize("hostile", NULL_BYTE_PATHS)
def test_a_null_byte_is_rejected_as_an_unsafe_path(dataset_dir: Path, hostile: str) -> None:
    # The type is the assertion: a bare ValueError out of resolve() is a caller's
    # 500, while UnsafeDatasetPathError is the rejection every caller handles.
    with pytest.raises(UnsafeDatasetPathError):
        resolve_dataset_path(dataset_dir, hostile)


@pytest.mark.parametrize("hostile", NULL_BYTE_PATHS)
def test_a_null_byte_is_named_in_the_rejection(dataset_dir: Path, hostile: str) -> None:
    with pytest.raises(UnsafeDatasetPathError, match="null byte"):
        resolve_dataset_path(dataset_dir, hostile)


def test_a_null_byte_is_rejected_before_the_filesystem_is_touched(
    dataset_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(self: Path, strict: bool = False) -> Path:
        raise AssertionError(f"resolve() reached for {self!r}")

    monkeypatch.setattr(Path, "resolve", explode)
    with pytest.raises(UnsafeDatasetPathError):
        resolve_dataset_path(dataset_dir, "files/nested/statement.csv\x00.png")


def test_legitimate_nested_relative_path_resolves(dataset_dir: Path) -> None:
    resolved = resolve_dataset_path(dataset_dir, "files/nested/statement.csv")
    assert resolved == (dataset_dir / "files" / "nested" / "statement.csv").resolve()
    assert resolved.read_bytes() == b"Amount\n1.00\n"


def test_resolved_path_stays_inside_the_dataset(dataset_dir: Path) -> None:
    resolved = resolve_dataset_path(dataset_dir, "files/nested/statement.csv")
    assert resolved.is_relative_to(dataset_dir.resolve())
