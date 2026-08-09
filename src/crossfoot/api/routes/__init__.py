"""Every route the API publishes, in the order the OpenAPI schema lists them."""

from fastapi import APIRouter

from crossfoot.api.routes.crops import router as crops_router
from crossfoot.api.routes.documents import router as documents_router
from crossfoot.api.routes.exceptions import router as exceptions_router
from crossfoot.api.routes.metrics import router as metrics_router
from crossfoot.api.routes.review import router as review_router

ROUTERS: tuple[APIRouter, ...] = (
    metrics_router,
    review_router,
    crops_router,
    documents_router,
    exceptions_router,
)

__all__ = ["ROUTERS"]
