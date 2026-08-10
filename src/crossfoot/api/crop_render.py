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
from typing import Any

import numpy as np

from crossfoot import pdfium
from crossfoot.api.dto import CropUnavailableReason
from crossfoot.constants import MAX_PAGE_PIXELS, CropKind
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
OVERSIZE_PAGE_DETAIL = "page {page} renders to {pixels} pixels, over the {limit} pixel limit"
# A row_position counts the rows visible on one page. Which page a model was
# looking at is not recorded, so on a document of several pages the number
# indexes nothing and the whole page is the only honest answer.
SINGLE_PAGE = 1
# The suffix of the temporary file an in-flight render writes to, before the
# rename that publishes it.
PENDING_SUFFIX = ".pending"


class CropSourceError(Exception):
    """The field exists, but its pixels cannot be produced from the source document."""

    def __init__(self, reason: CropUnavailableReason, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


def render_crop_file(*, source: CropSource, dataset_dir: Path, destination: Path) -> CropKind:
    """Rasterize the field's page, cut its region, and cache the PNG at destination.

    Returns how the region was actually found, which is knowable only here: the
    row band needs the page image the extractor no longer has.
    """
    image, pages = _page_image(_source_path(dataset_dir, source.file_path), source.page)
    region = crops.review_region(
        source.bbox,
        image,
        row_position=source.row_position if pages == SINGLE_PAGE else None,
        expected_rows=source.expected_rows,
        hint=source.hint,
    )
    _publish(destination, crops.encode_png(crops.fit_to_served_edge(region.cut())))
    return region.kind


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


def _page_image(path: Path, page: int) -> tuple[crops.Image, int]:
    """One rasterized page as the BGR array the crop helpers work in, and the page count.

    PDFium is not thread safe and this is the API's rasterization path, so the
    whole document scope is serialized by `pdfium.open_document`. The copy into
    numpy happens inside that scope too: the rendered bitmap is memory PDFium
    owns, and the crop helpers run on this array long after it is closed.

    The pixel budget is read off the MediaBox before the render, because
    fit_to_served_edge only shrinks an image that already exists and a page may
    legally declare 200 inches a side. A page over the budget is unreadable in
    the same sense a damaged one is: its pixels cannot be produced here.
    """
    try:
        with pdfium.open_document(path) as document:
            pages = len(document)
            if not 0 <= page < pages:
                raise CropSourceError(
                    CropUnavailableReason.PAGE_MISSING,
                    MISSING_PAGE_DETAIL.format(page=page, pages=pages),
                )
            scale = REVIEW_CROP_DPI / PDF_POINTS_PER_INCH
            _refuse_oversize_page(document[page], page, scale)
            rendered = document[page].render(scale=scale).to_pil()
            # cv2 encodes BGR, so the channel order is flipped exactly once, here.
            image: crops.Image = np.ascontiguousarray(
                np.asarray(rendered.convert("RGB"), dtype=np.uint8)[:, :, ::-1]
            )
    except (pdfium.PdfiumError, OSError) as error:
        raise CropSourceError(CropUnavailableReason.SOURCE_UNREADABLE, str(error)) from error
    return image, pages


def _refuse_oversize_page(page: Any, index: int, scale: float) -> None:
    """Refuse a page whose declared size would allocate more than the budget allows."""
    width, height = page.get_size()
    pixels = round(width * scale) * round(height * scale)
    if pixels > MAX_PAGE_PIXELS:
        raise CropSourceError(
            CropUnavailableReason.SOURCE_UNREADABLE,
            OVERSIZE_PAGE_DETAIL.format(page=index, pixels=pixels, limit=MAX_PAGE_PIXELS),
        )


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
