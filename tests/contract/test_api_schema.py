"""The OpenAPI schema is the frontend contract, so the snapshot is the freeze.

Written against docs/contracts-phase3.md before `crossfoot.api` exists, so the
module-level importorskip keeps collection clean today. The snapshot file is
generated once, with `--snapshot-update`, on the first run where the module
imports: it cannot be generated while the test skips, and a review of that first
diff is the freeze the contract asks for.

Phase 3 names routes but no factory, so these tests pin the smallest surface
that can express it, and that pin is binding in the phase 2 sense:

    crossfoot.api.create_app(
        *, db_path: Path, crops_root: Path, scorecards_dir: Path
    ) -> FastAPI

Every path in the schema lives under `/api`; the built frontend is served by a
mount, which contributes no OpenAPI paths.
"""

import json
from pathlib import Path
from typing import Any

import pytest
from api_seed import DB_NAME, connection, create_schema
from syrupy.assertion import SnapshotAssertion

api = pytest.importorskip("crossfoot.api")

# Every route docs/contracts-phase3.md names, with the method it names, and
# nothing else. A route added without amending the document fails the last test
# here as well as showing up in the snapshot diff.
DOCUMENTED_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        ("get", "/api/stats/summary"),
        ("get", "/api/review/queue"),
        ("get", "/api/review/items/{field_id}"),
        ("post", "/api/review/items/{field_id}/accept"),
        ("post", "/api/review/items/{field_id}/correct"),
        ("get", "/api/crops/{doc_id}/{field_id}.png"),
        ("get", "/api/documents"),
        ("get", "/api/documents/{doc_id}"),
        ("get", "/api/exceptions"),
        ("post", "/api/exceptions/{exception_id}/resolve"),
        ("get", "/api/metrics"),
    }
)


@pytest.fixture
def empty_app(tmp_path: Path) -> Any:
    """An app over an empty but schema-complete database."""
    db_path = tmp_path / DB_NAME
    with connection(db_path) as conn:
        create_schema(conn)
    crops_root = tmp_path / "crops"
    crops_root.mkdir()
    scorecards_dir = tmp_path / "scorecards"
    scorecards_dir.mkdir()
    return api.create_app(db_path=db_path, crops_root=crops_root, scorecards_dir=scorecards_dir)


def schema_routes(app: Any) -> set[tuple[str, str]]:
    """(method, path) pairs the schema publishes, methods lowercased."""
    paths: dict[str, dict[str, Any]] = app.openapi()["paths"]
    return {(method.lower(), path) for path, operations in paths.items() for method in operations}


def test_openapi_schema_is_the_frozen_frontend_contract(
    empty_app: Any, snapshot: SnapshotAssertion
) -> None:
    schema = json.dumps(empty_app.openapi(), sort_keys=True, indent=2)
    assert schema == snapshot


def test_every_documented_route_exists_with_its_documented_method(empty_app: Any) -> None:
    assert schema_routes(empty_app) >= DOCUMENTED_ROUTES


def test_no_undocumented_route_has_appeared(empty_app: Any) -> None:
    assert schema_routes(empty_app) == DOCUMENTED_ROUTES


def test_every_published_path_lives_under_the_api_prefix(empty_app: Any) -> None:
    assert all(path.startswith("/api/") for _, path in schema_routes(empty_app))
