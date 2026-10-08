"""EC2 deployment contracts.

One request type and one result type for deploying to a configured EC2
instance. The *target* itself is deliberately not part of the request: the
instance, region and key come from :class:`~app.core.config.EC2Settings` set by
the operator, so a caller cannot aim a deployment at a host nobody approved —
the only choices exposed are what to build and whether to approve the mutating
steps.

The request inherits the local Docker request because the two flows share every
field that describes the application; the defaults differ only where the target
differs (image tag, container name, published port, health path).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, model_validator

from app.core.config import get_settings
from app.models.deployment_record import DeploymentTarget
from app.models.docker import DeploymentStep, DockerDeployRequest
from app.models.execution import ExecutionResponse, Task
from app.models.incident import IncidentReport
from app.models.verification import VerificationResult


class EC2DeploymentRequest(DockerDeployRequest):
    """Body of ``POST /api/deployment/ec2``.

    Omitted application fields default from ``AEGIS_EC2__*`` configuration so
    the common case is ``{"repository_path": "examples/sample_app",
    "approve": true}``; every default can still be overridden per request.
    """

    @model_validator(mode="before")
    @classmethod
    def _defaults_from_settings(cls, data: Any) -> Any:
        """Fill the application fields from EC2 configuration when absent.

        A ``before`` validator rather than field defaults: the values live in
        settings, which are resolved per process, and a field default is fixed
        at class creation. Runs before every field validator, so the inherited
        image and container-name checks still see the final values.
        """
        if isinstance(data, dict):
            ec2 = get_settings().ec2
            data.setdefault("image", ec2.image)
            data.setdefault("container_name", ec2.container_name)
            data.setdefault("health_path", ec2.health_path)
            if not data.get("ports"):
                data["ports"] = [f"{ec2.host_port}:{ec2.container_port}"]
        return data


class EC2DeploymentResult(BaseModel):
    """What ``POST /api/deployment/ec2`` returns.

    Carries the same fields the local workflow returns, plus the target and the
    preparation step: whether the instance was reachable and ready is the
    difference between a remote deployment failing for an application reason and
    failing because the machine was not set up.
    """

    deployment_id: str
    target: DeploymentTarget
    succeeded: bool = Field(
        description=(
            "True only when verification returned SUCCESS. A dry run, a refused "
            "step and a failed verification are all False."
        )
    )
    #: The connect and prepare steps, present even when the run stopped before
    #: PLAN: whether the instance was reachable and ready is the difference
    #: between an application failure and a machine that was not set up.
    steps: list[DeploymentStep] = Field(default_factory=list)
    plan: list[Task] = Field(default_factory=list)
    execution: ExecutionResponse | None = None
    verification: VerificationResult | None = None
    debug: Any = None
    recovery: Any = None
    recovered: bool = False
    incident: IncidentReport | None = None
    plan_explanation: str | None = None
    stopped: bool = False
    stop_reason: str | None = None
    notes: list[str] = Field(
        default_factory=list,
        description="Caveats, including anything that limited what was verified.",
    )


__all__ = ["EC2DeploymentRequest", "EC2DeploymentResult"]
