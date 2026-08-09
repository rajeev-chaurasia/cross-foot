"""Review crops: the picture a human sees beside a low confidence value.

Correctness never depends on model coordinates. The deterministic path crops the
union of the word boxes that produced the value. The LLM path locates the rows of
the table with a horizontal projection profile and takes the row at the reported
row_position; the model's own bbox refines that band only after it passes the
sanity checks, and is discarded silently otherwise. FULL_PAGE is always available
underneath. Crops are never scored.

The profile is measured against a scan, not a rendering, so it is defended on
four sides: a local ink threshold, because a degraded page has a background that
varies across it; a skew search, because a page a degree off smears every row
into its neighbour; and two agreements before a band is served at all, that the
rows found are the page's one dominant evenly spaced run, and that there are
exactly as many of them as the model said it read. A band that fails any of them
is not narrowed to a guess, it is dropped for the whole page.
"""

from __future__ import annotations

import itertools
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

# A review crop is read in a browser panel, so the longest edge is bounded here.
# A page scanned at 200 dpi is upwards of eight megabytes of PNG, which is detail
# no reviewer sees and bytes every request pays for. Bands are already small; it
# is the full page fallback underneath them that this holds down.
MAX_SERVED_EDGE_PX = 1600

# Rows are found on a fixed height copy of the page. The profile needs the shape
# of the print rather than its detail, and a fixed height keeps every threshold
# below a constant instead of something that drifts with the scan's resolution.
DETECT_HEIGHT_PX = 1100

# Skew is searched over this range, at this step, by taking the angle whose
# profile has the sharpest edges. Beyond a few degrees a page is not a scan of a
# statement, and the profile stops being the right question.
MAX_SKEW_DEG = 3.0
SKEW_STEP_DEG = 0.25

# Ink is thresholded locally rather than globally: on a heavy scan the paper
# itself is darker in places than the print is elsewhere, and one threshold for
# the whole page marks that paper as ink.
INK_BLOCK_PX = 31
INK_BIAS = 8
# Speckle is a pixel or two across; a stroke of print survives this opening.
SPECKLE_KERNEL_PX = 2

# A row of print marks this share of the page width with ink.
ROW_INK_WIDTH_FRACTION = 0.18
# Runs outside this range are speckle or a block of prose, not one line of print.
MIN_ROW_HEIGHT_PX = 3
MAX_ROW_HEIGHT_PX = 40
# The rows of one table are evenly spaced, within this share of their pitch.
ROW_PITCH_TOLERANCE = 0.08
# The rows of one table are the same height, within this share of the mean.
MAX_ROW_HEIGHT_SPREAD = 0.25
# A pitch needs two rows to exist, so a table shorter than this has no shape to
# recognize and never yields a band.
MIN_TABLE_ROWS = 2

# Bands are padded by one row of their own height on each side.
BAND_PAD_ROWS = 1
# A review crop is read by a human on screen, so an exact box is opened out by
# this share of the page's shorter edge on every side. Flush against the value
# there is no context, and the reader cannot tell a total from a line amount.
REVIEW_PAD_FRACTION = 0.012

# Sanity checks on the model's optional bbox, which is a hint and never evidence.
# A box wider than it is tall, by a plausible margin either way, is the only
# shape a row of a statement takes.
MIN_HINT_ASPECT = 1.5
MAX_HINT_ASPECT = 200.0

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
class Region:
    """The pixels to serve and the honest name for how they were found.

    The image travels with the box because a band is cut from a straightened
    copy of the page. Every other kind indexes the page exactly as rasterized.
    """

    image: Image
    box: PixelBox
    kind: CropKind

    def cut(self) -> Image:
        cut: Image = self.image[self.box.top : self.box.bottom, self.box.left : self.box.right]
        return cut


