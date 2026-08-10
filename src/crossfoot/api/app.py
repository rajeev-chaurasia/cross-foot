"""The application factory.

`create_app` takes every path it reads, so the tests point it at a temporary
directory and `crossfoot serve` points it at the repo's. No auth: this is a
single operator tool and pretending otherwise would be theatre.
"""

from __future__ import annotations

from contextlib import closing
from pathlib import Path

from fastapi import FastAPI
from starlette.exceptions import HTTPException
from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.status import HTTP_404_NOT_FOUND
from starlette.types import Scope

from crossfoot import __version__
from crossfoot.api.deps import API_PREFIX, STATE_ATTRIBUTE, ApiPaths
from crossfoot.api.routes import ROUTERS
from crossfoot.db import connect
from crossfoot.db.schema import ensure_schema

API_TITLE = "Crossfoot review API"
API_DESCRIPTION = (
    "The review queue, the exceptions dashboard, and the published calibration figures."
)

# Where `crossfoot serve` and a reloading worker find their data.
DEFAULT_DB_PATH = Path("data/crossfoot.db")
DEFAULT_CROPS_ROOT = Path("data/crops")
DEFAULT_SCORECARDS_DIR = Path("scorecards")
DEFAULT_DATASET_DIR = Path("data/dataset")
FRONTEND_DIST = Path("frontend/dist")

FRONTEND_MOUNT = "/"
FRONTEND_NAME = "frontend"
SPA_SHELL = "index.html"
API_ROOT_SEGMENT = API_PREFIX.strip("/")


class SpaFiles(StaticFiles):
    """Static files that answer an unknown page with the app shell.

    Routing happens in the browser, so /metrics is a real page with no file
    behind it. Plain StaticFiles answers 404 there, which breaks a bookmark, a
    refresh, and any shared link that is not the root.
    """

    async def get_response(self, path: str, scope: Scope) -> Response:
        try:
            return await super().get_response(path, scope)
        except HTTPException as error:
            if error.status_code != HTTP_404_NOT_FOUND or not _is_page(path):
                raise
            return await super().get_response(SPA_SHELL, scope)


def _is_page(path: str) -> bool:
    """Whether an unmatched path is a browser route rather than a missing file.

    A suffix means an asset was asked for, and answering a missing script with
    HTML turns a clear 404 into a content type error. An unmatched path under
    the API prefix is a caller's typo and has to stay JSON, since a page there
    would tell a client its request succeeded.
    """
    parts = Path(path).parts
    return not Path(path).suffix and parts[:1] != (API_ROOT_SEGMENT,)


def create_app(
    *,
    db_path: Path,
    crops_root: Path,
    scorecards_dir: Path,
    dataset_dir: Path = DEFAULT_DATASET_DIR,
) -> FastAPI:
    """The API over a materialized review database.

    `dataset_dir` defaults rather than being required because the first three
    arguments are the frozen contract signature; it is where a crop that has not
    been rendered yet finds its page.
    """
    app = FastAPI(title=API_TITLE, description=API_DESCRIPTION, version=__version__)
    setattr(
        app.state,
        STATE_ATTRIBUTE,
        ApiPaths(
            db_path=db_path,
            crops_root=crops_root,
            scorecards_dir=scorecards_dir,
            dataset_dir=dataset_dir,
        ),
    )
    # Idempotent, and it is what lets a database materialized by an older build
    # still serve rather than fail on a column it never had.
    with closing(connect(db_path)) as connection, connection:
        ensure_schema(connection)
    for router in ROUTERS:
        app.include_router(router, prefix=API_PREFIX)
    return app


def mount_frontend(app: FastAPI, dist: Path = FRONTEND_DIST) -> bool:
    """Serve the built frontend under the API, if it has been built.

    A mount publishes no OpenAPI paths, so the schema stays exactly the API.
    """
    if not (dist / "index.html").is_file():
        return False
    app.mount(FRONTEND_MOUNT, SpaFiles(directory=dist, html=True), name=FRONTEND_NAME)
    return True


def default_app() -> FastAPI:
    """Factory for `uvicorn --reload`, which needs an import string, not an app."""
    app = create_app(
        db_path=DEFAULT_DB_PATH,
        crops_root=DEFAULT_CROPS_ROOT,
        scorecards_dir=DEFAULT_SCORECARDS_DIR,
        dataset_dir=DEFAULT_DATASET_DIR,
    )
    mount_frontend(app)
    return app
