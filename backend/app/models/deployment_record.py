"""Persistent deployment history.

A deployment is the unit of history. One record per workflow run, written when the
run starts and rewritten as each stage produces something worth keeping, so an
interrupted run is still visible rather than missing entirely.

Nested structures reuse the domain models they came from — an
:class:`~app.models.verification.VerificationResult` is stored as that type, not
as a parallel "persistence shape". Two schemas that have to be kept in step is one
more than a record needs; a model change is then automatically a storage change.

The cost of that choice is that history is stored as JSON rather than as
normalised rows, so a change to a domain model changes the shape of records
already written. That is acceptable for an audit trail read as a document, and it
is why these tables are not a source of truth for anything but history.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field, model_validator

from app.models.deployment_plan import RepositoryDeploymentPlan
from app.models.deployment_target import DeploymentTarget
from app.models.execution import ExecutionAction
from app.models.incident import IncidentReport
from app.models.verification import VerificationResult


class DeploymentStatus(StrEnum):
    """How a deployment ended.

    ``DEGRADED`` is a real outcome and is not collapsed into success. A deployment
    that is serving but unproven — a verification warning, or one that only
    verified after a recovery attempt — has not been shown to work, and reporting
    it as success is the sort of gap this whole project exists to close.
    """

    #: Still running, or interrupted before it finished.
    IN_PROGRESS = "in_progress"
    #: Verified working.
    SUCCEEDED = "succeeded"
    #: Serving, but something could not be confirmed.
    DEGRADED = "degraded"
    #: Not working.
    FAILED = "failed"
    #: Nothing was deployed, by request.
    DRY_RUN = "dry_run"


class FailureStage(StrEnum):
    """Which part of the workflow a failure came from."""

    PLAN = "plan"
    EXECUTE = "execute"
    VERIFY = "verify"
    INVESTIGATE = "investigate"
    RECOVER = "recover"


class FailureRecord(BaseModel):
    """One failure, flattened out of whichever stage produced it.

    The stages record failures in their own shapes — a task result, a verification
    check, an incident cause. History needs them side by side and sortable, so they
    are normalised into this without pretending the stages agree on what a failure
    is.
    """

    stage: FailureStage
    #: Machine-readable kind, e.g. ``tool_error``, ``out_of_memory``.
    kind: str
    message: str
    detail: str | None = None
    task_id: str | None = None
    tool: str | None = None
    occurred_at: datetime | None = None


class Lesson(BaseModel):
    """Something learned from a deployment, reusable by later ones.

    ``fingerprint`` is the identity of the lesson, not of the occurrence: the same
    root cause in the same repository and component produces the same fingerprint,
    so repeats increment ``occurrences`` on one row instead of accumulating
    duplicates. That is what makes the store useful — the second time a repository
    fails the same way, the lesson says so with a count behind it.

    Fingerprints are stable strings rather than embeddings, so the same row can
    later carry a vector column without callers changing. Nothing here depends on
    that column existing.
    """

    lesson_id: str
    fingerprint: str
    title: str
    summary: str
    detail: str | None = None
    repository: str | None = None
    component: str | None = None
    #: The incident cause or failure signature this came from, e.g. ``out_of_memory``.
    cause_id: str | None = None
    severity: str = "warning"
    evidence: list[str] = Field(default_factory=list)
    recommendation: str | None = None
    tags: list[str] = Field(default_factory=list)
    occurrences: int = Field(default=1, ge=1)
    #: Deployments this lesson was seen in, most recent last. Provenance: a lesson
    #: without it is a claim nobody can check.
    deployment_ids: list[str] = Field(default_factory=list)
    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class DeploymentRecord(BaseModel):
    """One deployment, as history.

    Written at the start of a run with ``IN_PROGRESS`` and updated as stages
    complete, so an interrupted run is recorded as interrupted rather than
    absent.
    """

    deployment_id: str
    run_id: str | None = None
    #: ``owner/name`` when the repository was resolved from GitHub.
    repository: str | None = None
    #: The local path that was deployed, as given in the request.
    repository_path: str | None = None
    commit_sha: str | None = None
    commit_ref: str | None = None
    image: str | None = None
    container: str | None = None
    #: Where it ran. None for the local daemon, which predates this field and
    #: stays absent rather than being relabelled after the fact.
    target: DeploymentTarget | None = None

    status: DeploymentStatus = DeploymentStatus.IN_PROGRESS
    #: Failed verification, then recovered. Kept separate from ``status`` because
    #: "succeeded" and "succeeded after something broke" are different facts.
    recovered: bool = False
    escalated: bool = False
    escalation_reason: str | None = None
    dry_run: bool = False
    error: str | None = None

    #: The analysed plan, when one was produced by the planner.
    plan: RepositoryDeploymentPlan | None = None
    #: The ordered plan this run actually executed. Distinct from ``actions``:
    #: this is what was intended, ``actions`` is what happened, including retries
    #: and skips. A trace that conflates them cannot show the difference between a
    #: plan that was followed and one that was quietly abandoned.
    tasks: list[dict] = Field(default_factory=list)
    actions: list[ExecutionAction] = Field(default_factory=list)
    verification: VerificationResult | None = None
    failures: list[FailureRecord] = Field(default_factory=list)
    recovery_attempts: list[dict] = Field(default_factory=list)
    incident: IncidentReport | None = None
    #: The request as received, so a trace can be read without guessing what was asked for.
    request: dict | None = None

    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_seconds: float | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @model_validator(mode="after")
    def _timestamps_ordered(self) -> DeploymentRecord:
        """A record whose end precedes its start is a bug, not a slow deployment."""
        if (
            self.started_at is not None
            and self.finished_at is not None
            and self.finished_at < self.started_at
        ):
            raise ValueError("finished_at precedes started_at")
        return self

    @property
    def terminal(self) -> bool:
        return self.status is not DeploymentStatus.IN_PROGRESS


class DeploymentSummary(BaseModel):
    """A list row: enough to identify a run, none of the weight.

    History lists are read far more often than history details. Returning the plan
    and full action log for every row would make the list endpoint expensive for
    data nobody looks at while scrolling.
    """

    deployment_id: str
    repository: str | None = None
    repository_path: str | None = None
    commit_sha: str | None = None
    commit_ref: str | None = None
    image: str | None = None
    container: str | None = None
    target: DeploymentTarget | None = None
    status: DeploymentStatus
    recovered: bool = False
    escalated: bool = False
    escalation_reason: str | None = None
    dry_run: bool = False
    action_count: int = 0
    failure_count: int = 0
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_seconds: float | None = None

    @classmethod
    def of(cls, record: DeploymentRecord) -> DeploymentSummary:
        return cls(
            deployment_id=record.deployment_id,
            repository=record.repository,
            repository_path=record.repository_path,
            commit_sha=record.commit_sha,
            commit_ref=record.commit_ref,
            image=record.image,
            container=record.container,
            target=record.target,
            status=record.status,
            recovered=record.recovered,
            escalated=record.escalated,
            escalation_reason=record.escalation_reason,
            dry_run=record.dry_run,
            action_count=len(record.actions),
            failure_count=len(record.failures),
            started_at=record.started_at,
            finished_at=record.finished_at,
            duration_seconds=record.duration_seconds,
        )


class DeploymentQuery(BaseModel):
    """List filters.

    Bounded rather than open-ended: a history endpoint with no ceiling returns
    every deployment ever run, which is a slow query wearing a costume.
    """

    repository: str | None = None
    status: DeploymentStatus | None = None
    limit: int = Field(default=50, ge=1, le=200)
    offset: int = Field(default=0, ge=0)


class DeploymentListResponse(BaseModel):
    items: list[DeploymentSummary]
    total: int
    limit: int
    offset: int


class DeploymentDetail(DeploymentRecord):
    """A record plus the lessons it produced.

    Lessons ride along here rather than behind their own endpoint: they are only
    meaningful with the deployment in front of you, and a separate endpoint would
    be a second place for them to be looked up and forgotten.
    """

    lessons: list[Lesson] = Field(default_factory=list)


class MemoryHealth(BaseModel):
    """Whether the memory store can be reached."""

    backend: str
    status: str
    detail: str | None = None
    deployments: int | None = None
    lessons: int | None = None


__all__ = [
    "DeploymentDetail",
    "DeploymentListResponse",
    "DeploymentQuery",
    "DeploymentRecord",
    "DeploymentStatus",
    "DeploymentSummary",
    "DeploymentTarget",
    "FailureRecord",
    "FailureStage",
    "Lesson",
    "MemoryHealth",
]