@dataclass(frozen=True, slots=True)
class TableRows:
    """The rows of one table, as found on a straightened copy of the page."""

    skew_degrees: float
    spans: tuple[tuple[int, int], ...]  # top, bottom in page pixels


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
    expected_rows: int | None = None,
    root: Path = DEFAULT_CROPS_ROOT,
) -> Crop:
    """Render one review crop, reusing the cached file when it already exists."""
    path = crop_path(field.doc_id, field.field_id, root=root)
    image = decode_png(page_png)
    region = region_for(field, image, row_position=row_position, expected_rows=expected_rows)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(encode_png(fit_to_served_edge(region.cut())))
    return Crop(path=path, kind=region.kind)


def region_for(
    field: ExtractedField,
    image: Image,
    *,
    row_position: int | None = None,
    expected_rows: int | None = None,
) -> Region:
    """The region to crop for a field the pipeline itself measured."""
    if field.crop_kind is CropKind.EXACT_BBOX and field.bbox is not None:
        exact = _to_pixels(field.bbox, image).intersect(full_page(image))
        if not exact.is_empty():
            return Region(image=image, box=exact, kind=CropKind.EXACT_BBOX)
    hint = None if field.crop_kind is CropKind.EXACT_BBOX else field.bbox
    return _band_or_page(image, row_position=row_position, expected_rows=expected_rows, hint=hint)


def review_region(
    bbox: BBox | None,
    image: Image,
    *,
    row_position: int | None = None,
    expected_rows: int | None = None,
    hint: BBox | None = None,
) -> Region:
    """The region a reviewer sees: the padded value box, its row band, or the page.

    Separate from `region_for` because the two answer different questions. The
    pipeline crops flush to the word boxes it measured; a human needs margin
    around the value, and needs an image even when there are no coordinates at
    all, which is what makes the full page the floor rather than a failure.
    """
    if bbox is not None:
        exact = _to_pixels(bbox, image).intersect(full_page(image))
        if not exact.is_empty():
            padded = _padded(exact, image).intersect(full_page(image))
            return Region(image=image, box=padded, kind=CropKind.EXACT_BBOX)
    return _band_or_page(image, row_position=row_position, expected_rows=expected_rows, hint=hint)


def full_page(image: Image) -> PixelBox:
    height, width = image.shape[:2]
    return PixelBox(left=0, top=0, right=int(width), bottom=int(height))


def fit_to_served_edge(image: Image, *, max_edge: int = MAX_SERVED_EDGE_PX) -> Image:
    """Shrink until the longest edge fits the cap, keeping the aspect ratio."""
    height, width = image.shape[:2]
    longest = max(int(height), int(width))
    if longest <= max_edge:
        return image
    scale = max_edge / longest
    size = (max(1, round(int(width) * scale)), max(1, round(int(height) * scale)))
    resized: Image = cv2.resize(image, size, interpolation=cv2.INTER_AREA).astype(np.uint8)
    return resized


def table_rows(image: Image, expected_rows: int) -> TableRows | None:
    """The rows of the page's table, or None when they cannot be trusted.

    Every check here is a reason to answer None. A band is only ever offered
    when the page holds one dominant run of evenly spaced, equally tall lines of
    print, and that run has exactly as many rows as the model reported reading.
    """
    if expected_rows < MIN_TABLE_ROWS:
        return None
    work, scale = _detection_image(image)
    mask = _ink_mask(work)
    angle = _skew_degrees(mask)
    straight = _rotated(mask, angle) if angle else mask
    run = _table_run(_print_rows(straight), expected_rows)
    if run is None:
        return None
    return TableRows(
        skew_degrees=angle,
        spans=tuple((round(top / scale), round(bottom / scale)) for top, bottom in run),
    )


def row_stripes(image: Image) -> tuple[tuple[int, int], ...]:
    """Every band of print on the page, top to bottom, at full resolution.

    The shape the profile sees, without the agreements that turn it into a
    table. Rows of a table come from `table_rows`; this is the primitive under
    it and the anchor a model's bbox has to overlap to be believed at all.
    """
    work, scale = _detection_image(image)
    return tuple(
        (round(top / scale), round(bottom / scale)) for top, bottom in _print_rows(_ink_mask(work))
    )


