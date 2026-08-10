"""The built frontend is served so that every route survives a refresh.

Routing happens in the browser, so only `/` has a file behind it. The mount has
to answer the other routes with the same shell or a bookmark, a refresh, and a
shared link all return 404. It must not do that for assets or for the API, where
answering a miss with HTML hides the miss.
"""

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from crossfoot.api.app import SPA_SHELL, mount_frontend

SHELL_MARKER = "<!doctype html><title>crossfoot</title><div id=root></div>"
ASSET_PATH = "assets/index-abc123.js"
ASSET_BODY = "console.log(1)"

BROWSER_ROUTES = ("/metrics", "/exceptions", "/documents/doc-parts_statement-dlr-atlas-202604-01")


def _built_dist(tmp_path: Path) -> Path:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / SPA_SHELL).write_text(SHELL_MARKER, encoding="utf-8")
    (dist / ASSET_PATH).write_text(ASSET_BODY, encoding="utf-8")
    return dist


def _client(dist: Path) -> TestClient:
    app = FastAPI()

    @app.get("/api/stats/summary")
    def summary() -> dict[str, int]:
        return {"documents_processed": 1}

    assert mount_frontend(app, dist)
    return TestClient(app)


def test_mount_reports_absent_when_the_frontend_was_never_built(tmp_path: Path) -> None:
    assert mount_frontend(FastAPI(), tmp_path / "dist") is False


def test_root_serves_the_shell(tmp_path: Path) -> None:
    response = _client(_built_dist(tmp_path)).get("/")

    assert response.status_code == 200
    assert response.text == SHELL_MARKER


def test_browser_routes_serve_the_shell(tmp_path: Path) -> None:
    client = _client(_built_dist(tmp_path))

    for route in BROWSER_ROUTES:
        response = client.get(route)

        assert response.status_code == 200, route
        assert response.text == SHELL_MARKER, route


def test_existing_assets_are_served_unchanged(tmp_path: Path) -> None:
    response = _client(_built_dist(tmp_path)).get(f"/{ASSET_PATH}")

    assert response.status_code == 200
    assert response.text == ASSET_BODY


def test_a_missing_asset_stays_a_miss(tmp_path: Path) -> None:
    """Serving the shell here would report a content type error, not the 404."""
    response = _client(_built_dist(tmp_path)).get("/assets/index-gone.js")

    assert response.status_code == 404
    assert response.text != SHELL_MARKER


def test_the_api_still_answers_and_its_misses_stay_json(tmp_path: Path) -> None:
    client = _client(_built_dist(tmp_path))

    assert client.get("/api/stats/summary").json() == {"documents_processed": 1}

    missing = client.get("/api/no/such/route")

    assert missing.status_code == 404
    assert missing.text != SHELL_MARKER
