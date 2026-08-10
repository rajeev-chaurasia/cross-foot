"""Review crops: exact boxes, row bands, and the fallback underneath both.

The synthetic page here is a table and nothing else, so a band that lands on the
wrong row is visible as an arithmetic fact rather than a judgement call. What the
band has to survive on a real scan is checked at the API boundary, in
tests/unit/test_api_crops_render.py, against a rasterized document.
"""

from pathlib import Path

import numpy as np

from crossfoot.constants import CropKind, ExtractionRoute, FieldFamily, FieldName, FieldSource
from crossfoot.extraction import crops
from crossfoot.models.extraction import BBox, ExtractedField, FieldSignals

WIDTH = 900
HEIGHT = 1200
# A table of six evenly spaced rows, printed across most of the width, plus a
# title far above it that belongs to no run.
ROW_TOP = 400
ROW_PITCH = 100
ROW_HEIGHT = 30
ROW_COUNT = 6
TITLE = (80, 110)


def _page(rows: int = ROW_COUNT) -> crops.Image:
    image = np.full((HEIGHT, WIDTH, 3), 255, dtype=np.uint8)
    image[TITLE[0] : TITLE[1], 100:400] = 0
    for index in range(rows):
        top = ROW_TOP + index * ROW_PITCH
        image[top : top + ROW_HEIGHT, 60 : WIDTH - 60] = 0
    return image


def _row_centre(row_position: int) -> float:
    return ROW_TOP + (row_position - 1) * ROW_PITCH + ROW_HEIGHT / 2


def _field(**kwargs: object) -> ExtractedField:
    defaults: dict[str, object] = {
        "field_id": "fld-doc-x-0001-line_amount",
        "doc_id": "doc-x",
        "name": FieldName.LINE_AMOUNT,
        "family": FieldFamily.AMOUNT,
        "source": FieldSource.LLM_VISION,
        "signals": FieldSignals(route=ExtractionRoute.SCANNED_PDF),
    }
    return ExtractedField.model_validate(defaults | kwargs)


# Finding the rows.


def test_the_table_is_found_when_it_holds_the_rows_the_model_reported() -> None:
    rows = crops.table_rows(_page(), ROW_COUNT)
    assert rows is not None
    assert len(rows.spans) == ROW_COUNT
    for index, (top, bottom) in enumerate(rows.spans, start=1):
        assert abs((top + bottom) / 2 - _row_centre(index)) <= ROW_HEIGHT


def test_a_table_of_a_different_size_than_reported_is_refused() -> None:
    # The page prints six rows; a model that reported five was reading something
    # else, and there is nothing here to index with its row numbers.
    assert crops.table_rows(_page(), ROW_COUNT - 1) is None


def test_a_page_with_no_table_yields_no_rows() -> None:
    blank = np.full((HEIGHT, WIDTH, 3), 255, dtype=np.uint8)
    assert crops.table_rows(blank, ROW_COUNT) is None


def test_rows_of_uneven_height_are_refused() -> None:
    image = _page()
    # One row printed twice as tall as its neighbours: evenly spaced, but not the
    # same line of print repeated, so the run is not a table.
    top = ROW_TOP + 2 * ROW_PITCH
    image[top : top + ROW_HEIGHT * 2, 60 : WIDTH - 60] = 0
    assert crops.table_rows(image, ROW_COUNT) is None


# Choosing a region.


def test_a_vision_line_gets_the_band_of_its_own_row() -> None:
    region = crops.review_region(None, _page(), row_position=4, expected_rows=ROW_COUNT)
    assert region.kind is CropKind.ROW_BAND
    centre = (region.box.top + region.box.bottom) / 2
    assert abs(centre - _row_centre(4)) <= ROW_HEIGHT
    # A band, not a page: one row plus a row of padding either side.
    assert region.box.bottom - region.box.top < HEIGHT * 0.2


def test_a_vision_header_field_keeps_the_whole_page() -> None:
    region = crops.review_region(None, _page(), row_position=None, expected_rows=ROW_COUNT)
    assert region.kind is CropKind.FULL_PAGE
    assert region.box == crops.full_page(_page())


def test_a_row_beyond_the_table_keeps_the_whole_page() -> None:
    region = crops.review_region(None, _page(), row_position=ROW_COUNT + 1, expected_rows=ROW_COUNT)
    assert region.kind is CropKind.FULL_PAGE


def test_deterministic_field_crops_its_own_word_boxes() -> None:
    field = _field(
        crop_kind=CropKind.EXACT_BBOX,
        bbox=BBox(page=0, x0=0.1, y0=0.4, x1=0.5, y1=0.55),
        source=FieldSource.DETERMINISTIC,
    )
    region = crops.region_for(field, _page())
    assert region.kind is CropKind.EXACT_BBOX
    assert (region.box.left, region.box.right) == (90, 450)


