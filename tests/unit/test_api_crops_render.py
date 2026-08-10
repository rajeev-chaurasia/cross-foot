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

from crossfoot import pdfium
from crossfoot.api import create_app, crop_cache, crop_render
from crossfoot.api.dto import CropUnavailableReason
from crossfoot.constants import (
    CorruptionKind,
    CropKind,
    ExtractionRoute,
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
# A tabular statement: perfectly healthy, and with no page anywhere in it.
TABULAR_DOC = "doc-tabular"
TABULAR_FIELD = "fld-tabular-0001-line_amount"
TABULAR_CSV = "Date,Invoice,Description,Amount\n07/10/2026,INV1000000,Restock,1000.00\n"

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

# The handler's own refusal, and the router's when no route matched at all.
ESCAPING_SEGMENT_STATUS = 400
NO_SUCH_ROUTE_STATUS = 404

# Kept in the two families the security file separates, because they leave by
# different doors and the door is part of the claim: a segment that survives as
# one path segment reaches the handler and is refused there.
ROUTABLE_ESCAPES = (
    "/api/crops/%2E%2E/secret.png",
    "/api/crops/..%5Coutside/secret.png",
    "/api/crops/doc-a/..%5C..%5Coutside%5Csecret.png",
    "/api/crops/C%3A/secret.png",
    "/api/crops/%5C%5C.%5CC%3A/secret.png",
    "/api/crops/%5Cetc%5Cpasswd/secret.png",
)
# These decode into extra path segments before the router sees them, so they
# match no route and never reach the handler at all.
ENCODED_SEPARATOR_ESCAPES = (
    "/api/crops/%2E%2E%2F%2E%2E%2Foutside/secret.png",
    "/api/crops/%2F%2F.%2FC%3A/secret.png",
    "/api/crops/%2Fetc%2Fpasswd/secret.png",
    "/api/crops/doc-a/%2E%2E%2F%2E%2E%2Foutside%2Fsecret.png",
)
HOSTILE_URLS = (*ROUTABLE_ESCAPES, *ENCODED_SEPARATOR_ESCAPES)
# The code each family is documented to leave by, paired with the url so a
# refusal that moved between doors is a failure rather than an accepted variant.
HOSTILE_REQUESTS = (
    *((url, ESCAPING_SEGMENT_STATUS) for url in ROUTABLE_ESCAPES),
    *((url, NO_SUCH_ROUTE_STATUS) for url in ENCODED_SEPARATOR_ESCAPES),
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

# The render's own record, which is a different table from the extractor's
# fields.crop_kind and is absent entirely until a crop has been cut.
_CROP_KIND = "SELECT crop_kind FROM rendered_crops WHERE field_id = :field_id"
_CROP_RECORDS = "SELECT COUNT(*) AS recorded FROM rendered_crops"


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
    signals = FieldSignals(route=ExtractionRoute.DIGITAL_PDF).model_dump_json()
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
    _seed_tabular(connection, signals)


def _seed_tabular(connection: sqlite3.Connection, signals: str) -> None:
    """A CSV document, whose fields were read from rows rather than from a page."""
    connection.execute(
        _INSERT_DOCUMENT,
        {
            "doc_id": TABULAR_DOC,
            "file_path": f"files/{TABULAR_DOC}.csv",
            "quality_tier": QualityTier.CSV.value,
            "route": ExtractionRoute.CSV.value,
        },
    )
    connection.execute(
        _INSERT_LINE_FIELD,
        {
            "field_id": TABULAR_FIELD,
            "doc_id": TABULAR_DOC,
            "line_no": 1,
            "name": FieldName.LINE_AMOUNT.value,
            "family": FieldFamily.AMOUNT.value,
            "source": FieldSource.DETERMINISTIC.value,
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
    # Written for real, so a refusal to draw it is about the format rather than
    # about a file that happened to be missing.
    (files / f"{TABULAR_DOC}.csv").write_text(TABULAR_CSV, encoding="utf-8")
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


def _recorded_kind(db_path: Path, field_id: str) -> str | None:
    """What the render recorded for this field, or None when none has run."""
    with closing(connect(db_path)) as connection:
        row = connection.execute(_CROP_KIND, {"field_id": field_id}).fetchone()
    return None if row is None else str(row["crop_kind"])


def _crop_records(db_path: Path) -> int:
    """How many renders have recorded a kind, over every field in the database."""
    with closing(connect(db_path)) as connection:
        row = connection.execute(_CROP_RECORDS).fetchone()
    return int(row["recorded"])


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
    # Against the size the page itself is served at, not the uncapped raster:
    # every response is bounded by MAX_SERVED_EDGE_PX, so a comparison with the
    # raster is true of the whole page too and proves nothing. Strictly smaller
    # on both axes here is the proof it cropped rather than quietly handing back
    # the page and calling it a crop.
    page_width, page_height = _capped(PAGE_PIXELS)
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
    monkeypatch.setattr(crop_cache, "render_crop_file", lambda **kwargs: renders.append(kwargs))
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
    monkeypatch.setattr(crop_cache, "render_crop_file", lambda **kwargs: renders.append(kwargs))
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


# Formats that have rows rather than pages.


def test_a_tabular_field_reports_no_page_image_rather_than_an_unreadable_file(
    client: TestClient,
) -> None:
    response = client.get(_url(TABULAR_DOC, TABULAR_FIELD))
    assert response.status_code == 424
    payload = response.json()
    assert payload["reason"] == CropUnavailableReason.NO_PAGE_IMAGE.value
    assert payload["detail"]


def test_a_tabular_field_is_never_opened_as_a_document(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The CSV on disk is healthy. PDFium asked to parse one answers "data format
    # error", which a reviewer reads as a corrupted statement, so the format has
    # to be turned away before the file is opened at all.
    def _refuse(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("a tabular source was opened as a document")

    # crop_render holds the module, so patching the attribute here is what it sees.
    monkeypatch.setattr(pdfium, "open_document", _refuse)
    assert client.get(_url(TABULAR_DOC, TABULAR_FIELD)).status_code == 424


def test_a_tabular_field_writes_no_crop_and_records_no_kind(
    client: TestClient, crops_root: Path, tmp_path: Path
) -> None:
    client.get(_url(TABULAR_DOC, TABULAR_FIELD))
    assert list(crops_root.rglob("*.png")) == []
    assert _recorded_kind(tmp_path / "crossfoot.db", TABULAR_FIELD) is None


# The caption the review item publishes, which has to describe the served bytes.


def _detail(client: TestClient, field_id: str) -> dict[str, Any]:
    response = client.get(f"/api/review/items/{field_id}")
    assert response.status_code == 200
    payload: dict[str, Any] = response.json()
    return payload


def test_the_item_caption_is_settled_before_any_crop_is_requested(client: TestClient) -> None:
    # This field is seeded full_page, because that is all the extractor could say
    # without the page in front of it. The band is only knowable from the page,
    # so a caption that says row_band here is proof the item looked.
    field_id = _vision_field(VISION_DOC, BAND_ROW, FieldName.LINE_AMOUNT)
    payload = _detail(client, field_id)
    assert payload["crop_kind"] == CropKind.ROW_BAND.value
    assert payload["crop_unavailable_reason"] is None


def test_the_item_caption_does_not_move_when_the_crop_is_fetched(client: TestClient) -> None:
    field_id = _vision_field(VISION_DOC, BAND_ROW, FieldName.LINE_AMOUNT)
    before = _detail(client, field_id)["crop_kind"]
    assert client.get(_band_url(BAND_ROW)).status_code == 200
    assert _detail(client, field_id)["crop_kind"] == before


def test_an_unforeseen_render_failure_costs_the_picture_and_nothing_else(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The item is a value, a signal breakdown and the rest of the line, plus a panel.

    A render that fails for a reason nobody enumerated used to take the whole
    route with it, so the reviewer lost the field instead of the picture.
    """

    def _vanish(**kwargs: Any) -> None:
        raise FileNotFoundError("the page image went away mid render")

    monkeypatch.setattr(crop_cache, "render_crop_file", _vanish)
    payload = _detail(client, _vision_field(VISION_DOC, BAND_ROW, FieldName.LINE_AMOUNT))
    assert payload["crop_kind"] is None
    assert payload["crop_unavailable_reason"] == CropUnavailableReason.SOURCE_UNREADABLE.value
    assert payload["value"] is not None
    assert payload["signals"]
    assert payload["document"]["doc_id"] == VISION_DOC


def test_a_tabular_item_is_captioned_as_a_format_with_no_page_image(client: TestClient) -> None:
    payload = _detail(client, TABULAR_FIELD)
    assert payload["crop_kind"] is None
    assert payload["crop_unavailable_reason"] == CropUnavailableReason.NO_PAGE_IMAGE.value


def test_a_crop_cached_without_a_record_is_cut_again_before_it_is_captioned(
    client: TestClient, crops_root: Path
) -> None:
    """Rebuilding the database leaves the crops on disk and no record of them.

    Captioning that file with whatever the extractor had guessed is exactly the
    contradiction this record exists to stop, so the item renders it again.
    """
    field_id = _vision_field(VISION_DOC, BAND_ROW, FieldName.LINE_AMOUNT)
    stale = crops_root / VISION_DOC / f"{field_id}.png"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(PNG_MAGIC + b"a crop from a build whose record is gone")

    payload = _detail(client, field_id)

    assert payload["crop_kind"] == CropKind.ROW_BAND.value
    assert b"whose record is gone" not in stale.read_bytes()


# Containment, restated against the renderer.


@pytest.mark.parametrize(("url", "expected_status"), HOSTILE_REQUESTS)
def test_a_hostile_segment_never_renders_and_never_leaks(
    client: TestClient, url: str, expected_status: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    renders: list[object] = []

    def _spy(**kwargs: Any) -> None:
        renders.append(kwargs)

    monkeypatch.setattr(crop_cache, "render_crop_file", _spy)
    response = client.get(url)
    assert response.status_code == expected_status
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
    # so a rejected request must leave the table empty. Counted over the whole
    # table rather than over named fields, because a hostile pair that got
    # through would record the field id it invented and no named field would
    # show it.
    db_path = tmp_path / "crossfoot.db"
    for url in HOSTILE_URLS:
        client.get(url)
    assert _crop_records(db_path) == 0
    # The count is live: one ordinary request moves it. Without this, a query
    # that answered zero for every database would pass the assertion above.
    assert client.get(_url(DOC, EXACT_FIELD)).status_code == 200
    assert _crop_records(db_path) == 1
