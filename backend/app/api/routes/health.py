"""Health and readiness endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Response, status

from app.api.deps import StatusServiceDep
from app.models.health import HealthResponse, LivenessResponse

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse, summary="Service health")
def get_health(response: Response, service: StatusServiceDep) -> HealthResponse:
    """Return aggregate service health.

    Responds with HTTP 503 when a component reports ``error`` so container
    orchestrators can act on it.
    """
    health = service.health()
    if health.status == "error":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return health


@router.get("/health/live", response_model=LivenessResponse, summary="Liveness probe")
def get_liveness() -> LivenessResponse:
    """Return 200 as long as the process is able to serve requests."""
    return LivenessResponse()
