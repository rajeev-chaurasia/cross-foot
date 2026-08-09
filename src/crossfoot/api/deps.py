"""Request scoped wiring: where the data lives and one connection per request.

The paths are held on `app.state` rather than in module globals so two apps can
run in one process, which is what the contract tests do.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from fastapi import Depends, Request

from crossfoot.db import connect

API_PREFIX = "/api"
# One template, used by the route that serves a crop and by the item that links
# to it, so no client ever rebuilds the URL by hand.
CROP_PATH_TEMPLATE = "/crops/{doc_id}/{field_id}.png"

STATE_ATTRIBUTE = "api_paths"


@dataclass(frozen=True, slots=True)
class ApiPaths:
    """Everything the API reads, resolved once at startup."""

    db_path: Path
    crops_root: Path
    scorecards_dir: Path
    # Where the scans themselves live. A crop is rendered from the source
    # document on the first request for it, and `documents.file_path` is
    # relative to this directory, so the API cannot find a page without it.
    dataset_dir: Path


def api_paths(request: Request) -> ApiPaths:
    paths: ApiPaths = getattr(request.app.state, STATE_ATTRIBUTE)
    return paths


def db_connection(request: Request) -> Iterator[sqlite3.Connection]:
    """A connection per request, closed whatever the handler does with it."""
    connection = connect(api_paths(request).db_path, check_same_thread=False)
    try:
        yield connection
    finally:
        connection.close()


Connection = Annotated[sqlite3.Connection, Depends(db_connection)]
Paths = Annotated[ApiPaths, Depends(api_paths)]
