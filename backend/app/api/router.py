"""Root API router.

Aggregates feature routers so ``main.py`` only wires a single router.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.routes import health

api_router = APIRouter()
api_router.include_router(health.router)
