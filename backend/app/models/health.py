"""Health and status response schemas.

These are the wire contract consumed by the frontend dashboard; keep them in
sync with ``frontend/src/types/health.ts``.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

ComponentStatus = Literal["ok", "degraded", "not_configured", "error"]


class ComponentHealth(BaseModel):
    """State of a single subsystem."""

    name: str = Field(description="Subsystem identifier, e.g. 'database'.")
    status: ComponentStatus = Field(description="Current subsystem state.")
    detail: str | None = Field(
        default=None, description="Human-readable explanation of the status."
    )


class HealthResponse(BaseModel):
    """Aggregate health of the Aegis backend."""

    status: ComponentStatus = Field(description="Overall service state.")
    service: str
    version: str
    environment: str
    uptime_seconds: float = Field(ge=0, description="Seconds since process start.")
    stage: str = Field(
        description="Development stage the codebase currently implements.",
    )
    components: list[ComponentHealth] = Field(
        default_factory=list, description="Per-subsystem breakdown."
    )


class LivenessResponse(BaseModel):
    """Minimal liveness probe payload for orchestrators."""

    alive: Literal[True] = True


class RootResponse(BaseModel):
    """Service banner served from ``/``."""

    service: str
    version: str
    docs: str