def test_llm_field_without_a_row_position_falls_back_to_the_full_page() -> None:
    region = crops.region_for(_field(), _page())
    assert region.kind is CropKind.FULL_PAGE
    assert (region.box.left, region.box.top) == (0, 0)
    assert (region.box.right, region.box.bottom) == (WIDTH, HEIGHT)


# The model's bbox, which is a hint and never evidence.


def test_a_plausible_bbox_narrows_the_band() -> None:
    # Overlaps the fourth row and is shaped like one, so it is trusted to sharpen.
    hint = BBox(
        page=0, x0=0.2, y0=_row_centre(4) / HEIGHT - 0.01, x1=0.8, y1=_row_centre(4) / HEIGHT + 0.01
    )
    narrowed = crops.review_region(
        None, _page(), row_position=4, expected_rows=ROW_COUNT, hint=hint
    )
    plain = crops.review_region(None, _page(), row_position=4, expected_rows=ROW_COUNT)
    assert narrowed.kind is CropKind.ROW_BAND
    assert narrowed.box.right - narrowed.box.left < plain.box.right - plain.box.left


def test_a_bbox_that_overlaps_no_ink_is_discarded_silently() -> None:
    hint = BBox(page=0, x0=0.2, y0=0.02, x1=0.8, y1=0.04)
    region = crops.review_region(None, _page(), row_position=4, expected_rows=ROW_COUNT, hint=hint)
    plain = crops.review_region(None, _page(), row_position=4, expected_rows=ROW_COUNT)
    assert region.kind is CropKind.ROW_BAND
    assert region.box == plain.box


def test_a_bbox_shaped_like_a_column_is_discarded_silently() -> None:
    # Tall and narrow: it overlaps the row, but no row of a statement is shaped
    # like that, so the aspect check drops it.
    hint = BBox(page=0, x0=0.40, y0=0.30, x1=0.42, y1=0.60)
    region = crops.review_region(None, _page(), row_position=4, expected_rows=ROW_COUNT, hint=hint)
    plain = crops.review_region(None, _page(), row_position=4, expected_rows=ROW_COUNT)
    assert region.kind is CropKind.ROW_BAND
    assert region.box == plain.box


def test_a_bbox_reaching_outside_the_page_is_discarded_silently() -> None:
    hint = BBox(
        page=0, x0=0.2, y0=_row_centre(4) / HEIGHT - 0.01, x1=1.6, y1=_row_centre(4) / HEIGHT + 0.01
    )
    region = crops.review_region(None, _page(), row_position=4, expected_rows=ROW_COUNT, hint=hint)
    plain = crops.review_region(None, _page(), row_position=4, expected_rows=ROW_COUNT)
    assert region.kind is CropKind.ROW_BAND
    assert region.box == plain.box


# The size cap.


def test_a_crop_larger_than_the_cap_is_shrunk_with_its_aspect_kept() -> None:
    tall = np.full((crops.MAX_SERVED_EDGE_PX * 2, crops.MAX_SERVED_EDGE_PX, 3), 255, dtype=np.uint8)
    fitted = crops.fit_to_served_edge(tall)
    assert max(fitted.shape[:2]) == crops.MAX_SERVED_EDGE_PX
    assert fitted.shape[0] == fitted.shape[1] * 2


def test_a_crop_inside_the_cap_is_left_exactly_as_it_was() -> None:
    small = _page()
    assert crops.fit_to_served_edge(small) is small


# Caching.


def test_crops_render_lazily_and_are_cached(tmp_path: Path) -> None:
    field = _field()
    png = crops.encode_png(_page())
    first = crops.render_crop(field, png, row_position=4, expected_rows=ROW_COUNT, root=tmp_path)
    assert first.path == tmp_path / "doc-x" / f"{field.field_id}.png"
    assert first.kind is CropKind.ROW_BAND

    # The first render wrote a decodable crop, not an empty file to fill later.
    assert crops.decode_png(first.path.read_bytes()).size > 0

    first.path.write_bytes(b"sentinel")  # a rerun must not re-encode over this
    written = first.path.stat().st_mtime_ns
    second = crops.render_crop(field, png, row_position=4, expected_rows=ROW_COUNT, root=tmp_path)
    # Untouched by both of the things a rewrite moves: the timestamp and the bytes.
    assert second.path.stat().st_mtime_ns == written
    assert second.path.read_bytes() == b"sentinel"
    # The cached answer still names the region, which is the part of the render
    # a cache hit could skip computing and get wrong.
    assert second.kind is CropKind.ROW_BAND
