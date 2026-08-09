"""Crops render on the first request, from the rows the queue is already read from.

The screen this project is judged on puts a flagged value next to the pixels it
came from, so a field the database holds always yields an image: the exact box
when there is one, the band of the row a vision field was read from when that row
can be located, and the whole page underneath everything else. A 404 means the
pair names no field, never that nothing has been rendered yet.

Every served image is bounded by MAX_SERVED_EDGE_PX. A page at review dpi is
megabytes of PNG that no browser panel can show, so the cap is checked here
against every kind of crop the route can produce, the full page included.

Offline throughout. The source document is the hand-assembled PDF fixture and
the corrupted one is the generator's own artifact, so nothing here rasterizes a
real scan or reaches the network.

The hostile path forms are restated from tests/contract/test_api_crops_security.py
rather than imported: that file is the authority on containment and stays
untouched, and what is checked here is the new half of the claim, that a segment
which fails containment never reaches the renderer either.
"""

import sqlite3
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import pypdfium2
import pytest
from fastapi.testclient import TestClient
from pdf_fixtures import (
    FONT_SIZE,
    PAGE_HEIGHT,
    PAGE_WIDTH,
    TRUTH_DOC,
    minimal_pdf,
    statement_items,
)

from crossfoot.api import create_app, crop_render
from crossfoot.api.dto import CropUnavailableReason
from crossfoot.api.routes import crops as crops_route
from crossfoot.constants import (
    CorruptionKind,
    CropKind,
    FieldFamily,
    FieldName,
    FieldSource,
    QualityTier,
    ReviewStatus,
)
from crossfoot.db import connect
from crossfoot.db.schema import ensure_schema
from crossfoot.extraction import crops
from crossfoot.generator.corrupt import write_corrupted
from crossfoot.models.extraction import FieldSignals

DOC = "doc-a"
BROKEN_DOC = "doc-broken"
# A scanned statement: no coordinates anywhere, only the row each value was read
# from. Its twin prints the same page but is seeded with a row count that does
# not match it, which is how a page whose table cannot be trusted behaves.
VISION_DOC = "doc-vision"
MISCOUNTED_DOC = "doc-miscounted"

EXACT_FIELD = "fld-a-0001-line_amount"
FULL_PAGE_FIELD = "fld-a-0002-statement_number"
NO_COORDS_FIELD = "fld-a-0003-line_amount"
BROKEN_FIELD = "fld-broken-0001-line_amount"
UNKNOWN_FIELD = "fld-a-9999"

# The amount cell on the first printed line of the fixture statement, as a share
# of the page. Small enough that padding cannot grow it back to the full page.
AMOUNT_BBOX = (0.78, 0.25, 0.92, 0.28)

# The vision fixture's table: rows of one line of print, evenly spaced, with the
# first row's baseline this far up the page. Laid out here rather than imported
# so the row a band is checked against is a number this file chose.
VISION_ROWS = 6
VISION_FIRST_BASELINE = 600
VISION_ROW_PITCH = 24
# The row a band is asked for, and one far from it, so two bands can be compared.
BAND_ROW = 3
OTHER_BAND_ROW = 6
# A row carrying stored coordinates shaped like a column: overlapping ink, but
# not the shape of a row, so the sanity check drops the hint and keeps the band.
HINTED_ROW = 4
COLUMN_SHAPED_BBOX = (0.40, 0.20, 0.42, 0.60)

# The page the fixture PDF declares, in the pixels the renderer produces from it.
SCALE = crop_render.REVIEW_CROP_DPI / crop_render.PDF_POINTS_PER_INCH
PAGE_PIXELS = (round(PAGE_WIDTH * SCALE), round(PAGE_HEIGHT * SCALE))
# A band is one row of print with a row of padding either side, so it is a small
# share of the page. Anything near the page height is the fallback wearing the
# wrong name.
MAX_BAND_HEIGHT_FRACTION = 0.10

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
# Planted outside the crop root, named so a naive join really would serve it.
SECRET_BYTES = PNG_MAGIC + b"CROSSFOOT-UNIT-SECRET-DO-NOT-SERVE"

