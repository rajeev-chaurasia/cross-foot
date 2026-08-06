"""Containment for manifest-declared paths.

A manifest is data, not a trusted instruction: every path it names is resolved
under the dataset directory or rejected, so a hostile record cannot make the
runner read files elsewhere on the machine.
"""

from pathlib import Path, PureWindowsPath


class UnsafeDatasetPathError(ValueError):
    """A manifest path is absolute, escapes the dataset directory, or is empty."""


def resolve_dataset_path(dataset_dir: Path, relative_path: str) -> Path:
    """Resolve a manifest-relative path inside dataset_dir, or raise UnsafeDatasetPathError."""
    if not relative_path.strip():
        raise UnsafeDatasetPathError("manifest path is empty")
    # Windows semantics are the strict reading everywhere: they recognize drive
    # letters, UNC and device prefixes, and backslash separators.
    candidate = PureWindowsPath(relative_path)
    if candidate.drive or candidate.root or candidate.is_absolute():
        raise UnsafeDatasetPathError(f"manifest path is absolute: {relative_path!r}")
    if any(part == ".." for part in candidate.parts):
        raise UnsafeDatasetPathError(f"manifest path escapes the dataset: {relative_path!r}")
    base = dataset_dir.resolve()
    resolved = (base / relative_path).resolve()
    if not resolved.is_relative_to(base):
        raise UnsafeDatasetPathError(f"manifest path escapes the dataset: {relative_path!r}")
    return resolved
