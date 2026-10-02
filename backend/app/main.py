"""Aegis backend application entrypoint."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.router import api_router
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.models.health import RootResponse
from app.services.status import StatusService

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Manage application startup and shutdown.

    Long-lived resources (database pool, MCP sessions, agent graph) will be
    opened here as later stages introduce them.
    """
    settings = get_settings()
    app.state.status_service = StatusService(settings)
    logger.info("Aegis backend starting (env=%s)", settings.environment)
    yield
    logger.info("Aegis backend shutting down")


def create_app() -> FastAPI:
    """Build and configure the FastAPI application."""
    settings = get_settings()
    configure_logging(settings)

    app = FastAPI(
        title=settings.app_name,
        version=settings.version,
        description=(
            "Autonomous AI DevOps Engineer API. "
            f"Current implementation stage: {settings.environment}."
        ),
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(api_router, prefix=settings.api_prefix)

    @app.get("/", response_model=RootResponse, tags=["meta"])
    def read_root() -> RootResponse:
        """Service banner, useful for humans poking at the API."""
        return RootResponse(
            service=settings.app_name,
            version=settings.version,
            docs="/docs",
        )

    @app.get("/health", response_model=dict, tags=["meta"], include_in_schema=False)
    def unversioned_health() -> dict[str, str]:
        """Unversioned alias so container probes can avoid the version prefix."""
        return {"status": "ok"}

    return app


app = create_app()
