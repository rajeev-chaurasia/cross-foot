"""Contract tests for GET /api/crops: it serves cached crops and nothing else.

Written against docs/contracts-phase3.md before the implementation exists, so
the module-level importorskip keeps collection clean today.

Phase 1 shipped an arbitrary-file-read bug through exactly this shape, which is
why `crossfoot.evals.paths.resolve_dataset_path` exists and why the contract
says crop path segments "are validated the same way manifest paths are;
anything that escapes the crop root is a 400, never a file read". The same
severity applies here, so a real file is planted outside the crop root and every
hostile request is checked for its bytes.

Two families of hostile input, and the difference is routing, not policy:

- a segment that stays one path segment ("..", "..\\outside", "C:", "\\\\.\\C:",
  "\\etc\\passwd") reaches the handler, so the handler must answer 400
- a segment carrying percent-encoded separators decodes to extra path segments
  before the router sees it, so it matches no route and dies at 404

Both must leak nothing, which is the assertion that actually matters.
"""

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from api_seed import DB_NAME, connection, create_schema, insert_document, insert_field, signals_json
from fastapi.testclient import TestClient

from crossfoot.constants import (
    DocType,
    ExtractionRoute,
    FieldFamily,
    FieldName,
    QualityTier,
    ReviewStatus,
    SplitName,
)

api = pytest.importorskip("crossfoot.api")

DOC = "doc-a"
FIELD = "fld-a-0002"
MISSING_FIELD = "fld-a-9999"

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
CROP_BYTES = PNG_MAGIC + b"cached crop for fld-a-0002"
# Planted outside the crop root, named so a naive join really would serve it.
SECRET_BYTES = PNG_MAGIC + b"CROSSFOOT-CONTRACT-SECRET-DO-NOT-SERVE"

# Hostile segments that survive as one path segment, so the route matches and
# the handler is the only thing standing between the request and the file.
# The two "outside" forms resolve exactly onto the planted secret.
ROUTABLE_ESCAPES = (
    "/api/crops/%2E%2E/secret.png",  # doc_id ".."
    "/api/crops/..%5Coutside/secret.png",  # doc_id "..\\outside"
    "/api/crops/doc-a/..%5C..%5Coutside%5Csecret.png",  # field_id climbs out
    "/api/crops/C%3A/secret.png",  # doc_id "C:", a Windows drive letter
    "/api/crops/%5C%5C.%5CC%3A/secret.png",  # doc_id "\\\\.\\C:", device form
    "/api/crops/%5Cetc%5Cpasswd/secret.png",  # doc_id "\\etc\\passwd", rooted
)

# Hostile segments carrying encoded separators. These decode into extra path
# segments, so no route matches and the request never reaches a file at all.
ENCODED_SEPARATOR_ESCAPES = (
    "/api/crops/%2E%2E%2F%2E%2E%2Foutside/secret.png",
    "/api/crops/%2F%2F.%2FC%3A/secret.png",  # the "//./" device form
    "/api/crops/%2Fetc%2Fpasswd/secret.png",
    "/api/crops/doc-a/%2E%2E%2F%2E%2E%2Foutside%2Fsecret.png",
)


def seed(conn: sqlite3.Connection) -> None:
    insert_document(
        conn,
        doc_id=DOC,
        doc_type=DocType.PARTS_STATEMENT,
        quality_tier=QualityTier.CLEAN_DIGITAL,
        route=ExtractionRoute.DIGITAL_PDF,
        split=SplitName.TEST,
    )
    insert_field(
        conn,
        field_id=FIELD,
        doc_id=DOC,
        line_no=1,
        name=FieldName.LINE_AMOUNT,
        family=FieldFamily.AMOUNT,
        raw_text="$1,234.56",
        value="1234.56",
        value_cents=123_456,
        confidence=0.20,
        status=ReviewStatus.NEEDS_REVIEW,
        signals=signals_json(QualityTier.CLEAN_DIGITAL, validator_pass=1.0),
    )


@pytest.fixture
def crops_root(tmp_path: Path) -> Path:
    """A crop root holding one cached crop, with a secret planted beside it."""
    root = tmp_path / "crops"
    (root / DOC).mkdir(parents=True)
    (root / DOC / f"{FIELD}.png").write_bytes(CROP_BYTES)
    outside = tmp_path / "outside"
    outside.mkdir()
    # Both a sibling of the root and the root's own parent, so "..", "..\\..",
    # and "..\\outside" all land on something worth stealing.
    (outside / "secret.png").write_bytes(SECRET_BYTES)
    (tmp_path / "secret.png").write_bytes(SECRET_BYTES)
    return root


@pytest.fixture
def client(tmp_path: Path, crops_root: Path) -> Iterator[TestClient]:
    db_path = tmp_path / DB_NAME
    with connection(db_path) as conn:
        create_schema(conn)
        seed(conn)
    scorecards_dir = tmp_path / "scorecards"
    scorecards_dir.mkdir()
    app = api.create_app(db_path=db_path, crops_root=crops_root, scorecards_dir=scorecards_dir)
    with TestClient(app) as test_client:
        yield test_client


# The happy path.


def test_a_cached_crop_is_served(client: TestClient) -> None:
    response = client.get(f"/api/crops/{DOC}/{FIELD}.png")
    assert response.status_code == 200
    assert response.content == CROP_BYTES
    assert response.headers["content-type"].startswith("image/png")


def test_a_well_formed_request_for_a_missing_crop_is_404(client: TestClient) -> None:
    response = client.get(f"/api/crops/{DOC}/{MISSING_FIELD}.png")
    assert response.status_code == 404
    # A 404 body, not a traceback: TestClient re-raises server exceptions, so a
    # crash would fail this test rather than answer it.
    assert response.headers["content-type"].startswith("application/json")


# Path containment.


@pytest.mark.parametrize("url", ROUTABLE_ESCAPES)
def test_a_segment_escaping_the_crop_root_is_400(client: TestClient, url: str) -> None:
    response = client.get(url)
    assert response.status_code == 400


@pytest.mark.parametrize("url", ROUTABLE_ESCAPES)
def test_an_escaping_segment_never_returns_the_planted_file(client: TestClient, url: str) -> None:
    response = client.get(url)
    assert SECRET_BYTES not in response.content
    assert response.status_code != 200


@pytest.mark.parametrize("url", ENCODED_SEPARATOR_ESCAPES)
def test_encoded_separators_never_reach_a_file(client: TestClient, url: str) -> None:
    response = client.get(url)
    # Rejected by the router before the handler, which is still never a read.
    assert response.status_code in {400, 404}
    assert SECRET_BYTES not in response.content


def test_the_planted_file_is_still_where_it_was_left(client: TestClient, crops_root: Path) -> None:
    for url in (*ROUTABLE_ESCAPES, *ENCODED_SEPARATOR_ESCAPES):
        assert SECRET_BYTES not in client.get(url).content
    assert (crops_root.parent / "outside" / "secret.png").read_bytes() == SECRET_BYTES


def test_containment_does_not_break_the_ordinary_request(client: TestClient) -> None:
    for url in ROUTABLE_ESCAPES:
        client.get(url)
    assert client.get(f"/api/crops/{DOC}/{FIELD}.png").content == CROP_BYTES
