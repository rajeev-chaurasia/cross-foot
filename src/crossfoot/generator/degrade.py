"""Scan degradation: rasterize a PDF, run Augraphy, reassemble image-only pages.

The output PDF carries no text layer at all; a contract test asserts that
pdfplumber extracts zero characters from scanned tiers.
"""

import random
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import cv2
import img2pdf
import numpy as np
from augraphy import (
    AugraphyPipeline,
    BadPhotoCopy,
    DirtyRollers,
    Faxify,
    Geometric,
    InkBleed,
    LightingGradient,
    SubtleNoise,
)
from numba import njit

from crossfoot import pdfium

SCAN_LIGHT_PROFILE = "scan_light"
SCAN_HEAVY_PROFILE = "scan_heavy"

SCAN_DPI = 200
PDF_POINTS_PER_INCH = 72
JPEG_QUALITY = 85
LIGHT_MAX_ROTATION_DEGREES = 1
HEAVY_MAX_ROTATION_DEGREES = 3
LIGHT_NOISE_RANGE = 8
HEAVY_NOISE_RANGE = 18
NP_SEED_MODULUS = 2**32  # numpy accepts 32-bit seeds only
# Fixed document dates keep output bytes reproducible; real timestamps never
# belong in dataset artifacts.
IMG2PDF_FIXED_DATE = datetime(2026, 1, 1, tzinfo=UTC)

PipelineBuilder = Callable[[random.Random], Any]


@njit(cache=True)
def _seed_numba_rng(seed: int) -> None:
    """Seed the generator state numba keeps for compiled code.

    DirtyRollers draws from `random` inside an njit function, and numba's
    nopython `random` and `np.random` are separate generators that the
    interpreter's seed calls cannot reach. Only a call from inside compiled
    code seeds them, and numba exposes no way to read that state back, so it
    cannot be saved and restored the way the interpreter's state is.
    """
    random.seed(seed)
    np.random.seed(seed)


def _light_pipeline(rng: random.Random) -> Any:
    """Slight skew, an uneven brightness gradient, and mild sensor noise."""
    del rng  # scan_light has no alternative augmentations to pick between
    post_phase = [
        Geometric(
            rotate_range=(-LIGHT_MAX_ROTATION_DEGREES, LIGHT_MAX_ROTATION_DEGREES),
            p=1,
        ),
        LightingGradient(max_brightness=255, min_brightness=64, transparency=0.6, p=1),
        SubtleNoise(subtle_range=LIGHT_NOISE_RANGE, p=1),
    ]
    return AugraphyPipeline(ink_phase=[], paper_phase=[], post_phase=post_phase)


def _heavy_pipeline(rng: random.Random) -> Any:
    """Harsh skew, ink bleed or bad photocopy, dirty rollers or fax dithering."""
    degrader = (
        InkBleed(intensity_range=(0.5, 0.8), kernel_size=(7, 7), p=1)
        if rng.random() < 0.5
        else BadPhotoCopy(noise_type=1, blur_noise=1, p=1)
    )
    transport = (
        DirtyRollers(line_width_range=(6, 16), p=1)
        if rng.random() < 0.5
        else Faxify(monochrome=1, monochrome_method="threshold_otsu", halftone=0, p=1)
    )
    post_phase = [
        Geometric(
            rotate_range=(-HEAVY_MAX_ROTATION_DEGREES, HEAVY_MAX_ROTATION_DEGREES),
            p=1,
        ),
        degrader,
        transport,
        SubtleNoise(subtle_range=HEAVY_NOISE_RANGE, p=1),
    ]
    return AugraphyPipeline(ink_phase=[], paper_phase=[], post_phase=post_phase)


PROFILE_BUILDERS: dict[str, PipelineBuilder] = {
    SCAN_LIGHT_PROFILE: _light_pipeline,
    SCAN_HEAVY_PROFILE: _heavy_pipeline,
}


def degrade_to_scan(pdf_path: Path, profile: str, seed: int) -> None:
    """Overwrite a born-digital PDF with an image-only scan of itself."""
    try:
        builder = PROFILE_BUILDERS[profile]
    except KeyError as error:
        raise ValueError(f"unknown scan profile {profile!r}") from error
    pipeline = builder(random.Random(seed))

    # Augraphy draws from the global random and numpy generators; seed both per
    # page for determinism, then restore whatever state the caller had. Numba's
    # own generators are seeded alongside them but cannot be restored.
    python_state = random.getstate()
    numpy_state = np.random.get_state()
    page_images: list[bytes] = []
    try:
        # PDFium is not thread safe, so the document scope holds the process
        # wide lock. Generation is single threaded, so the lock is uncontended
        # and the degrading stays inside the scope, where `to_numpy` still has
        # the bitmap PDFium rendered it into.
        with pdfium.open_document(pdf_path) as document:
            for index in range(len(document)):
                bitmap = document[index].render(scale=SCAN_DPI / PDF_POINTS_PER_INCH)
                image = bitmap.to_numpy()
                page_seed = (seed + index) % NP_SEED_MODULUS
                # All three calls are load bearing. The first two seed the
                # interpreter's generators, which reach every augmentation
                # running as Python; the third reaches the ones running as
                # compiled code, and nothing called from here can replace it.
                random.seed(seed + index)
                np.random.seed(page_seed)
                _seed_numba_rng(page_seed)
                augmented = np.clip(pipeline(image), 0, 255).astype(np.uint8)
                success, encoded = cv2.imencode(
                    ".jpg", augmented, [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_QUALITY]
                )
                if not success:
                    raise RuntimeError(f"jpeg encoding failed for page {index} of {pdf_path}")
                page_images.append(encoded.tobytes())
    finally:
        random.setstate(python_state)
        np.random.set_state(numpy_state)

    # The internal engine plus fixed dates yields byte-stable output; the
    # pikepdf engine derives the document /ID from the wall clock.
    pdf_bytes = img2pdf.convert(
        page_images,
        layout_fun=img2pdf.get_fixed_dpi_layout_fun((SCAN_DPI, SCAN_DPI)),
        creationdate=IMG2PDF_FIXED_DATE,
        moddate=IMG2PDF_FIXED_DATE,
        engine=img2pdf.Engine.internal,
    )
    pdf_path.write_bytes(pdf_bytes)
