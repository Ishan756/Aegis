"""Failure investigation and bounded recovery endpoints."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, status
from fastapi.responses import PlainTextResponse

from app.agents.investigation import investigate_failure
from app.agents.recovery_workflow import run_recovery
from app.core.config import get_settings
from app.models.incident import IncidentReport
from app.models.self_healing import RecoveryOutcome, SelfHealingPolicy
from app.models.verification import VerificationRequest

router = APIRouter(prefix="/deployment", tags=["recovery"])


class IncidentRequest(VerificationRequest):
    """A failed deployment to investigate.

    Extends the verification request so every field already used to *check* a
    deployment is also available to *diagnose* one. The container is named
    rather than looked up, because the verifier is what established that it
    exists at all.
    """

    image: str | None = None
    repository_path: str | None = None
    repository: str | None = None
    stop_reason: str | None = None
    stop_task_id: str | None = None


class RecoverRequest(IncidentRequest):
    """An incident, plus whether a human has approved an escalated fix.

    ``human_approved`` is the only thing that can promote an
    ``approval_required`` action. It is a request field rather than something the
    agent decides, which is what stops an agent approving its own remediation.
    """

    #: Overrides the configured policy. Absent means "use the service settings".
    policy: SelfHealingPolicy | None = None
    human_approved: bool = False
    #: Redeploy and re-verify between attempts. Off by default so a caller can
    #: inspect the diagnosis before anything is started again.
    verify_after_fix: bool = True


@router.post(
    "/incident",
    response_model=None,
    status_code=status.HTTP_200_OK,
    summary="Investigate a failed deployment",
    description=(
        "Collects evidence with read-only tools -- container status, health, logs, an "
        "HTTP probe, recent commits and the repository's own Dockerfile -- and "
        "returns an evidence-backed incident report.\n\n"
        "Every suspected root cause cites the observations that support it, and a "
        "cause with no evidence is rejected rather than reported. Confidence is "
        "derived from how many independent sources corroborate a cause, not "
        "asserted.\n\n"
        "Strictly read-only. Nothing here changes the deployment being diagnosed."
    ),
)
async def investigate_endpoint(
    payload: IncidentRequest,
    format: Literal["json", "markdown"] = "json",  # noqa: A002 - the documented query name
) -> IncidentReport | PlainTextResponse:
    """Investigate ``payload.container_name`` and report what the evidence shows."""
    from app.agents.deployment_verification import verify_deployment

    verification = None
    if payload.container_name:
        verification = await verify_deployment(payload)

    report = await investigate_failure(
        verification,
        container=payload.container_name,
        image=payload.image,
        repository_path=payload.repository_path,
        repository=payload.repository,
        stop_reason=payload.stop_reason,
        stop_task_id=payload.stop_task_id,
    )

    if format == "markdown":
        return PlainTextResponse(_report_markdown(report), media_type="text/plain; charset=utf-8")
    return report


@router.post(
    "/recover",
    response_model=None,
    status_code=status.HTTP_200_OK,
    summary="Attempt bounded automatic recovery",
    description=(
        "Runs the recovery loop: investigate, propose a fix, check its risk, apply it "
        "if policy allows, redeploy, and verify. Loops only while the configured "
        "attempt budget remains.\n\n"
        "Automatic application is limited to non-destructive actions -- retrying a "
        "deployment, retrying a transient operation, restarting a failed container. "
        "Image rebuilds need either `allow_rebuild` or `human_approved`. Code and "
        "configuration changes are never automatic under any setting, and there is "
        "no flag that enables them.\n\n"
        "Low confidence stops the loop and escalates. A refused action is recorded "
        "as an attempt that was considered and declined, because those are the "
        "attempts worth reading later."
    ),
)
async def recover_endpoint(payload: RecoverRequest) -> RecoveryOutcome:
    """Attempt recovery for ``payload.container_name`` within the configured bounds."""
    from app.agents.deployment_verification import verify_deployment

    policy = payload.policy or get_settings().self_healing.to_policy()

    verification = None
    if payload.container_name:
        verification = await verify_deployment(payload)

    async def verify() -> object:
        return await verify_deployment(payload)

    outcome = await run_recovery(
        policy,
        verification=verification,
        stop_reason=payload.stop_reason,
        stop_task_id=payload.stop_task_id,
        container=payload.container_name,
        image=payload.image,
        repository_path=payload.repository_path,
        repository=payload.repository,
        human_approved=payload.human_approved,
        verify=verify if payload.verify_after_fix else None,
        redeploy=None,
    )
    return outcome


def _report_markdown(report: IncidentReport) -> str:
    """Render an incident report for a human. Used by `?format=markdown`."""
    lines = [
        f"# Incident: {report.incident_id}",
        "",
        f"**Symptom:** {report.symptom}",
        f"**Component:** {report.affected_component}",
        f"**Confidence:** {report.confidence}",
        f"**Automatic fix safe:** {report.automatic_fix_safe}",
        "",
        "## Evidence",
    ]
    if report.evidence:
        lines.extend(f"- {item.summary()}" for item in report.evidence)
    else:
        lines.append("- None collected.")

    lines += ["", "## Suspected root causes"]
    if report.suspected_root_causes:
        for cause in report.suspected_root_causes:
            lines.append(f"### {cause.id} ({cause.confidence})")
            lines.append(f"{cause.cause}")
            lines.append("")
            lines.append(f"- Component: `{cause.component}`")
            if cause.confirm:
                lines.append(f"- Confirm by: {cause.confirm}")
            lines.append("- Cited evidence:")
            for item in cause.evidence:
                lines.append(f"  - {item.summary()}")
            lines.append("")
    else:
        lines += [
            "None established. The available evidence matched no known failure "
            "signature, which is reported rather than filled in with a guess."
        ]

    lines += ["", "## Recommendation"]
    if report.recommended_fix:
        fix = report.recommended_fix
        lines.append(f"- **{fix.action}** — {fix.description}")
        lines.append(f"- Rationale: {fix.rationale}")
        if fix.requires_approval:
            lines.append("- Requires approval: yes")
    else:
        lines.append("- None proposed.")

    lines += ["", "## Next action", report.next_action, ""]
    return "\n".join(lines)


__all__ = ["IncidentRequest", "RecoverRequest", "router"]
