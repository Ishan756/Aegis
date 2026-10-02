"""Versioned API surface.

Only stable, shape-committed endpoints belong here. Pre-1.0 endpoints (the agent
router) are mounted separately in ``create_app`` under an unversioned prefix.
"""

from fastapi import APIRouter

from app.api.routes import health

api_router = APIRouter()
api_router.include_router(health.router)

__all__ = ["api_router"]