HOSTILE_URLS = (
    "/api/crops/%2E%2E/secret.png",
    "/api/crops/..%5Coutside/secret.png",
    "/api/crops/doc-a/..%5C..%5Coutside%5Csecret.png",
    "/api/crops/C%3A/secret.png",
    "/api/crops/%5C%5C.%5CC%3A/secret.png",
    "/api/crops/%5Cetc%5Cpasswd/secret.png",
    "/api/crops/%2E%2E%2F%2E%2E%2Foutside/secret.png",
    "/api/crops/%2F%2F.%2FC%3A/secret.png",
    "/api/crops/%2Fetc%2Fpasswd/secret.png",
    "/api/crops/doc-a/%2E%2E%2F%2E%2E%2Foutside%2Fsecret.png",
)

_INSERT_DOCUMENT = """
INSERT INTO documents (doc_id, file_path, doc_type, quality_tier, route, split, error_kind)
VALUES (:doc_id, :file_path, NULL, :quality_tier, :route, NULL, NULL)
"""

_INSERT_FIELD = """
INSERT INTO fields (
    field_id, doc_id, line_no, name, family, raw_text, value, value_cents, value_date,
    source, crop_kind, page, x0, y0, x1, y1, confidence, status, signals
) VALUES (
    :field_id, :doc_id, 1, :name, :family, '$1,234.56', '1234.56', 123456, NULL,
    :source, :crop_kind, :page, :x0, :y0, :x1, :y1, 0.2, :status, :signals
)
"""

# The same insert with the line number spelled out, for the vision fixtures where
# which row a field sits on is the whole point.
_INSERT_LINE_FIELD = """
INSERT INTO fields (
    field_id, doc_id, line_no, name, family, raw_text, value, value_cents, value_date,
    source, crop_kind, page, x0, y0, x1, y1, confidence, status, signals
) VALUES (
    :field_id, :doc_id, :line_no, :name, :family, '$1,234.56', '1234.56', 123456, NULL,
    :source, :crop_kind, :page, :x0, :y0, :x1, :y1, 0.2, :status, :signals
)
"""

_CROP_KIND = "SELECT crop_kind FROM fields WHERE field_id = :field_id"


def vision_items() -> list[tuple[float, float, int, str]]:
    """A statement whose table is the one evenly spaced run of print on the page."""
    items: list[tuple[float, float, int, str]] = [
        (180, 720, 12, "MERIDIAN PARTS STATEMENT"),
        (50, 660, FONT_SIZE, "Statement PS-2026-07-001"),
        (50, 630, FONT_SIZE, "Date"),
        (150, 630, FONT_SIZE, "Invoice"),
        (260, 630, FONT_SIZE, "Description"),
        (470, 630, FONT_SIZE, "Amount"),
    ]
    for index in range(VISION_ROWS):
        baseline = VISION_FIRST_BASELINE - index * VISION_ROW_PITCH
        items.extend(
            [
                (50, baseline, FONT_SIZE, f"07/{10 + index:02d}/2026"),
                (150, baseline, FONT_SIZE, f"INV{1000000 + index * 7717}"),
                (260, baseline, FONT_SIZE, "Maintenance parts restock"),
                (470, baseline, FONT_SIZE, f"${1000 + index * 137}.{index * 11:02d}"),
            ]
        )
    items.extend(
        [
            (360, 380, FONT_SIZE, "Previous balance"),
            (470, 380, FONT_SIZE, "$200.00"),
            (360, 350, FONT_SIZE, "Total due"),
            (470, 350, FONT_SIZE, "$9,450.00"),
        ]
    )
    return items


def _vision_field(doc_id: str, line_no: int | None, name: FieldName) -> str:
    position = "header" if line_no is None else f"{line_no:04d}"
    return f"fld-{doc_id}-{position}-{name.value}"