def decode_png(png_bytes: bytes) -> Image:
    image = cv2.imdecode(np.frombuffer(png_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("page image is not decodable")
    decoded: Image = image.astype(np.uint8)
    return decoded


def encode_png(image: Image) -> bytes:
    ok, buffer = cv2.imencode(CROP_SUFFIX, image)
    if not ok:
        raise ValueError("crop could not be encoded as png")
    encoded: bytes = buffer.tobytes()
    return encoded


def _band_or_page(
    image: Image, *, row_position: int | None, expected_rows: int | None, hint: BBox | None
) -> Region:
    """A row band when the row can be located, the whole page when it cannot."""
    if row_position is None or expected_rows is None:
        return Region(image=image, box=full_page(image), kind=CropKind.FULL_PAGE)
    rows = table_rows(image, expected_rows)
    if rows is None or not 1 <= row_position <= len(rows.spans):
        return Region(image=image, box=full_page(image), kind=CropKind.FULL_PAGE)
    straight = _rotated(image, rows.skew_degrees) if rows.skew_degrees else image
    top, bottom = rows.spans[row_position - 1]
    pad = (bottom - top) * BAND_PAD_ROWS
    page = full_page(straight)
    band = PixelBox(
        left=0, top=max(0, top - pad), right=page.right, bottom=min(page.bottom, bottom + pad)
    ).intersect(page)
    return Region(image=straight, box=_refined(band, hint, straight, rows), kind=CropKind.ROW_BAND)


def _refined(band: PixelBox, hint: BBox | None, image: Image, rows: TableRows) -> PixelBox:
    """Narrow the band with the model's bbox, but only if the bbox survives.

    Inside the page, shaped like a row, and overlapping a row that was actually
    found. A hint that fails any of the three is dropped without comment: it is
    a refinement of a band the page already justified, never evidence of its own.
    """
    if hint is None:
        return band
    hinted = _rotate_box(_to_pixels(hint, image), image, rows.skew_degrees)
    if hinted.is_empty() or hinted != hinted.intersect(full_page(image)):
        return band
    aspect = (hinted.right - hinted.left) / (hinted.bottom - hinted.top)
    if not MIN_HINT_ASPECT <= aspect <= MAX_HINT_ASPECT:
        return band
    if not any(hinted.top < bottom and top < hinted.bottom for top, bottom in rows.spans):
        return band
    narrowed = band.intersect(hinted)
    return band if narrowed.is_empty() else narrowed


def _detection_image(image: Image) -> tuple[Image, float]:
    """A grayscale page at the detection height, and the scale that produced it."""
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
    height, width = gray.shape[:2]
    scale = DETECT_HEIGHT_PX / int(height)
    size = (max(1, round(int(width) * scale)), DETECT_HEIGHT_PX)
    work: Image = cv2.resize(gray, size, interpolation=cv2.INTER_AREA).astype(np.uint8)
    return work, scale


def _ink_mask(gray: Image) -> Image:
    """Ink as white on black, thresholded against the local page rather than the page."""
    binary = cv2.adaptiveThreshold(
        cv2.medianBlur(gray, 3),
        255,
        cv2.ADAPTIVE_THRESH_MEAN_C,
        cv2.THRESH_BINARY_INV,
        INK_BLOCK_PX,
        INK_BIAS,
    )
    speckle = np.ones((SPECKLE_KERNEL_PX, SPECKLE_KERNEL_PX), np.uint8)
    mask: Image = cv2.morphologyEx(binary, cv2.MORPH_OPEN, speckle).astype(np.uint8)
    return mask


def _skew_degrees(mask: Image) -> float:
    """The angle whose row profile has the sharpest edges, which is the upright one.

    A row of print spread across a tilted page raises the profile everywhere and
    peaks nowhere, so the squared difference of the profile is largest exactly
    when the rows line up with the pixel grid.
    """
    steps = round(MAX_SKEW_DEG / SKEW_STEP_DEG)
    best, best_score = 0.0, -1.0
    for step in range(-steps, steps + 1):
        angle = step * SKEW_STEP_DEG
        profile = (_rotated(mask, angle) if angle else mask).sum(axis=1, dtype=np.float64)
        score = float(np.square(np.diff(profile)).sum())
        if score > best_score:
            best, best_score = angle, score
    return best


def _rotated(image: Image, degrees: float) -> Image:
    """The image turned about its centre, with the paper's own white outside it."""
    height, width = image.shape[:2]
    matrix = cv2.getRotationMatrix2D((int(width) / 2, int(height) / 2), degrees, 1.0)
    border = 0 if image.ndim == 2 else (255, 255, 255)
    rotated: Image = cv2.warpAffine(
        image, matrix, (int(width), int(height)), borderValue=border
    ).astype(np.uint8)
    return rotated


def _rotate_box(box: PixelBox, image: Image, degrees: float) -> PixelBox:
    """The box's corners turned with the page, as the smallest upright box holding them."""
    if not degrees:
        return box
    height, width = image.shape[:2]
    matrix = cv2.getRotationMatrix2D((int(width) / 2, int(height) / 2), degrees, 1.0)
    corners = np.array(
        [
            [box.left, box.top, 1.0],
            [box.right, box.top, 1.0],
            [box.left, box.bottom, 1.0],
            [box.right, box.bottom, 1.0],
        ]
    )
    moved = corners @ np.asarray(matrix, dtype=np.float64).T
    return PixelBox(
        left=int(moved[:, 0].min()),
        top=int(moved[:, 1].min()),
        right=round(float(moved[:, 0].max())),
        bottom=round(float(moved[:, 1].max())),
    )


def _print_rows(mask: Image) -> list[tuple[int, int]]:
    """Runs of rows carrying enough ink to be a line of print, at detection scale."""
    ink_per_row = np.count_nonzero(mask, axis=1)
    inked = ink_per_row >= max(1, int(mask.shape[1] * ROW_INK_WIDTH_FRACTION))
    return [
        (start, stop)
        for start, stop in _runs(inked)
        if MIN_ROW_HEIGHT_PX <= stop - start <= MAX_ROW_HEIGHT_PX
    ]


def _table_run(rows: list[tuple[int, int]], expected_rows: int) -> list[tuple[int, int]] | None:
    """The page's one dominant evenly spaced run, if it is the size promised."""
    runs = _pitch_runs(rows)
    if not runs:
        return None
    longest = max(len(run) for run in runs)
    candidates = [run for run in runs if len(run) == longest]
    # Two runs of equal length leave nothing to choose between, and a run that is
    # not the size the model reported is not the table the model read.
    if len(candidates) != 1 or longest != expected_rows:
        return None
    run = candidates[0]
    heights = np.array([bottom - top for top, bottom in run], dtype=np.float64)
    mean = float(heights.mean())
    if mean <= 0 or float(heights.std()) / mean > MAX_ROW_HEIGHT_SPREAD:
        return None
    return run


def _pitch_runs(rows: list[tuple[int, int]]) -> list[list[tuple[int, int]]]:
    """Maximal runs of rows whose centre to centre pitch stays consistent."""
    if len(rows) < MIN_TABLE_ROWS:
        return []
    centres = [(top + bottom) / 2 for top, bottom in rows]
    gaps = [after - before for before, after in itertools.pairwise(centres)]
    runs: list[list[tuple[int, int]]] = []
    start = 0
    while start < len(gaps):
        end, pitch, seen = start, gaps[start], 1
        while end + 1 < len(gaps) and abs(gaps[end + 1] - pitch) <= ROW_PITCH_TOLERANCE * pitch:
            seen += 1
            pitch += (gaps[end + 1] - pitch) / seen
            end += 1
        runs.append(rows[start : end + 2])
        start = end + 1
    return runs


def _padded(box: PixelBox, image: Image) -> PixelBox:
    """Open a box out by REVIEW_PAD_FRACTION of the page's shorter edge, isotropically."""
    height, width = image.shape[:2]
    pad = max(1, round(min(int(height), int(width)) * REVIEW_PAD_FRACTION))
    return PixelBox(
        left=box.left - pad, top=box.top - pad, right=box.right + pad, bottom=box.bottom + pad
    )


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
