"""Containment for manifest-declared paths.

A manifest is data, not a trusted instruction: every path it names is resolved
under the dataset directory or rejected, so a hostile record cannot make the
runner read files elsewhere on the machine.
"""

from pathlib import Path, PureWindowsPath


class UnsafeDatasetPathError(ValueError):
    """A manifest path is absolute, escapes the dataset directory, is empty, or is unusable."""


def resolve_dataset_path(dataset_dir: Path, relative_path: str) -> Path:
    """Resolve a manifest-relative path inside dataset_dir, or raise UnsafeDatasetPathError."""
    if not relative_path.strip():
        raise UnsafeDatasetPathError("manifest path is empty")
    # A null byte is not a traversal, but it survives every check below and the
    # filesystem call inside resolve() raises on it, so containment has to fail
    # closed here rather than let a ValueError out of the resolver.
    if "\x00" in relative_path:
        raise UnsafeDatasetPathError(f"manifest path contains a null byte: {relative_path!r}")
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
