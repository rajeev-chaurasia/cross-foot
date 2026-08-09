"""Crops: the pixels beside a value, and nothing else on the disk.

Phase 1 shipped an arbitrary file read through exactly this shape, which is why
`resolve_dataset_path` exists. Both segments go through it before anything is
opened: `doc_id` against the crop root, then the file name against the directory
the first step produced. A segment that is absolute, carries a drive letter or a device
prefix, or climbs out with `..` is rejected before a path is built, so a hostile
request is answered from memory and never from the filesystem.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import FileResponse

from crossfoot.api.deps import CROP_PATH_TEMPLATE, Paths
from crossfoot.evals.paths import UnsafeDatasetPathError, resolve_dataset_path

router = APIRouter(tags=["crops"])

# The extension is part of the route, so it is spelled here rather than imported
# from crossfoot.extraction.crops: serving a cached file needs no image stack.
CROP_SUFFIX = ".png"
PNG_MEDIA_TYPE = "image/png"

ESCAPING_SEGMENT_DETAIL = "crop path escapes the crop root"
MISSING_CROP_DETAIL = "no cached crop for {doc_id}/{field_id}"


@router.get(
    CROP_PATH_TEMPLATE,
    response_class=FileResponse,
    responses={
        status.HTTP_200_OK: {"content": {PNG_MEDIA_TYPE: {}}, "description": "The cached crop."}
    },
)
def crop(paths: Paths, doc_id: str, field_id: str) -> FileResponse:
    """The cached crop for one field, or 400 for a segment that leaves the crop root."""
    try:
        directory = resolve_dataset_path(paths.crops_root, doc_id)
        path = resolve_dataset_path(directory, f"{field_id}{CROP_SUFFIX}")
    except UnsafeDatasetPathError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=ESCAPING_SEGMENT_DETAIL
        ) from error
    if not path.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=MISSING_CROP_DETAIL.format(doc_id=doc_id, field_id=field_id),
        )
    return FileResponse(path, media_type=PNG_MEDIA_TYPE)
