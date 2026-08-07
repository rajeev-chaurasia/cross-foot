"""Review crops: exact boxes, row bands, and the fallback underneath both."""

from pathlib import Path

import numpy as np

from crossfoot.constants import CropKind, FieldFamily, FieldName, FieldSource, QualityTier
from crossfoot.extraction import crops
from crossfoot.models.extraction import BBox, ExtractedField, FieldSignals

WIDTH = 200
HEIGHT = 120
# Three inked bands with clear white gutters between them.
STRIPES = ((10, 20), (50, 60), (90, 100))


def _page() -> crops.Image:
    image = np.full((HEIGHT, WIDTH, 3), 255, dtype=np.uint8)
    for top, bottom in STRIPES:
        image[top:bottom, 20:180] = 0
    return image


def _field(**kwargs: object) -> ExtractedField:
    defaults: dict[str, object] = {
        "field_id": "fld-doc-x-0001-line_amount",
        "doc_id": "doc-x",
        "name": FieldName.LINE_AMOUNT,
        "family": FieldFamily.AMOUNT,
        "source": FieldSource.LLM_VISION,
        "signals": FieldSignals(quality_tier=QualityTier.SCAN_LIGHT),
    }
    return ExtractedField.model_validate(defaults | kwargs)


def test_projection_profile_finds_every_printed_band() -> None:
    assert crops.row_stripes(_page()) == STRIPES


def test_row_band_takes_the_stripe_at_the_row_position() -> None:
    band = crops.row_band(_page(), 2)
    assert band is not None
    assert band.top < STRIPES[1][0] and band.bottom > STRIPES[1][1]
    assert band.top > STRIPES[0][1]  # padding stops short of the row above


def test_row_band_is_none_past_the_last_stripe() -> None:
    assert crops.row_band(_page(), len(STRIPES) + 1) is None


def test_deterministic_field_crops_its_own_word_boxes() -> None:
    field = _field(
        crop_kind=CropKind.EXACT_BBOX,
        bbox=BBox(page=0, x0=0.1, y0=0.4, x1=0.5, y1=0.55),
        source=FieldSource.DETERMINISTIC,
    )
    box, kind = crops.region_for(field, _page())
    assert kind is CropKind.EXACT_BBOX
    assert (box.left, box.right) == (20, 100)


def test_llm_field_without_a_row_position_falls_back_to_the_full_page() -> None:
    box, kind = crops.region_for(_field(), _page())
    assert kind is CropKind.FULL_PAGE
    assert (box.left, box.top, box.right, box.bottom) == (0, 0, WIDTH, HEIGHT)


def test_a_plausible_bbox_narrows_the_band() -> None:
    # Overlaps the second stripe, so it is trusted to sharpen the band.
    field = _field(bbox=BBox(page=0, x0=0.1, y0=48 / HEIGHT, x1=0.9, y1=58 / HEIGHT))
    box, kind = crops.region_for(field, _page(), row_position=2)
    plain, _ = crops.region_for(_field(), _page(), row_position=2)
    assert kind is CropKind.ROW_BAND
    assert box.bottom - box.top < plain.bottom - plain.top


def test_a_bbox_that_overlaps_no_ink_is_discarded_silently() -> None:
    field = _field(bbox=BBox(page=0, x0=0.1, y0=0.6, x1=0.9, y1=0.68))
    box, kind = crops.region_for(field, _page(), row_position=2)
    plain, _ = crops.region_for(_field(), _page(), row_position=2)
    assert kind is CropKind.ROW_BAND
    assert box == plain


def test_crops_render_lazily_and_are_cached(tmp_path: Path) -> None:
    field = _field()
    png = crops.encode_png(_page())
    first = crops.render_crop(field, png, row_position=2, root=tmp_path)
    assert first.path == tmp_path / "doc-x" / f"{field.field_id}.png"
    assert first.path.exists()

    written = first.path.stat().st_mtime_ns
    first.path.write_bytes(b"sentinel")  # a rerun must not re-encode over this
    second = crops.render_crop(field, png, row_position=2, root=tmp_path)
    assert second.path.read_bytes() == b"sentinel"
    assert second.kind is first.kind
    assert written  # the first render really did write the file
