"""Where a field's crop lives, and what the render that cut it decided.

Two routes ask about the same crop and they ask different questions. The PNG
route wants the bytes, and a cached file is an answer to that on its own. The
review item wants the caption that goes over those bytes, and a file on disk is
not an answer to that at all: how a crop was cut is a decision only the render
makes, so the item settles the render rather than guessing from what it finds.

The kind is computed by the render and persisted, never derived on read. Only
the render holds the page image the row band decision is made on, so re-deriving
it would rasterize the page again on every read of the queue; `rendered_crops`
is that decision's one record and the render is its one writer. It is a table of
its own rather than the `fields.crop_kind` the extractor filled in, because that
column is what could be told about a value before any page was looked at, and
one column meaning both left a caption free to contradict the picture under it.

A record can go missing under a crop that has not: rebuilding the review
database replaces every extraction row, and the crops on disk are left where
they are. Asking for a kind then renders again and writes the PNG and the record
together, so the two are settled at the same moment and by the same decision.

Containment comes first in both paths: both segments go through
`resolve_dataset_path` before anything is opened, so a hostile pair is refused
before the database is read and long before the renderer sees it.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from crossfoot.api.crop_render import render_crop_file
from crossfoot.api.deps import ApiPaths
from crossfoot.constants import CropKind
from crossfoot.db import crops as crop_db
from crossfoot.evals.paths import resolve_dataset_path

# The extension the cache is keyed by. Spelled here rather than imported from
# crossfoot.extraction.crops: reading a cached file needs no image stack.
CROP_SUFFIX = ".png"


class UnknownCropFieldError(LookupError):
    """The pair names no field, so there is nothing to render and nothing to caption."""


@dataclass(frozen=True, slots=True)
class RenderedCrop:
    """A cached PNG and the kind the render that wrote it settled on."""

    path: Path
    kind: CropKind


def cached_path(paths: ApiPaths, *, doc_id: str, field_id: str) -> Path:
    """Where this field's crop is cached, inside the crop root or not at all.

    Raises `UnsafeDatasetPathError` for either segment, before any file is
    opened and before the database is read.
    """
    directory = resolve_dataset_path(paths.crops_root, doc_id)
    return resolve_dataset_path(directory, f"{field_id}{CROP_SUFFIX}")


def rendered_crop(
    paths: ApiPaths, connection: sqlite3.Connection, *, doc_id: str, field_id: str
) -> RenderedCrop:
    """The field's crop together with the kind the render that cut it recorded.

    Renders unless both halves are already there, since a PNG whose render was
    never recorded cannot be captioned and a record whose PNG is gone names no
    picture.

    Raises `UnsafeDatasetPathError` for a segment that leaves the crop root,
    `UnknownCropFieldError` when the pair names no field, and `CropSourceError`
    when the document behind it has no pixels to give.
    """
    path = cached_path(paths, doc_id=doc_id, field_id=field_id)
    source = crop_db.source(connection, doc_id=doc_id, field_id=field_id)
    if source is None:
        raise UnknownCropFieldError(field_id)
    recorded = crop_db.rendered_kind(connection, field_id)
    if recorded is not None and path.is_file():
        return RenderedCrop(path=path, kind=recorded)
    kind = render_crop_file(source=source, dataset_dir=paths.dataset_dir, destination=path)
    crop_db.record_kind(connection, field_id=field_id, kind=kind)
    return RenderedCrop(path=path, kind=kind)