def _seed_vision(connection: sqlite3.Connection, signals: str) -> None:
    """A vision document numbered to match its page, and one numbered to miss it."""
    for doc_id, rows in ((VISION_DOC, VISION_ROWS), (MISCOUNTED_DOC, VISION_ROWS - 1)):
        connection.execute(
            _INSERT_DOCUMENT,
            {
                "doc_id": doc_id,
                "file_path": f"files/{doc_id}.pdf",
                "quality_tier": QualityTier.SCAN_LIGHT.value,
                "route": "scanned_pdf",
            },
        )
        for line_no in range(1, rows + 1):
            hinted = doc_id == VISION_DOC and line_no == HINTED_ROW
            x0, y0, x1, y1 = COLUMN_SHAPED_BBOX
            connection.execute(
                _INSERT_LINE_FIELD,
                {
                    "field_id": _vision_field(doc_id, line_no, FieldName.LINE_AMOUNT),
                    "doc_id": doc_id,
                    "line_no": line_no,
                    "name": FieldName.LINE_AMOUNT.value,
                    "family": FieldFamily.AMOUNT.value,
                    "source": FieldSource.LLM_VISION.value,
                    "crop_kind": CropKind.FULL_PAGE.value,
                    "page": 0 if hinted else None,
                    "x0": x0 if hinted else None,
                    "y0": y0 if hinted else None,
                    "x1": x1 if hinted else None,
                    "y1": y1 if hinted else None,
                    "status": ReviewStatus.NEEDS_REVIEW.value,
                    "signals": signals,
                },
            )
        connection.execute(
            _INSERT_LINE_FIELD,
            {
                "field_id": _vision_field(doc_id, None, FieldName.STATEMENT_NUMBER),
                "doc_id": doc_id,
                "line_no": None,
                "name": FieldName.STATEMENT_NUMBER.value,
                "family": FieldFamily.REFERENCE.value,
                "source": FieldSource.LLM_VISION.value,
                "crop_kind": CropKind.FULL_PAGE.value,
                "page": None,
                "x0": None,
                "y0": None,
                "x1": None,
                "y1": None,
                "status": ReviewStatus.NEEDS_REVIEW.value,
                "signals": signals,
            },
        )


def _seed(connection: sqlite3.Connection) -> None:
    """Two documents: one readable, one corrupted, with a field of each crop kind."""
    signals = FieldSignals(quality_tier=QualityTier.CLEAN_DIGITAL).model_dump_json()
    for doc_id in (DOC, BROKEN_DOC):
        connection.execute(
            _INSERT_DOCUMENT,
            {
                "doc_id": doc_id,
                "file_path": f"files/{doc_id}.pdf",
                "quality_tier": QualityTier.CLEAN_DIGITAL.value,
                "route": "digital_pdf",
            },
        )
    x0, y0, x1, y1 = AMOUNT_BBOX
    boxes: tuple[tuple[str, str, CropKind, int | None, tuple[float | None, ...]], ...] = (
        (EXACT_FIELD, DOC, CropKind.EXACT_BBOX, 0, (x0, y0, x1, y1)),
        (FULL_PAGE_FIELD, DOC, CropKind.FULL_PAGE, None, (None, None, None, None)),
        # An exact_bbox row that lost its coordinates: still a picture, not a 404.
        (NO_COORDS_FIELD, DOC, CropKind.EXACT_BBOX, None, (None, None, None, None)),
        (BROKEN_FIELD, BROKEN_DOC, CropKind.FULL_PAGE, None, (None, None, None, None)),
    )
    for field_id, doc_id, crop_kind, page, corners in boxes:
        connection.execute(
            _INSERT_FIELD,
            {
                "field_id": field_id,
                "doc_id": doc_id,
                "name": FieldName.LINE_AMOUNT.value,
                "family": FieldFamily.AMOUNT.value,
                "source": FieldSource.DETERMINISTIC.value,
                "crop_kind": crop_kind.value,
                "page": page,
                "x0": corners[0],
                "y0": corners[1],
                "x1": corners[2],
                "y1": corners[3],
                "status": ReviewStatus.NEEDS_REVIEW.value,
                "signals": signals,
            },
        )
    _seed_vision(connection, signals)


@pytest.fixture
def dataset_dir(tmp_path: Path) -> Path:
    """A dataset holding readable statements and one file of binary junk."""
    dataset = tmp_path / "dataset"
    files = dataset / "files"
    files.mkdir(parents=True)
    (files / f"{DOC}.pdf").write_bytes(minimal_pdf(statement_items(TRUTH_DOC)))
    for doc_id in (VISION_DOC, MISCOUNTED_DOC):
        (files / f"{doc_id}.pdf").write_bytes(minimal_pdf(vision_items()))
    write_corrupted(CorruptionKind.BINARY_JUNK, 7, files / f"{BROKEN_DOC}.pdf")
    return dataset


@pytest.fixture
def crops_root(tmp_path: Path) -> Path:
    """An empty crop root, with a secret planted where a naive join would find it."""
    root = tmp_path / "crops"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.png").write_bytes(SECRET_BYTES)
    (tmp_path / "secret.png").write_bytes(SECRET_BYTES)
    return root


@pytest.fixture
def client(tmp_path: Path, dataset_dir: Path, crops_root: Path) -> Iterator[TestClient]:
    db_path = tmp_path / "crossfoot.db"
    with closing(connect(db_path)) as connection, connection:
        ensure_schema(connection)
        _seed(connection)
    scorecards_dir = tmp_path / "scorecards"
    scorecards_dir.mkdir()
    app = create_app(
        db_path=db_path,
        crops_root=crops_root,
        scorecards_dir=scorecards_dir,
        dataset_dir=dataset_dir,
    )
    with TestClient(app) as test_client:
        yield test_client


