"""Shared FastAPI dependencies."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from app.core.config import Settings, get_settings
from app.services.status import StatusService


def get_status_service(request: Request) -> StatusService:
    """Return the process-wide :class:`StatusService`.

    Constructed during startup and stored on ``app.state`` so uptime is
    measured from process boot rather than from the first request.
    """
    return request.app.state.status_service


def get_app_settings() -> Settings:
    """Return the cached application settings."""
    return get_settings()


StatusServiceDep = Annotated[StatusService, Depends(get_status_service)]
SettingsDep = Annotated[Settings, Depends(get_app_settings)]
