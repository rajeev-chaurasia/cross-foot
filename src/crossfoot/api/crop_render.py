"""Rendering the pixels behind a value, once, on the request that first asks for them.

docs/contracts-phase3.md binds this: crops are rendered lazily and cached under
`data/crops/{doc_id}/{field_id}.png`. Lazily means the first request pays for
one page raster and every later request is a file read.

Containment is settled before anything here runs. The route only ever calls this
with a destination `resolve_dataset_path` already built inside the crop root,
and the source document goes through the same function against the dataset
directory, because a `file_path` column came from a manifest and a manifest is
data, never a trusted instruction.

A source that cannot be rasterized is a fact about the document, not a bug in
the server, so it raises CropSourceError and the route answers with a typed body
the queue can render around.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

import numpy as np
import pypdfium2

from crossfoot.api.dto import CropUnavailableReason
from crossfoot.db.crops import CropSource
from crossfoot.evals.paths import UnsafeDatasetPathError, resolve_dataset_path
from crossfoot.extraction import crops

# 200 dpi rather than the vision path's VISION_DPI of 180. A review crop is read
# by a human at full size instead of being tokenized, so there is no image token
# budget to protect, and 200 is the resolution the dataset's scanned pages were
# rasterized at, so a crop lands on the scan's own pixels rather than resampling
# them. The cost is one raster per field, paid once.
REVIEW_CROP_DPI = 200
PDF_POINTS_PER_INCH = 72

MISSING_SOURCE_DETAIL = "the dataset holds no file at {file_path}"
MISSING_PAGE_DETAIL = "page {page} is not in a document of {pages} pages"
# The suffix of the temporary file an in-flight render writes to, before the
# rename that publishes it.
PENDING_SUFFIX = ".pending"


class CropSourceError(Exception):
    """The field exists, but its pixels cannot be produced from the source document."""

    def __init__(self, reason: CropUnavailableReason, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


def render_crop_file(*, source: CropSource, dataset_dir: Path, destination: Path) -> None:
    """Rasterize the field's page, cut its region, and cache the PNG at destination."""
    image = _page_image(_source_path(dataset_dir, source.file_path), source.page)
    box, _kind = crops.review_region(source.bbox, image)
    _publish(destination, crops.encode_png(image[box.top : box.bottom, box.left : box.right]))


def _source_path(dataset_dir: Path, file_path: str) -> Path:
    """The document under the dataset directory, contained the same way the runner contains it."""
    try:
        path = resolve_dataset_path(dataset_dir, file_path)
    except UnsafeDatasetPathError as error:
        raise CropSourceError(CropUnavailableReason.SOURCE_UNREACHABLE, str(error)) from error
    if not path.is_file():
        raise CropSourceError(
            CropUnavailableReason.SOURCE_MISSING,
            MISSING_SOURCE_DETAIL.format(file_path=file_path),
        )
    return path


def _page_image(path: Path, page: int) -> crops.Image:
    """One rasterized page, as the BGR array the crop helpers work in."""
    try:
        document = pypdfium2.PdfDocument(path)
    except (pypdfium2.PdfiumError, OSError) as error:
        raise CropSourceError(CropUnavailableReason.SOURCE_UNREADABLE, str(error)) from error
    try:
        pages = len(document)
        if not 0 <= page < pages:
            raise CropSourceError(
                CropUnavailableReason.PAGE_MISSING,
                MISSING_PAGE_DETAIL.format(page=page, pages=pages),
            )
        rendered = document[page].render(scale=REVIEW_CROP_DPI / PDF_POINTS_PER_INCH).to_pil()
    except (pypdfium2.PdfiumError, OSError) as error:
        raise CropSourceError(CropUnavailableReason.SOURCE_UNREADABLE, str(error)) from error
    finally:
        document.close()
    # cv2 encodes BGR, so the channel order is flipped exactly once, here.
    image: crops.Image = np.ascontiguousarray(
        np.asarray(rendered.convert("RGB"), dtype=np.uint8)[:, :, ::-1]
    )
    return image


def _publish(destination: Path, payload: bytes) -> None:
    """Write then rename, so two requests racing for one crop cannot serve half a file."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    # One name per writer: the handlers run in a threadpool, so two of them can
    # be rendering the same field at once.
    pending = destination.with_name(
        f"{destination.name}.{os.getpid()}-{threading.get_ident()}{PENDING_SUFFIX}"
    )
    pending.write_bytes(payload)
    pending.replace(destination)
