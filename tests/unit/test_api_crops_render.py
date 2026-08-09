"""Crops render on the first request, from the rows the queue is already read from.

The screen this project is judged on puts a flagged value next to the pixels it
came from, so a field the database holds always yields an image: the exact box
when there is one, the whole page underneath everything else. A 404 means the
pair names no field, never that nothing has been rendered yet.

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

import pytest
from fastapi.testclient import TestClient
from pdf_fixtures import PAGE_HEIGHT, PAGE_WIDTH, TRUTH_DOC, minimal_pdf, statement_items

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

EXACT_FIELD = "fld-a-0001-line_amount"
FULL_PAGE_FIELD = "fld-a-0002-statement_number"
NO_COORDS_FIELD = "fld-a-0003-line_amount"
BROKEN_FIELD = "fld-broken-0001-line_amount"
UNKNOWN_FIELD = "fld-a-9999"

# The amount cell on the first printed line of the fixture statement, as a share
# of the page. Small enough that padding cannot grow it back to the full page.
AMOUNT_BBOX = (0.78, 0.25, 0.92, 0.28)

# The page the fixture PDF declares, in the pixels the renderer produces from it.
SCALE = crop_render.REVIEW_CROP_DPI / crop_render.PDF_POINTS_PER_INCH
PAGE_PIXELS = (round(PAGE_WIDTH * SCALE), round(PAGE_HEIGHT * SCALE))

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


@pytest.fixture
def dataset_dir(tmp_path: Path) -> Path:
    """A dataset holding one readable statement and one file of binary junk."""
    dataset = tmp_path / "dataset"
    files = dataset / "files"
    files.mkdir(parents=True)
    (files / f"{DOC}.pdf").write_bytes(minimal_pdf(statement_items(TRUTH_DOC)))
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


def _pixels(payload: bytes) -> tuple[int, int]:
    """(width, height) of a PNG body, decoded rather than trusted."""
    image = crops.decode_png(payload)
    return int(image.shape[1]), int(image.shape[0])


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
    assert _pixels(response.content) == PAGE_PIXELS


def test_a_field_with_no_coordinates_still_gets_the_page(client: TestClient) -> None:
    response = client.get(_url(DOC, NO_COORDS_FIELD))
    assert response.status_code == 200
    assert _pixels(response.content) == PAGE_PIXELS


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
