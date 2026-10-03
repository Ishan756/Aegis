"""Unified deployment planning endpoints."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, status
from fastapi.responses import PlainTextResponse

from app.agents.deployment_planner import plan_repository_deployment
from app.models.deployment_plan import (
    DeploymentPlanRequest,
    DeploymentPlanResponse,
)

router = APIRouter(prefix="/deployment", tags=["deployment"])


@router.post(
    "/plan",
    response_model=None,
    status_code=status.HTTP_200_OK,
    summary="Produce a unified deployment plan for a GitHub repository",
    description=(
        "Combines repository analysis, DevOps planning and risk assessment into a single "
        "plan: detected stack, build, test, deployment, health-check and rollback "
        "strategies, required environment variables and services, risks, approval "
        "requirements and ordered steps.\n\n"
        "Decisions come from the repository profile rather than from guesswork. A "
        "Dockerfile means a container build; its absence produces a *recommendation* to "
        "add one, and nothing is written to the repository. A missing test suite is "
        "reported as limited testing, never as a pass. An unresolved required environment "
        "variable blocks the deployment until it is supplied.\n\n"
        "Read-only throughout. By default the response carries both a machine-readable "
        "`plan` object and a human-readable `summary_markdown` rendered from it, so the "
        "two cannot disagree. Pass `?format=markdown` to receive the rendered form alone "
        "as `text/plain`."
    ),
)
async def plan_deployment_endpoint(
    payload: DeploymentPlanRequest,
    format: Literal["json", "markdown"] = "json",  # noqa: A002 - the documented query name
) -> DeploymentPlanResponse | PlainTextResponse:
    """Plan a deployment for ``payload.owner``/``payload.repository``.

    Async because every repository interaction is an awaited MCP round trip to a
    subprocess.
    """
    plan = await plan_repository_deployment(payload)
    summary = plan.summary_markdown()

    if format == "markdown":
        return PlainTextResponse(summary, media_type="text/plain; charset=utf-8")

    return DeploymentPlanResponse(plan=plan, summary_markdown=summary)
