"""Endpoints for the execution engine and the deployment workflow."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, status
from fastapi.responses import PlainTextResponse

from app.agents.deployment_workflow import run_deployment_workflow
from app.agents.execution_engine import execute_run
from app.core.config import get_settings
from app.models.docker import DockerDeployRequest
from app.models.execution import ExecutionRequest, ExecutionResponse

router = APIRouter(prefix="/deployment", tags=["deployment"])


@router.post(
    "/execute",
    response_model=None,
    status_code=status.HTTP_200_OK,
    summary="Execute a validated task list through MCP tools",
    description=(
        "Runs tasks sequentially in the order given, applying the tool execution "
        "policy to every call before the tool is touched.\n\n"
        "Every attempt is recorded as an action -- timestamp, task, tool, redacted "
        "arguments, status, duration, error and retry count -- whether it succeeded "
        "or not. Only transient failures are retried; a policy refusal is answered "
        "by a human, so retrying identical arguments would only produce identical "
        "refusals. A failed critical task stops the run rather than letting later "
        "tasks build on a broken foundation.\n\n"
        "Argument values are redacted by key name before they are recorded, so a "
        "credential cannot reach a durable log. Pass `?format=markdown` for the "
        "rendered form, including the full audit trail."
    ),
)
async def execute_tasks_endpoint(
    payload: ExecutionRequest,
    format: Literal["json", "markdown"] = "json",  # noqa: A002 - the documented query name
) -> ExecutionResponse | PlainTextResponse:
    """Execute ``payload.tasks`` in order."""
    response = await execute_run(payload)

    if format == "markdown":
        return PlainTextResponse(
            response.summary_markdown(), media_type="text/plain; charset=utf-8"
        )
    return response


@router.post(
    "/workflow",
    response_model=None,
    status_code=status.HTTP_200_OK,
    summary="Run the local deployment workflow end to end",
    description=(
        "Runs PLAN → EXECUTE → VERIFY → END against the local Docker daemon.\n\n"
        "PLAN derives the task list, EXECUTE runs it through policy-checked MCP "
        "tools with a full audit trail, and VERIFY proves the result with seven "
        "read-only checks. A stopped execution or a non-SUCCESS verification routes "
        "to DEBUG, which classifies the failure and proposes next steps.\n\n"
        "DEBUG applies nothing: it produces hypotheses and proposals only, because "
        "self-healing needs its own blast-radius analysis and approval gate.\n\n"
        "Mutating tools require `approve: true`. The workflow never manufactures "
        "approval on the caller's behalf. No AWS and no registry push."
    ),
)
async def run_workflow_endpoint(
    payload: DockerDeployRequest,
    format: Literal["json", "markdown"] = "json",  # noqa: A002 - the documented query name
) -> dict[str, Any] | PlainTextResponse:
    """Plan, execute and verify one local deployment."""
    # From settings, not a request field: the bounds on automatic recovery are a
    # property of the deployment, not something each caller supplies. Disabled by
    # default, so an unmodified install still diagnoses and escalates.
    #
    # `human_approved` is deliberately left False. This endpoint cannot approve on
    # a caller's behalf; a human authorises an escalated fix through
    # `POST /api/deployment/recover`, where the approval is explicit.
    outcome: dict[str, Any] = await run_deployment_workflow(
        payload,
        self_healing_policy=get_settings().self_healing.to_policy(),
        self_healing_human_approved=False,
    )

    if format == "markdown":
        lines = ["# Deployment workflow", ""]
        if outcome.get("plan_explanation"):
            lines += [str(outcome["plan_explanation"]), ""]
        execution = outcome.get("execution")
        if execution is not None:
            lines += [execution.summary_markdown(), ""]
        verification = outcome.get("verification")
        if verification is not None:
            lines += [verification.summary_markdown(), ""]
        debug = outcome.get("debug")
        if debug is not None:
            lines += [
                f"# Debug (proposals only, nothing applied)\n\n{debug.summary}\n",
                "",
            ]
            if debug.hypotheses:
                lines += ["## Hypotheses", ""]
                for hypothesis in debug.hypotheses:
                    lines.append(f"- **{hypothesis.summary}** ({hypothesis.confidence})")
                    lines.append(f"  - Test: {hypothesis.test}")
                lines.append("")
            if debug.proposals:
                lines += ["## Proposals (not applied)", ""]
                for proposal in debug.proposals:
                    lines.append(
                        f"- {proposal.action} — {proposal.rationale} "
                        f"[{proposal.tool or 'manual'}, approval required]"
                    )
                lines.append("")
        incident = outcome.get("incident")
        if incident is not None:
            lines += [f"# Incident\n\n{incident.next_action}\n", ""]
            lines += ["## Evidence", ""]
            for item in incident.evidence:
                lines.append(f"- {item.summary()}")
            lines.append("")
            lines += ["## Suspected root causes", ""]
            for cause in incident.suspected_root_causes or []:
                lines.append(f"- **{cause.id}** ({cause.confidence}) — {cause.cause}")
            lines.append("")
        recovery = outcome.get("recovery")
        if recovery is not None:
            lines += [f"# Recovery — {recovery.summary()}\n", ""]
            lines += ["## Attempts", ""]
            for attempt in recovery.attempts:
                lines.append(f"- {attempt.summary()}")
            if recovery.next_action:
                lines += ["", f"Next: {recovery.next_action}"]
            lines.append("")
        return PlainTextResponse("\n".join(lines), media_type="text/plain; charset=utf-8")

    return outcome
