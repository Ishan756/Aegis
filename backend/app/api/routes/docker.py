"""Docker deployment endpoints.

Local only. These endpoints build and run containers on the Docker daemon the
backend can see, and nothing here contacts AWS or pushes an image anywhere.
"""

from __future__ import annotations

from fastapi import APIRouter, status

from app.agents.docker_deployment import check_docker_availability, deploy_locally
from app.models.docker import (
    DeploymentResult,
    DockerAvailabilityResponse,
    DockerDeployRequest,
)

router = APIRouter(prefix="/docker", tags=["docker"])


@router.get(
    "/availability",
    response_model=DockerAvailabilityResponse,
    status_code=status.HTTP_200_OK,
    summary="Check Docker availability",
    description=(
        "Reports whether the Docker CLI is present, whether the daemon responds, "
        "which versions are running, and the directory that build contexts are "
        "confined to."
    ),
)
async def docker_availability() -> DockerAvailabilityResponse:
    """Report Docker availability.

    Returns 200 with ``available: false`` rather than an error status: the
    question was "is Docker usable", and that is an answer.
    """
    return await check_docker_availability()


@router.post(
    "/deploy",
    response_model=DeploymentResult,
    status_code=status.HTTP_200_OK,
    summary="Build and run a repository locally",
    description=(
        "Runs the deployment workflow against the local Docker daemon:\n\n"
        "`inspect → build → run → health check → collect logs`\n\n"
        "Every stage goes through the MCP tool layer, so the execution policy "
        "applies to each one. Building an image executes the Dockerfile's `RUN` "
        "steps and starting a container changes system state, so both require "
        "explicit approval: pass `approve: true` with an `approval_reference`, or "
        "the call is refused and reported rather than silently skipped. Use "
        "`dry_run: true` to inspect without building anything.\n\n"
        "A deployment is reported successful only when the container started *and* "
        "reported healthy. An image with no `HEALTHCHECK` is never counted as "
        "healthy."
    ),
)
async def deploy_endpoint(payload: DockerDeployRequest) -> DeploymentResult:
    """Build and run ``payload.image`` from ``payload.repository_path``.

    Async because each stage is an awaited MCP round trip to a subprocess. The
    response is 200 whether or not the deployment succeeded; the outcome is in
    the body, because "the container is unhealthy" is a result, not an error
    response.
    """
    return await deploy_locally(payload)
