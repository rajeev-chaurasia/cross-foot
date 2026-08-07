"""Review crops: the picture a human sees beside a low confidence value.

Correctness never depends on model coordinates. The deterministic path crops the
union of the word boxes that produced the value. The LLM path finds table row
stripes with a horizontal projection profile and takes the stripe at the
reported row_position; the model's own bbox refines that band only after it
overlaps a detected stripe, and is discarded silently otherwise. FULL_PAGE is
always available underneath. Crops are never scored.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
import numpy.typing as npt

from crossfoot.constants import CropKind
from crossfoot.models.extraction import BBox, ExtractedField

_LOGGER = logging.getLogger(__name__)

DEFAULT_CROPS_ROOT = Path("data/crops")
CROP_SUFFIX = ".png"

# A row of print marks this share of the page width with ink.
MIN_INK_WIDTH_FRACTION = 0.02
# Runs thinner than this are speckle, not a line of text.
MIN_STRIPE_HEIGHT_PX = 2
# Bands are padded by one row of their own height on each side.
BAND_PAD_ROWS = 1

Image = npt.NDArray[np.uint8]


@dataclass(frozen=True, slots=True)
class PixelBox:
    """Half-open pixel region, origin at the top left."""

    left: int
    top: int
    right: int
    bottom: int

    def is_empty(self) -> bool:
        return self.right <= self.left or self.bottom <= self.top

    def intersect(self, other: PixelBox) -> PixelBox:
        return PixelBox(
            left=max(self.left, other.left),
            top=max(self.top, other.top),
            right=min(self.right, other.right),
            bottom=min(self.bottom, other.bottom),
        )


@dataclass(frozen=True, slots=True)
class Crop:
    path: Path
    kind: CropKind


def crop_path(doc_id: str, field_id: str, *, root: Path = DEFAULT_CROPS_ROOT) -> Path:
    return root / doc_id / f"{field_id}{CROP_SUFFIX}"


def render_crop(
    field: ExtractedField,
    page_png: bytes,
    *,
    row_position: int | None = None,
    root: Path = DEFAULT_CROPS_ROOT,
) -> Crop:
    """Render one review crop, reusing the cached file when it already exists."""
    path = crop_path(field.doc_id, field.field_id, root=root)
    image = decode_png(page_png)
    box, kind = region_for(field, image, row_position=row_position)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(encode_png(image[box.top : box.bottom, box.left : box.right]))
    return Crop(path=path, kind=kind)


def region_for(
    field: ExtractedField, image: Image, *, row_position: int | None = None
) -> tuple[PixelBox, CropKind]:
    """The region to crop and the honest name for how it was found."""
    page = full_page(image)
    if field.crop_kind is CropKind.EXACT_BBOX and field.bbox is not None:
        exact = _to_pixels(field.bbox, image).intersect(page)
        if not exact.is_empty():
            return exact, CropKind.EXACT_BBOX
    if row_position is not None:
        band = row_band(image, row_position)
        if band is not None:
            return _refined(band, field.bbox, image), CropKind.ROW_BAND
    return page, CropKind.FULL_PAGE


def full_page(image: Image) -> PixelBox:
    height, width = image.shape[:2]
    return PixelBox(left=0, top=0, right=int(width), bottom=int(height))


def row_stripes(image: Image) -> tuple[tuple[int, int], ...]:
    """Inked horizontal bands, top to bottom, from the projection profile."""
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
    _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)
    ink_per_row = np.count_nonzero(binary, axis=1)
    inked = ink_per_row >= max(1, int(binary.shape[1] * MIN_INK_WIDTH_FRACTION))
    return tuple(
        (start, stop) for start, stop in _runs(inked) if stop - start >= MIN_STRIPE_HEIGHT_PX
    )


def row_band(image: Image, row_position: int) -> PixelBox | None:
    """The stripe at a 1-based row_position, padded one row each side.

    The profile sees every band of print on the page, not only table rows, so
    this is an approximate anchor. It is a review aid and is never scored.
    """
    stripes = row_stripes(image)
    if not 1 <= row_position <= len(stripes):
        return None
    top, bottom = stripes[row_position - 1]
    pad = (bottom - top) * BAND_PAD_ROWS
    height, width = image.shape[:2]
    return PixelBox(
        left=0,
        top=max(0, top - pad),
        right=int(width),
        bottom=min(int(height), bottom + pad),
    )


def decode_png(png_bytes: bytes) -> Image:
    image = cv2.imdecode(np.frombuffer(png_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("page image is not decodable")
    return image.astype(np.uint8)


def encode_png(image: Image) -> bytes:
    ok, buffer = cv2.imencode(CROP_SUFFIX, image)
    if not ok:
        raise ValueError("crop could not be encoded as png")
    encoded: bytes = buffer.tobytes()
    return encoded


def _refined(band: PixelBox, bbox: BBox | None, image: Image) -> PixelBox:
    """Narrow the band with the model's bbox, but only if it overlaps real ink."""
    if bbox is None:
        return band
    hinted = _to_pixels(bbox, image)
    overlaps = any(
        hinted.top < stop and start < hinted.bottom for start, stop in row_stripes(image)
    )
    if not overlaps:
        return band  # discarded silently: an unanchored hint refines nothing
    narrowed = band.intersect(hinted)
    return band if narrowed.is_empty() else narrowed


def _to_pixels(bbox: BBox, image: Image) -> PixelBox:
    height, width = image.shape[:2]
    return PixelBox(
        left=int(bbox.x0 * width),
        top=int(bbox.y0 * height),
        right=round(bbox.x1 * width),
        bottom=round(bbox.y1 * height),
    )


def _runs(flags: npt.NDArray[np.bool_]) -> list[tuple[int, int]]:
    """Half-open [start, stop) spans of consecutive True values."""
    padded = np.concatenate(([False], flags, [False]))
    edges = np.flatnonzero(padded[1:] != padded[:-1])
    return [(int(start), int(stop)) for start, stop in zip(edges[::2], edges[1::2], strict=True)]
