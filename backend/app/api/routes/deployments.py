"""Deployment history endpoints.

Read-only. History is a record of what happened; an API that can edit it is an API
whose record cannot be trusted as evidence.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Query, status
from fastapi.responses import PlainTextResponse

from app.core.exceptions import NotFoundError
from app.memory import get_memory_service
from app.models.deployment_record import (
    DeploymentDetail,
    DeploymentListResponse,
    DeploymentRecord,
    DeploymentStatus,
    FailureStage,
)

router = APIRouter(prefix="/deployments", tags=["deployments"])


@router.get(
    "",
    response_model=DeploymentListResponse,
    status_code=status.HTTP_200_OK,
    summary="List deployment history",
    description=(
        "Deployments newest-first, with a row per run: status, commit, image, and "
        "how many actions and failures it produced.\n\n"
        "Rows are summaries on purpose. A deployment's plan and full action log are "
        "large, and a history list is read far more often than a history detail — "
        "returning both in every row would make the list expensive for data nobody "
        "looks at. Fetch one deployment for the trace.\n\n"
        "Bounded and offset-paged. `limit` is capped at 200; an unbounded history "
        "endpoint is a slow query wearing a costume."
    ),
)
async def list_deployments_endpoint(
    repository: Annotated[
        str | None,
        Query(description="Filter by `owner/name` or the path that was deployed."),
    ] = None,
    # Aliased because `status` shadows the imported `status` module inside this
    # function, and the query name is the one clients read.
    status_filter: Annotated[
        DeploymentStatus | None,
        Query(alias="status", description="Filter by final status."),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> DeploymentListResponse:
    """Return one page of deployment history."""
    from app.models.deployment_record import DeploymentQuery

    return await get_memory_service().list_deployments(
        DeploymentQuery(
            repository=repository,
            status=status_filter,
            limit=limit,
            offset=offset,
        )
    )


@router.get(
    "/{deployment_id}",
    response_model=DeploymentDetail,
    status_code=status.HTTP_200_OK,
    summary="Read one deployment's execution trace",
    description=(
        "Everything recorded for a single deployment: the plan, every action taken, "
        "the verification result, the failures from each stage, any recovery "
        "attempts, the investigation that followed a failure, and the lessons it "
        "produced.\n\n"
        "Failures from PLAN, EXECUTE, VERIFY, INVESTIGATE and RECOVER are flattened "
        "into one list so a trace can be read top to bottom instead of by stage.\n\n"
        "`?format=markdown` renders the trace for a human."
    ),
)
async def get_deployment_endpoint(
    deployment_id: str,
    format: Literal["json", "markdown"] = "json",  # noqa: A002 - the documented query name
) -> DeploymentDetail | PlainTextResponse:
    """Return one deployment with its full trace."""
    service = get_memory_service()
    detail = await service.get_deployment(deployment_id)
    if detail is None:
        raise NotFoundError(
            "No deployment with that id was recorded.",
            details={"deployment_id": deployment_id},
        )

    if format == "markdown":
        return PlainTextResponse(_trace_markdown(detail), media_type="text/plain; charset=utf-8")
    return detail


def _trace_markdown(detail: DeploymentDetail) -> str:
    """Render a trace for a human reading a terminal or a diff."""
    record: DeploymentRecord = detail
    lines = [
        f"# Deployment `{record.deployment_id}`",
        "",
        f"**Status:** {record.status}"
        + (" (recovered)" if record.recovered else "")
        + (" (dry run)" if record.dry_run else ""),
        f"**Repository:** {record.repository or record.repository_path or 'unknown'}",
        f"**Commit:** {record.commit_sha or record.commit_ref or 'unknown'}",
        f"**Image:** {record.image or 'n/a'}",
        f"**Container:** {record.container or 'n/a'}",
    ]
    if record.escalated:
        lines.append(f"**Escalated:** {record.escalation_reason or 'yes'}")
    if record.error:
        lines.append(f"**Error:** {record.error}")
    if record.started_at:
        lines.append(f"**Started:** {record.started_at.isoformat()}")
    if record.finished_at:
        lines.append(f"**Finished:** {record.finished_at.isoformat()}")
        lines.append(f"**Duration:** {record.duration_seconds}s")

    lines += ["", "## Actions", ""]
    if record.actions:
        for action in record.actions:
            duration = f"{action.duration_seconds:.2f}s" if action.duration_seconds else "-"
            error = f" — {action.error}" if action.error else ""
            lines.append(f"{action.sequence}. `{action.tool}` {action.status} ({duration}){error}")
    else:
        lines.append("None recorded.")

    lines += ["", "## Verification", ""]
    if record.verification is None:
        lines.append("Not run.")
    else:
        lines.append(f"**{record.verification.status}**")
        for check in record.verification.checks:
            detail = f" — {check.detail}" if check.detail else ""
            lines.append(f"- `{check.name}`: {check.outcome}{detail}")

    lines += ["", "## Failures", ""]
    if record.failures:
        for failure in record.failures:
            lines.append(f"- **{failure.stage}/{failure.kind}** — {failure.message}")
    else:
        lines.append("None recorded.")

    if record.recovery_attempts:
        lines += ["", "## Recovery attempts", ""]
        for attempt in record.recovery_attempts:
            lines.append(
                f"- {attempt.get('index', '?')}. {attempt.get('action', '?')} "
                f"applied={attempt.get('applied')} succeeded={attempt.get('succeeded')}"
            )

    if detail.lessons:
        lines += ["", "## Lessons", ""]
        for lesson in detail.lessons:
            lines.append(f"- **{lesson.title}** (seen {lesson.occurrences}x) — {lesson.summary}")

    lines.append("")
    return "\n".join(lines)


__all__ = ["FailureStage", "router"]
