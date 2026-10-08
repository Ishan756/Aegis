"""EC2 deployment endpoint."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, status
from fastapi.responses import PlainTextResponse

from app.agents.ec2_deployment import run_ec2_deployment
from app.models.ec2_deployment import EC2DeploymentRequest, EC2DeploymentResult

router = APIRouter(prefix="/deployment", tags=["deployment"])


def _markdown(result: EC2DeploymentResult) -> str:
    """Render the outcome for a human, in the same order the run happened."""
    target = result.target
    instance_id = target.instance_id or "unknown"
    region = target.region or "unknown region"
    lines = [
        "# EC2 deployment",
        "",
        f"**Instance:** `{instance_id}` ({region})  ",
        f"**Host:** `{target.host}` as `{target.ssh_user}`  ",
        f"**Result:** {'succeeded' if result.succeeded else 'did not succeed'}"
        + (f" — {result.stop_reason}" if result.stop_reason else ""),
        "",
        "| Step | Stage | Outcome | Detail |",
        "| --- | --- | --- | --- |",
    ]
    for step in result.steps:
        lines.append(f"| {step.stage} | {step.stage} | {step.outcome} | {step.detail} |")

    if result.plan_explanation:
        lines += ["", f"**Plan:** {result.plan_explanation}"]
    execution = result.execution
    if execution is not None:
        lines += ["", execution.summary_markdown()]
    verification = result.verification
    if verification is not None:
        lines += ["", verification.summary_markdown()]
    if result.notes:
        lines += ["", "## Notes", ""]
        lines += [f"- {note}" for note in result.notes]
    return "\n".join(lines)


@router.post(
    "/ec2",
    response_model=None,
    status_code=status.HTTP_200_OK,
    summary="Deploy a repository to the configured EC2 instance",
    description=(
        "Runs the same PLAN → EXECUTE → VERIFY workflow as a local deployment, "
        "with the Docker daemon reached over SSH on the instance named by "
        "`AEGIS_EC2__*` configuration. The target itself is not part of the "
        "request: a caller chooses what to build and whether to approve, never "
        "where it goes.\n\n"
        "The instance is checked over SSH first, and Docker is installed or "
        "started there only when `approve: true` accompanies the request — those "
        "commands do not pass through the MCP policy, so this flag is their "
        "approval gate. Through the MCP, `build_image` and `start_container` are "
        "still refused without approval, exactly as they are locally.\n\n"
        "Verification probes the health endpoint through the instance's public "
        "address and, as additional evidence, from inside the instance itself, so "
        "a closed security-group port can be told apart from a broken "
        "application. History records the target alongside the usual plan, "
        "execution and verification."
    ),
)
async def deploy_to_ec2_endpoint(
    payload: EC2DeploymentRequest,
    format: Literal["json", "markdown"] = "json",  # noqa: A002 - the documented query name
) -> EC2DeploymentResult | PlainTextResponse:
    """Plan, execute and verify one deployment on the configured instance."""
    result = await run_ec2_deployment(payload)

    if format == "markdown":
        return PlainTextResponse(_markdown(result), media_type="text/plain; charset=utf-8")
    return result
