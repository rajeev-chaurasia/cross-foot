"""Crops: the pixels beside a value, rendered once and read from disk after that.

Phase 1 shipped an arbitrary file read through exactly this shape, which is why
`resolve_dataset_path` exists. Both segments go through it before anything is
opened: `doc_id` against the crop root, then the file name against the directory
the first step produced. A segment that is absolute, carries a drive letter or a
device prefix, or climbs out with `..` is rejected before a path is built, so a
hostile request is answered from memory, never from the filesystem, and never
from the renderer, which the check stands in front of.

The order is containment, then the cache, then the database, then the render,
and the last three live in crossfoot.api.crop_cache because the review item
settles the same crop to caption it. This route serves bytes and publishes no
caption, so a cached file answers it outright; the item, which does publish one,
never accepts a file whose render was not recorded. A document with pages always
yields an image: an exact box is cut from its page, a vision field gets the band
of the row it was read from when that row can be located, and anything else falls
back to the whole page. 404 means the pair names no field, not that nothing has
been rendered yet. A source document that cannot be rasterized, or that has no
pages to rasterize at all, is a property of the document, so it is a typed 424
body the queue can show the value around, never a 500.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import FileResponse, JSONResponse
from starlette.responses import Response

from crossfoot.api import crop_cache
from crossfoot.api.crop_render import CropSourceError
from crossfoot.api.deps import CROP_PATH_TEMPLATE, Connection, Paths
from crossfoot.api.dto import CropUnavailable
from crossfoot.evals.paths import UnsafeDatasetPathError

_LOGGER = logging.getLogger(__name__)

router = APIRouter(tags=["crops"])

PNG_MEDIA_TYPE = "image/png"

ESCAPING_SEGMENT_DETAIL = "crop path escapes the crop root"
MISSING_CROP_DETAIL = "no field {field_id} in document {doc_id}"

# 424 rather than 500: the request was fine and the field is real, but the
# document the crop depends on could not be read. FastAPI already publishes a
# 422 on this route for its own path validation, so the typed body needs a code
# of its own.
CROP_UNAVAILABLE_STATUS = status.HTTP_424_FAILED_DEPENDENCY


@router.get(
    CROP_PATH_TEMPLATE,
    response_class=FileResponse,
    responses={
        status.HTTP_200_OK: {"content": {PNG_MEDIA_TYPE: {}}, "description": "The crop."},
        CROP_UNAVAILABLE_STATUS: {
            "model": CropUnavailable,
            "description": "The field exists but its source document cannot be rendered.",
        },
    },
)
def crop(paths: Paths, connection: Connection, doc_id: str, field_id: str) -> Response:
    """The crop for one field, rendered on the first request and cached after it.

    A segment that leaves the crop root is a 400 and never a file read.
    """
    try:
        path = crop_cache.cached_path(paths, doc_id=doc_id, field_id=field_id)
    except UnsafeDatasetPathError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=ESCAPING_SEGMENT_DETAIL
        ) from error
    if path.is_file():
        return FileResponse(path, media_type=PNG_MEDIA_TYPE)
    try:
        rendered = crop_cache.rendered_crop(paths, connection, doc_id=doc_id, field_id=field_id)
    except crop_cache.UnknownCropFieldError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=MISSING_CROP_DETAIL.format(doc_id=doc_id, field_id=field_id),
        ) from error
    except CropSourceError as error:
        _LOGGER.warning("no crop for %s/%s: %s", doc_id, field_id, error.detail)
        payload = CropUnavailable(
            doc_id=doc_id, field_id=field_id, reason=error.reason, detail=error.detail
        )
        return JSONResponse(
            status_code=CROP_UNAVAILABLE_STATUS, content=payload.model_dump(mode="json")
        )
    return FileResponse(rendered.path, media_type=PNG_MEDIA_TYPE)