def _url(doc_id: str, field_id: str) -> str:
    return f"/api/crops/{doc_id}/{field_id}.png"


def _band_url(line_no: int, doc_id: str = VISION_DOC) -> str:
    return _url(doc_id, _vision_field(doc_id, line_no, FieldName.LINE_AMOUNT))


def _pixels(payload: bytes) -> tuple[int, int]:
    """(width, height) of a PNG body, decoded rather than trusted."""
    image = crops.decode_png(payload)
    return int(image.shape[1]), int(image.shape[0])


def _recorded_kind(db_path: Path, field_id: str) -> str:
    with closing(connect(db_path)) as connection:
        row = connection.execute(_CROP_KIND, {"field_id": field_id}).fetchone()
    return str(row["crop_kind"])


def _capped(pixels: tuple[int, int]) -> tuple[int, int]:
    """The size the route serves for a crop of these dimensions, after the cap."""
    width, height = pixels
    longest = max(width, height)
    if longest <= crops.MAX_SERVED_EDGE_PX:
        return pixels
    scale = crops.MAX_SERVED_EDGE_PX / longest
    return max(1, round(width * scale)), max(1, round(height * scale))


def _row_centre_pixels(line_no: int) -> float:
    """Where the row's print sits down the page, from the layout this file chose.

    Independent of the locator under test: it comes from the PDF baseline, turned
    the right way up and scaled to the pixels the renderer works in.
    """
    baseline = VISION_FIRST_BASELINE - (line_no - 1) * VISION_ROW_PITCH
    return (PAGE_HEIGHT - baseline - FONT_SIZE / 2) * SCALE


def _page_image(dataset_dir: Path, doc_id: str) -> crops.Image:
    """The fixture page as the renderer rasterizes it, for locating a served band."""
    document = pypdfium2.PdfDocument(dataset_dir / "files" / f"{doc_id}.pdf")
    try:
        rendered = document[0].render(scale=SCALE).to_pil()
    finally:
        document.close()
    image: crops.Image = np.ascontiguousarray(
        np.asarray(rendered.convert("RGB"), dtype=np.uint8)[:, :, ::-1]
    )
    return image


def _band_centre_on_page(payload: bytes, page: crops.Image) -> float:
    """Where a served band sits down its page, found by matching it back onto it.

    The band is served shrunk to the size cap, so the page is shrunk by the same
    factor before matching and the answer is scaled back up.
    """
    band = crops.decode_png(payload)
    scale = band.shape[1] / page.shape[1]
    shrunk = cv2.resize(
        page,
        (band.shape[1], max(1, round(page.shape[0] * scale))),
        interpolation=cv2.INTER_AREA,
    )
    scores = cv2.matchTemplate(shrunk, band, cv2.TM_CCOEFF_NORMED)
    _, best, _, location = cv2.minMaxLoc(scores)
    assert best > 0.9, f"the served band does not appear on its own page: {best}"
    centre: float = (int(location[1]) + band.shape[0] / 2) / scale
    return centre


# Rendering.


def test_an_exact_bbox_field_renders_a_crop_smaller_than_its_page(client: TestClient) -> None:
    response = client.get(_url(DOC, EXACT_FIELD))
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/png")
    width, height = _pixels(response.content)
    page_width, page_height = PAGE_PIXELS
    # Strictly smaller on both axes, which is the proof it cropped rather than
    # quietly handing back the page and calling it a crop.
    assert width < page_width
    assert height < page_height


def test_a_full_page_field_renders_the_whole_page(client: TestClient) -> None:
    response = client.get(_url(DOC, FULL_PAGE_FIELD))
    assert response.status_code == 200
    assert _pixels(response.content) == _capped(PAGE_PIXELS)


def test_a_field_with_no_coordinates_still_gets_the_page(client: TestClient) -> None:
    response = client.get(_url(DOC, NO_COORDS_FIELD))
    assert response.status_code == 200
    assert _pixels(response.content) == _capped(PAGE_PIXELS)


# Row bands: the picture a vision field gets when its row can be found.


