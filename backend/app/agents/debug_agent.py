"""Debug agent placeholder.

A failed deployment currently ends here. This module exists so the failure has a
home and so the graph's shape is real before any remediation logic exists.

**It proposes, it never acts.** Every field is a description of what a human or a
future agent could investigate. There is deliberately no code path here that
restarts a container, rolls back an image, edits a file or touches infrastructure.
Self-healing is a later, separate decision that needs its own blast-radius
analysis and its own approval gate -- folding it in here would mean the first
version of "fix it automatically" shipped without anyone choosing it.

When the logic does arrive, two rules are already fixed by this design:

- A remediation proposal must name the evidence that motivated it. A fix chosen
  without evidence is a guess, and guesses that touch infrastructure are
  expensive.
- Nothing is applied. ``applied`` and ``applied_action`` are reporting fields, and
  nothing in this module ever sets them to a value meaning "done".
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.models.verification import VerificationResult

logger = logging.getLogger(__name__)

#: What kind of failure the debug agent was handed.
FailureKind = Literal["verification_failed", "execution_stopped", "unknown"]


class Hypothesis(BaseModel):
    """A candidate cause, with the evidence that suggests it and the test for it."""

    summary: str
    evidence: list[str] = Field(default_factory=list)
    test: str = Field(description="How a human could confirm or rule this out.")
    confidence: Literal["low", "medium", "high"] = "low"


class RemediationProposal(BaseModel):
    """Something that could be done. Never applied by this module."""

    action: str
    rationale: str
    tool: str | None = Field(default=None, description="MCP tool this would call. Not called here.")
    requires_approval: bool = True
    reversible: bool = Field(
        default=False,
        description="False for anything that destroys state, such as a rebuild.",
    )


class DebugAssessment(BaseModel):
    """Why the deployment failed, and what could be done about it. Read-only."""

    kind: FailureKind = "unknown"
    summary: str
    container: str | None = None
    status: str | None = None
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    proposals: list[RemediationProposal] = Field(default_factory=list)
    next_actions: list[str] = Field(default_factory=list)

    #: Always false. Present so a caller can assert the agent did not act, and so
    #: the field exists to be flipped in the future *deliberately*.
    applied: bool = False
    applied_action: str | None = None

    assessed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    placeholder: bool = Field(
        default=True,
        description="True while this agent only classifies. Diagnosis logic is not implemented.",
    )


def _hypotheses_for(result: VerificationResult) -> list[Hypothesis]:
    """Turn failed checks into candidate causes.

    Each hypothesis names the check that motivated it, so a reader can disagree
    with the reasoning rather than having to reverse-engineer it.
    """
    hypotheses: list[Hypothesis] = []

    for check in result.checks:
        if check.outcome not in {"fail", "warn"}:
            continue
        detail = check.detail or check.skipped_reason or ""

        if check.name == "container_exists":
            hypotheses.append(
                Hypothesis(
                    summary="The container was never created, or was removed.",
                    evidence=[detail],
                    test="List containers matching the expected name.",
                    confidence="high",
                )
            )
        elif check.name == "container_running":
            hypotheses.append(
                Hypothesis(
                    summary="The container started and exited, possibly immediately.",
                    evidence=[detail],
                    test="Read the container's exit code and the tail of its log.",
                    confidence="high",
                )
            )
        elif check.name in {"port_available", "health_endpoint", "health_status_code"}:
            hypotheses.append(
                Hypothesis(
                    summary=(
                        "The process is running but not serving the expected port, "
                        "or it is serving a different path."
                    ),
                    evidence=[detail],
                    test=(
                        "Compare the container's published ports with the application's "
                        "listen address, then curl the path directly."
                    ),
                    confidence="medium",
                )
            )
        elif check.name == "logs_clean":
            hypotheses.append(
                Hypothesis(
                    summary="The application is failing at startup, most often on a dependency.",
                    evidence=[detail],
                    test="Read the cited log lines and the first exception in the log.",
                    confidence="medium",
                )
            )
        elif check.name == "dependencies_reachable":
            hypotheses.append(
                Hypothesis(
                    summary=(
                        "A required backing service is absent or not yet accepting connections."
                    ),
                    evidence=[detail],
                    test="Check whether the dependency's container is running and listening.",
                    confidence="low",
                )
            )

    return hypotheses


def _proposals_for(result: VerificationResult) -> list[RemediationProposal]:
    """Candidate next actions. None of them are performed here."""
    failed = {check.name for check in result.checks if check.outcome == "fail"}
    proposals: list[RemediationProposal] = []

    if "container_exists" in failed:
        proposals.append(
            RemediationProposal(
                action="Re-run the deployment to create the container.",
                rationale="The container does not exist, so there is nothing to inspect.",
                tool="docker.build_image",
            )
        )

    if "container_running" in failed:
        proposals.append(
            RemediationProposal(
                action="Read the exit code and full log before restarting.",
                rationale="Restarting first would discard the evidence of why it exited.",
                tool="docker.container_logs",
            )
        )

    if failed & {"port_available", "health_endpoint", "health_status_code"}:
        proposals.append(
            RemediationProposal(
                action="Compare the published ports with the application's listen address.",
                rationale="A running process that does not answer is usually a port mismatch.",
                tool="docker.container_status",
            )
        )

    if "logs_clean" in failed:
        proposals.append(
            RemediationProposal(
                action="Identify the first fatal line and fix the underlying error.",
                rationale="Log evidence usually names the failing dependency or call directly.",
                tool="docker.container_logs",
            )
        )

    if result.container:
        proposals.append(
            RemediationProposal(
                action="Stop and remove the failed container before retrying.",
                rationale="A stopped container keeps its name, so a retry would collide with it.",
                tool="docker.stop_container",
            )
        )

    return proposals


async def assess_failure(
    result: VerificationResult | None = None,
    *,
    stop_reason: str | None = None,
    stop_task_id: str | None = None,
) -> DebugAssessment:
    """Classify a failure and propose next steps. Acts on nothing.

    ``result`` is a *failed* verification. ``stop_reason`` describes an execution
    that stopped before verification could run. Both are recorded, because an
    execution failure and a verification failure need different investigations.
    """
    if result is not None:
        assessment = DebugAssessment(
            kind="verification_failed",
            summary=(
                f"Verification returned {result.status} for "
                f"{result.container or 'the deployment'}: "
                f"{len(result.failures)} failure(s), {len(result.warnings)} warning(s)."
            ),
            container=result.container,
            status=result.status,
            hypotheses=_hypotheses_for(result),
            evidence=[
                f"{source.source}: {source.detail or source.value or ''}".strip()
                for source in result.evidence
            ],
            proposals=_proposals_for(result),
            next_actions=[
                "Read the failing checks and their evidence.",
                "Confirm the cause before changing anything.",
                "Apply any remediation through a normal, approval-gated run.",
            ],
        )
    elif stop_reason:
        assessment = DebugAssessment(
            kind="execution_stopped",
            summary=f"Execution stopped at {stop_task_id or 'an unknown task'}: {stop_reason}",
            hypotheses=[
                Hypothesis(
                    summary="A planned task failed, so later tasks were never attempted.",
                    evidence=[stop_reason],
                    test="Read the recorded actions for the failing task in the audit trail.",
                    confidence="high",
                )
            ],
            evidence=[stop_reason],
            proposals=[
                RemediationProposal(
                    action="Resolve the blocking condition the stopped task reported.",
                    rationale="The run halted deliberately rather than building on a broken step.",
                    tool=None,
                )
            ],
            next_actions=[
                "Review the execution audit trail.",
                "Resolve the blocking risk or missing prerequisite.",
                "Re-run once the cause is understood.",
            ],
        )
    else:
        assessment = DebugAssessment(
            kind="unknown",
            summary="No verification result or stop reason was supplied.",
            next_actions=["Supply a verification result or an execution stop reason."],
        )

    logger.info(
        "failure assessed",
        extra={
            "kind": assessment.kind,
            "hypotheses": len(assessment.hypotheses),
            "proposals": len(assessment.proposals),
            "applied": assessment.applied,
        },
    )
    return assessment


__all__ = [
    "DebugAssessment",
    "FailureKind",
    "Hypothesis",
    "RemediationProposal",
    "assess_failure",
]
