"""Docker deployment contracts.

The request describes *what* to deploy locally; the response records what
actually happened, including anything that went wrong. The shape is deliberately
flat and auditable: every mutating step names the tool it called and whether it
was approved, so a deployment can be reconstructed after the fact.

No model in this module carries a credential. Logs are included because they are
the point of the final step, but they arrive already capped by the MCP server.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, TypedDict

from pydantic import BaseModel, Field, field_validator

from app.models.repository import RepositoryProfile

DeploymentStage = Literal[
    "inspect",
    "build",
    "run",
    "health",
    "logs",
]

StepOutcome = Literal["ok", "skipped", "failed", "refused", "unhealthy"]


class DockerDeployRequest(BaseModel):
    """Body of ``POST /api/docker/deploy``.

    The path is resolved against the configured repository root and then against
    the Docker context root, both of which are enforced server-side; the values
    here are never trusted as absolute.
    """

    repository_path: str = Field(
        min_length=1,
        max_length=512,
        description="Local directory to deploy, relative to the repository root.",
    )
    image: str = Field(
        min_length=1,
        max_length=255,
        description="Image tag to build and run, e.g. 'aegis-sample:dev'.",
    )
    container_name: str = Field(
        min_length=1,
        max_length=128,
        description="Name for the container.",
    )
    dockerfile: str = Field(default="Dockerfile", max_length=128)
    ports: list[str] = Field(
        default_factory=list,
        max_length=8,
        description="Port mappings as 'host:container', e.g. ['8080:8000'].",
    )

    approve: bool = Field(
        default=False,
        description=(
            "Grant approval for the mutating steps (build and start). Building an "
            "image runs the Dockerfile's RUN steps and starting a container changes "
            "system state, so both require explicit human approval. False leaves "
            "them refused rather than silently skipped."
        ),
    )
    approval_reference: str | None = Field(
        default=None,
        max_length=128,
        description="Who approved the deployment, recorded for the audit trail.",
    )

    dry_run: bool = Field(
        default=False,
        description=(
            "Inspect only: report what would be built and run without building or running anything."
        ),
    )
    health_timeout_seconds: float = Field(
        default=60.0,
        gt=0,
        le=600.0,
        description="How long to wait for the container to report healthy.",
    )
    health_poll_interval_seconds: float = Field(default=2.0, gt=0, le=60.0)
    log_tail: int = Field(
        default=100,
        ge=1,
        le=2000,
        description="Lines of container log to collect at the end.",
    )

    @field_validator("image")
    @classmethod
    def _check_image(cls, value: str) -> str:
        """Reject an image that could be mistaken for a flag or contain junk.

        The MCP server validates this too. Duplicating the check here means the
        API rejects a bad name with a 422 before a subprocess is ever launched,
        rather than relying on a round trip to find out.
        """
        if value.startswith("-"):
            raise ValueError("An image reference may not start with '-'.")
        if any(char.isspace() or ord(char) < 32 for char in value):
            raise ValueError("An image reference may not contain whitespace.")
        return value

    @field_validator("container_name")
    @classmethod
    def _check_container(cls, value: str) -> str:
        """Reject a container name Docker could not accept."""
        if value.startswith("-"):
            raise ValueError("A container name may not start with '-'.")
        if not value[0].isalnum():
            raise ValueError("A container name must start with a letter or digit.")
        if not all(char.isalnum() or char in "_.-" for char in value):
            raise ValueError("A container name may only contain letters, digits, '_', '.' and '-'.")
        return value

    @field_validator("ports")
    @classmethod
    def _check_ports(cls, value: list[str]) -> list[str]:
        """Reject malformed port mappings early."""
        for spec in value:
            parts = spec.split(":")
            if len(parts) > 2 or not all(part.isdigit() for part in parts):
                raise ValueError(f"{spec!r} is not a valid port mapping; use 'host:container'.")
            if not all(1 <= int(part) <= 65535 for part in parts):
                raise ValueError(f"{spec!r} contains a port outside the range 1-65535.")
        return value


class BuildOutcome(BaseModel):
    """What the build step did."""

    attempted: bool
    success: bool = False
    image: str | None = None
    image_id: str | None = None
    context: str | None = None
    dockerfile: str | None = None
    duration_seconds: float | None = None
    log_tail: list[str] = Field(default_factory=list, description="Last lines of the build log.")
    truncated: bool = False
    error: str | None = None


class RunOutcome(BaseModel):
    """What the run step did."""

    attempted: bool
    success: bool = False
    container: str | None = None
    container_id: str | None = None
    image: str | None = None
    ports: list[str] = Field(default_factory=list)
    error: str | None = None


class HealthOutcome(BaseModel):
    """The container's reported health.

    ``state`` distinguishes healthy, unhealthy, pending, no-healthcheck and
    never-ran. Only ``healthy`` counts as success, so a missing health check can
    never be mistaken for a passing one.
    """

    checked: bool = False
    state: Literal["healthy", "unhealthy", "starting", "no_healthcheck", "unknown"] = "unknown"
    detail: str = ""
    failing_streak: int = 0
    waited_seconds: float = 0.0
    exit_code: int | None = None
    healthy: bool = Field(default=False, description="True only when the check passed.")


class LogOutcome(BaseModel):
    """Capped container logs."""

    collected: bool = False
    logs: list[str] = Field(default_factory=list)
    error_logs: list[str] = Field(default_factory=list)
    truncated: bool = False
    error: str | None = None


class DeploymentStep(BaseModel):
    """One node of the workflow, as it actually ran."""

    stage: DeploymentStage
    outcome: StepOutcome
    tool: str | None = Field(default=None, description="The MCP tool invoked, qualified by server.")
    approved: bool | None = Field(
        default=None, description="Whether the policy required and received approval."
    )
    duration_seconds: float | None = None
    detail: str = ""


class DeploymentResult(BaseModel):
    """What ``POST /api/docker/deploy`` returns."""

    image: str
    container: str
    succeeded: bool = Field(
        description="True only when the container started and reported healthy."
    )
    profile: RepositoryProfile | None = Field(
        default=None, description="Static profile of the repository that was deployed."
    )
    steps: list[DeploymentStep] = Field(
        default_factory=list, description="Every stage, in the order it ran."
    )
    build: BuildOutcome | None = None
    run: RunOutcome | None = None
    health: HealthOutcome | None = None
    logs: LogOutcome | None = None
    notes: list[str] = Field(
        default_factory=list,
        description="Caveats, including anything that limited what was verified.",
    )


class DockerAvailabilityResponse(BaseModel):
    """What ``GET /api/docker/availability`` returns."""

    available: bool
    reason: str | None = None
    client_version: str | None = None
    server_version: str | None = None
    os: str | None = None
    arch: str | None = None
    context_root: str
    notes: list[str] = Field(default_factory=list)


class DockerDeployState(TypedDict, total=False):
    """LangGraph state for the local Docker deployment workflow."""

    repository_path: str
    image: str
    container_name: str
    dockerfile: str
    ports: list[str]
    approve: bool
    approval_reference: str | None
    dry_run: bool
    health_timeout_seconds: float
    health_poll_interval_seconds: float
    log_tail: int

    # Populated as the workflow advances.
    target: str
    profile: RepositoryProfile
    availability: dict
    build: BuildOutcome
    run: RunOutcome
    health: HealthOutcome
    logs: LogOutcome
    steps: list[DeploymentStep]
    notes: list[str]
    started_at: datetime