def test_a_vision_line_field_renders_a_band_of_its_own_row(
    client: TestClient, tmp_path: Path, dataset_dir: Path
) -> None:
    response = client.get(_band_url(BAND_ROW))
    assert response.status_code == 200
    width, height = _pixels(response.content)
    page_width, _ = _capped(PAGE_PIXELS)

    assert height < PAGE_PIXELS[1] * MAX_BAND_HEIGHT_FRACTION
    assert width > page_width  # a band keeps the page's full width, uncropped
    field_id = _vision_field(VISION_DOC, BAND_ROW, FieldName.LINE_AMOUNT)
    assert _recorded_kind(tmp_path / "crossfoot.db", field_id) == CropKind.ROW_BAND.value

    centre = _band_centre_on_page(response.content, _page_image(dataset_dir, VISION_DOC))
    assert abs(centre - _row_centre_pixels(BAND_ROW)) <= VISION_ROW_PITCH * SCALE


def test_two_rows_of_one_table_get_two_different_bands(
    client: TestClient, dataset_dir: Path
) -> None:
    page = _page_image(dataset_dir, VISION_DOC)
    first = client.get(_band_url(BAND_ROW))
    other = client.get(_band_url(OTHER_BAND_ROW))
    assert first.content != other.content
    apart = _band_centre_on_page(other.content, page) - _band_centre_on_page(first.content, page)
    assert apart == pytest.approx((OTHER_BAND_ROW - BAND_ROW) * VISION_ROW_PITCH * SCALE, abs=20)


def test_a_vision_header_field_keeps_the_whole_page(client: TestClient, tmp_path: Path) -> None:
    field_id = _vision_field(VISION_DOC, None, FieldName.STATEMENT_NUMBER)
    response = client.get(_url(VISION_DOC, field_id))
    assert response.status_code == 200
    assert _pixels(response.content) == _capped(PAGE_PIXELS)
    assert _recorded_kind(tmp_path / "crossfoot.db", field_id) == CropKind.FULL_PAGE.value


def test_a_row_that_cannot_be_located_keeps_the_whole_page(
    client: TestClient, tmp_path: Path
) -> None:
    # The page prints six rows and the model reported five, so its row numbers
    # index nothing that was found and no band is offered for any of them.
    response = client.get(_band_url(BAND_ROW, doc_id=MISCOUNTED_DOC))
    assert response.status_code == 200
    assert _pixels(response.content) == _capped(PAGE_PIXELS)
    field_id = _vision_field(MISCOUNTED_DOC, BAND_ROW, FieldName.LINE_AMOUNT)
    assert _recorded_kind(tmp_path / "crossfoot.db", field_id) == CropKind.FULL_PAGE.value


def test_a_stored_box_that_fails_a_sanity_check_leaves_the_band_alone(
    client: TestClient, dataset_dir: Path
) -> None:
    # The hinted row carries coordinates shaped like a column. A trusted hint
    # would have narrowed the band's width; a discarded one leaves it whole.
    hinted = client.get(_band_url(HINTED_ROW))
    assert hinted.status_code == 200
    assert _pixels(hinted.content)[0] == _pixels(client.get(_band_url(BAND_ROW)).content)[0]
    centre = _band_centre_on_page(hinted.content, _page_image(dataset_dir, VISION_DOC))
    assert abs(centre - _row_centre_pixels(HINTED_ROW)) <= VISION_ROW_PITCH * SCALE


