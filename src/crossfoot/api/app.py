"""The application factory.

`create_app` takes every path it reads, so the tests point it at a temporary
directory and `crossfoot serve` points it at the repo's. No auth: this is a
single operator tool and pretending otherwise would be theatre.
"""

from __future__ import annotations

from contextlib import closing
from pathlib import Path

from fastapi import FastAPI
from starlette.staticfiles import StaticFiles

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
FRONTEND_DIST = Path("frontend/dist")

FRONTEND_MOUNT = "/"
FRONTEND_NAME = "frontend"


def create_app(*, db_path: Path, crops_root: Path, scorecards_dir: Path) -> FastAPI:
    """The API over a materialized review database."""
    app = FastAPI(title=API_TITLE, description=API_DESCRIPTION, version=__version__)
    setattr(
        app.state,
        STATE_ATTRIBUTE,
        ApiPaths(db_path=db_path, crops_root=crops_root, scorecards_dir=scorecards_dir),
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
    app.mount(FRONTEND_MOUNT, StaticFiles(directory=dist, html=True), name=FRONTEND_NAME)
    return True


def default_app() -> FastAPI:
    """Factory for `uvicorn --reload`, which needs an import string, not an app."""
    app = create_app(
        db_path=DEFAULT_DB_PATH,
        crops_root=DEFAULT_CROPS_ROOT,
        scorecards_dir=DEFAULT_SCORECARDS_DIR,
    )
    mount_frontend(app)
    return app
