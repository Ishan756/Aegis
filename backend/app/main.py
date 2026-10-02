"""Aegis backend application entrypoint."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.errors import register_exception_handlers
from app.api.router import api_router
from app.api.routes import agent
from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.core.middleware import RequestLoggingMiddleware
from app.models.health import RootResponse
from app.services.status import StatusService

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Manage application startup and shutdown.

    Long-lived resources (database pool, MCP sessions, agent graph) will be
    opened here as later stages introduce them.
    """
    settings = get_settings()
    app.state.status_service = StatusService(settings)

    logger.info(
        "Aegis backend starting",
        # safe_summary() only reports whether credentials are present, never
        # their values, so this is safe to log.
        extra={"settings": settings.safe_summary()},
    )
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

    # Registered before middleware so every error path is handled, including
    # errors raised by the middleware stack itself.
    register_exception_handlers(app)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Added last so it is outermost: every request is logged and receives a
    # correlation ID before any other middleware sees it.
    app.add_middleware(
        RequestLoggingMiddleware,
        trust_forwarded_headers=settings.trust_forwarded_headers,
    )

    app.include_router(api_router, prefix=settings.api_prefix)

    # Agent routes are pre-1.0 and deliberately unversioned, so planning lives at
    # POST /api/agent/plan rather than under /api/v1. Move it under the versioned
    # prefix once the response shape settles.
    app.include_router(agent.router, prefix="/api")

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

__all__ = ["app", "create_app", "lifespan"]