def test_the_band_is_served_from_the_cache_the_second_time(
    client: TestClient, crops_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = client.get(_band_url(BAND_ROW))
    cached = (
        crops_root
        / VISION_DOC
        / f"{_vision_field(VISION_DOC, BAND_ROW, FieldName.LINE_AMOUNT)}.png"
    )
    written = cached.stat().st_mtime_ns

    renders: list[object] = []
    monkeypatch.setattr(crops_route, "render_crop_file", lambda **kwargs: renders.append(kwargs))
    second = client.get(_band_url(BAND_ROW))

    assert second.status_code == 200
    assert second.content == first.content
    assert renders == []
    assert cached.stat().st_mtime_ns == written


# The size cap.


def test_every_served_crop_fits_the_size_cap(client: TestClient) -> None:
    served = (
        _url(DOC, EXACT_FIELD),
        _url(DOC, FULL_PAGE_FIELD),
        _url(DOC, NO_COORDS_FIELD),
        _url(VISION_DOC, _vision_field(VISION_DOC, None, FieldName.STATEMENT_NUMBER)),
        _band_url(BAND_ROW),
        _band_url(BAND_ROW, doc_id=MISCOUNTED_DOC),
    )
    for url in served:
        response = client.get(url)
        assert response.status_code == 200, url
        assert max(_pixels(response.content)) <= crops.MAX_SERVED_EDGE_PX, url


def test_the_full_page_fallback_is_bounded_and_keeps_its_shape(client: TestClient) -> None:
    width, height = _pixels(client.get(_url(DOC, FULL_PAGE_FIELD)).content)
    assert max(width, height) == crops.MAX_SERVED_EDGE_PX
    assert width / height == pytest.approx(PAGE_PIXELS[0] / PAGE_PIXELS[1], abs=0.01)


def test_the_crop_is_cached_where_the_contract_says_it_is(
    client: TestClient, crops_root: Path
) -> None:
    assert client.get(_url(DOC, EXACT_FIELD)).status_code == 200
    assert (crops_root / DOC / f"{EXACT_FIELD}.png").is_file()


def test_the_second_request_serves_the_cached_file_without_rendering_again(
    client: TestClient, crops_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = client.get(_url(DOC, EXACT_FIELD))
    assert first.status_code == 200
    cached = crops_root / DOC / f"{EXACT_FIELD}.png"
    written = cached.stat().st_mtime_ns

    renders: list[object] = []
    monkeypatch.setattr(crops_route, "render_crop_file", lambda **kwargs: renders.append(kwargs))
    second = client.get(_url(DOC, EXACT_FIELD))

    assert second.status_code == 200
    assert second.content == first.content
    assert renders == []
    assert cached.stat().st_mtime_ns == written


# Everything that is not a picture.


def test_an_unknown_field_is_still_404(client: TestClient) -> None:
    response = client.get(_url(DOC, UNKNOWN_FIELD))
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")


def test_a_field_asked_for_under_the_wrong_document_is_404(client: TestClient) -> None:
    response = client.get(_url(BROKEN_DOC, EXACT_FIELD))
    assert response.status_code == 404


def test_a_corrupted_source_document_is_a_typed_error_not_a_500(client: TestClient) -> None:
    # TestClient re-raises server exceptions, so a 500 would fail this as an
    # error rather than answer it.
    response = client.get(_url(BROKEN_DOC, BROKEN_FIELD))
    assert response.status_code == 424
    payload = response.json()
    assert payload["reason"] == CropUnavailableReason.SOURCE_UNREADABLE.value
    assert payload["doc_id"] == BROKEN_DOC
    assert payload["field_id"] == BROKEN_FIELD
    assert payload["detail"]


def test_a_document_whose_file_is_gone_is_a_typed_error(
    client: TestClient, dataset_dir: Path
) -> None:
    (dataset_dir / "files" / f"{DOC}.pdf").unlink()
    payload = client.get(_url(DOC, FULL_PAGE_FIELD)).json()
    assert payload["reason"] == CropUnavailableReason.SOURCE_MISSING.value


# Containment, restated against the renderer.


@pytest.mark.parametrize("url", HOSTILE_URLS)
def test_a_hostile_segment_never_renders_and_never_leaks(
    client: TestClient, url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    renders: list[object] = []

    def _spy(**kwargs: Any) -> None:
        renders.append(kwargs)

    monkeypatch.setattr(crops_route, "render_crop_file", _spy)
    response = client.get(url)
    # 400 from the handler, or 404 when encoded separators kept it off the route.
    assert response.status_code in {400, 404}
    assert SECRET_BYTES not in response.content
    assert renders == []


def test_no_hostile_request_wrote_anything_into_the_crop_root(
    client: TestClient, crops_root: Path
) -> None:
    for url in HOSTILE_URLS:
        client.get(url)
    assert list(crops_root.rglob("*")) == []
    assert (crops_root.parent / "outside" / "secret.png").read_bytes() == SECRET_BYTES


def test_no_hostile_request_recorded_a_crop_kind(client: TestClient, tmp_path: Path) -> None:
    # Containment runs before the render, and the render is what records a kind,
    # so a rejected request must leave every field's caption exactly as seeded.
    db_path = tmp_path / "crossfoot.db"
    fields = (
        EXACT_FIELD,
        FULL_PAGE_FIELD,
        _vision_field(VISION_DOC, BAND_ROW, FieldName.LINE_AMOUNT),
    )
    before = {field_id: _recorded_kind(db_path, field_id) for field_id in fields}
    for url in HOSTILE_URLS:
        client.get(url)
    assert {field_id: _recorded_kind(db_path, field_id) for field_id in fields} == before
