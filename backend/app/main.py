"""Aegis backend application entrypoint."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.errors import register_exception_handlers
from app.api.router import api_router
from app.api.routes import agent, mcp, repository
from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.core.middleware import RequestLoggingMiddleware
from app.models.health import RootResponse
from app.services.mcp_manager import MCPClientManager, set_manager
from app.services.status import StatusService

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Manage application startup and shutdown.

    MCP sessions are opened here rather than per request: each server is a
    subprocess, so reconnecting per request would pay process-spawn cost on every
    call and could leave orphaned children. The manager is torn down on shutdown so
    no server process outlives the app.
    """
    settings = get_settings()
    manager = MCPClientManager(settings.mcp)
    await manager.connect()
    app.state.mcp_manager = manager
    set_manager(manager)

    # Built after the manager so health can report which servers actually
    # connected, rather than only whether MCP was enabled.
    app.state.status_service = StatusService(settings, app)

    logger.info(
        "Aegis backend starting",
        # safe_summary() only reports whether credentials are present, never
        # their values, so this is safe to log.
        extra={
            "settings": settings.safe_summary(),
            "mcp_servers": manager.connected_servers,
        },
    )
    try:
        yield
    finally:
        set_manager(None)
        await manager.close()
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

    # Agent routes are pre-1.0 and deliberately unversioned, so planning and
    # repository analysis live at /api/... rather than /api/v1/.... Move them
    # under the versioned prefix once the shapes settle.
    app.include_router(agent.router, prefix="/api")
    app.include_router(repository.router, prefix="/api")
    app.include_router(mcp.router, prefix="/api")

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
