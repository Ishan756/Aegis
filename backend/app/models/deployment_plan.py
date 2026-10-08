"""Unified deployment planning contracts.

One plan that combines three analyses: what the repository *is* (stack detection),
what *should* be done to deploy it (strategy), and what could go wrong (risk).

Named ``RepositoryDeploymentPlan`` rather than ``DeploymentPlan`` because
:mod:`app.models.planning` already owns that name for the natural-language planner.
Two same-named models in one codebase is a silent-wrong-import bug waiting to
happen, so the narrower name is used here.

The plan is machine-readable in its structure and human-readable through
:meth:`RepositoryDeploymentPlan.summary_markdown`, because the two audiences are
real: a runner consumes the fields, a reviewer needs to read it.

Nothing in this module carries a credential, and no field holds a resolved secret
value. An environment variable is described by name, source and whether it is
satisfied -- never by what it is set to.
"""

from __future__ import annotations

from typing import Any, Literal, TypedDict

from pydantic import BaseModel, Field, field_validator

from app.models.deployment_target import DeploymentTarget
from app.models.github import DeploymentReadiness, GitHubRepositoryAnalysisRequest, ReadinessCheck
from app.models.repository import PackageManager

RiskLevel = Literal["low", "medium", "high", "critical"]

#: Why a variable is considered unresolved. A template value is a placeholder, not
#: a setting, and treating it as satisfied is how a deployment reaches production
#: with the password "changeme".
UnresolvedReason = Literal["empty", "placeholder", "missing_declaration"]


class RepositoryReference(BaseModel):
    """Which repository and ref the plan covers."""

    owner: str
    name: str
    full_name: str
    ref: str = Field(description="The ref actually analysed, not necessarily the default.")
    default_branch: str | None = None
    source: Literal["github"] = "github"


class DetectedStack(BaseModel):
    """The stack facts the plan's decisions are derived from.

    A copy rather than a reference, so the plan is self-contained: a reader does
    not need the repository profile to understand why the strategies say what
    they say.
    """

    primary_language: str | None = None
    languages: list[str] = Field(default_factory=list)
    backend_framework: str | None = None
    frontend_framework: str | None = None
    package_manager: PackageManager | None = None
    package_managers: list[PackageManager] = Field(default_factory=list)
    test_framework: str | None = None
    test_file_count: int = Field(default=0, ge=0)
    has_dockerfile: bool = False
    dockerfiles: list[str] = Field(default_factory=list)
    has_docker_compose: bool = False
    ci_cd: list[str] = Field(default_factory=list)
    databases: list[str] = Field(default_factory=list)


class ContainerAssets(BaseModel):
    """What the Dockerfile and compose file actually declare.

    Read from their contents rather than inferred from filenames, because
    "there is a Dockerfile" and "the Dockerfile declares a health check" are very
    different things to a deployment plan.
    """

    dockerfile_read: bool = False
    base_image: str | None = None
    exposed_ports: list[int] = Field(default_factory=list)
    has_healthcheck: bool = False
    healthcheck_command: str | None = None
    has_entrypoint: bool = False
    entrypoint: str | None = None
    runs_as_non_root: bool = False
    compose_read: bool = False
    compose_services: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class Strategy(BaseModel):
    """One dimension of the plan, with the reasoning kept alongside it.

    ``rationale`` is not decoration: a strategy a reviewer cannot question is a
    strategy they will rubber-stamp.
    """

    approach: str = Field(description="Short machine-readable name, e.g. 'docker'.")
    summary: str = Field(description="One sentence, for the human-readable summary.")
    rationale: list[str] = Field(
        default_factory=list, description="The profile facts this decision rests on."
    )
    tools: list[str] = Field(default_factory=list, description="MCP tools this strategy would use.")
    commands: list[str] = Field(
        default_factory=list,
        description="Illustrative commands. Recorded for a human; never executed here.",
    )
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    limitations: list[str] = Field(default_factory=list)
    recommended_changes: list[str] = Field(
        default_factory=list,
        description="Repository changes the plan would need. Not applied automatically.",
    )


class EnvironmentVariable(BaseModel):
    """One configuration value the application expects."""

    name: str
    source: str = Field(description="File that declared it, e.g. '.env.example'.")
    required: bool = True
    resolved: bool = Field(
        description="True when the declaration supplies a real value, not a placeholder."
    )
    unresolved_reason: UnresolvedReason | None = None
    value_hint: str | None = Field(
        default=None,
        description="Placeholder text only, for telling 'changeme' from 'postgres://…'. "
        "Never a resolved secret.",
    )
    notes: str = ""


class RequiredService(BaseModel):
    """A backing service the deployment needs."""

    name: str
    purpose: str
    detected_from: list[str] = Field(default_factory=list)
    required: bool = True


class Risk(BaseModel):
    """Something that could go wrong, with what to do about it."""

    id: str
    severity: RiskLevel
    title: str
    detail: str
    mitigation: str = ""
    blocking: bool = Field(
        default=False,
        description="True when the deployment must not proceed until this is resolved.",
    )


class ApprovalRequirement(BaseModel):
    """A step that needs a human to say yes before it runs."""

    action: str
    reason: str
    risk_level: RiskLevel
    approver_role: str = "operator"
    tool: str | None = None


class OrderedStep(BaseModel):
    """One step in the deployment, in execution order."""

    order: int = Field(ge=1)
    title: str
    description: str = ""
    tool: str | None = Field(default=None, description="MCP tool this step would call.")
    command: str | None = Field(
        default=None, description="Illustrative command for a human. Never executed."
    )
    requires_approval: bool = False
    automated: bool = Field(
        default=True, description="False when no tool exists yet and a human must act."
    )
    blocked: bool = False
    blocked_reason: str | None = None
    depends_on: list[int] = Field(default_factory=list)


class RepositoryDeploymentPlan(BaseModel):
    """The unified plan: analysis, strategy, risk and ordered steps."""

    repository: RepositoryReference
    detected_stack: DetectedStack
    container_assets: ContainerAssets = Field(default_factory=ContainerAssets)

    build_strategy: Strategy
    test_strategy: Strategy
    deployment_strategy: Strategy
    health_check_strategy: Strategy
    rollback_strategy: Strategy

    required_environment_variables: list[EnvironmentVariable] = Field(default_factory=list)
    required_services: list[RequiredService] = Field(default_factory=list)
    risks: list[Risk] = Field(default_factory=list)
    approval_requirements: list[ApprovalRequirement] = Field(default_factory=list)
    ordered_steps: list[OrderedStep] = Field(default_factory=list)

    readiness: DeploymentReadiness | None = Field(
        default=None, description="Reused from the repository analysis stage."
    )
    readiness_checks: list[ReadinessCheck] = Field(default_factory=list)

    blocked: bool = Field(description="True when the deployment must not proceed yet.")
    blocked_by: list[str] = Field(
        default_factory=list, description="Human-readable reasons the plan is blocked."
    )
    requires_human_approval: bool = False
    notes: list[str] = Field(default_factory=list)

    @property
    def unresolved_environment_variables(self) -> list[EnvironmentVariable]:
        """Required variables that are not satisfied. Drives the blocked decision."""
        return [
            variable
            for variable in self.required_environment_variables
            if variable.required and not variable.resolved
        ]

    def summary_markdown(self) -> str:
        """Render the plan for a human reviewer.

        The structured form is the contract; this is the same information in a
        form someone can approve in one reading. Both come from one source, so
        they cannot disagree.
        """
        lines: list[str] = [
            f"# Deployment plan: {self.repository.full_name}",
            "",
            f"**Ref:** `{self.repository.ref}`  ",
            f"**Primary language:** {self.detected_stack.primary_language or 'unknown'}  ",
            f"**Package manager:** {self.detected_stack.package_manager or 'unknown'}  ",
            f"**Test files:** {self.detected_stack.test_file_count}  ",
            f"**Dockerfile:** {'yes' if self.detected_stack.has_dockerfile else 'no'}  ",
            "",
        ]

        if self.blocked:
            lines += ["## Blocked", ""]
            lines += [f"- {reason}" for reason in self.blocked_by]
            lines.append("")
        else:
            lines += ["## Status", "", "Not blocked. Proceed through the steps below.", ""]

        lines += self._strategy_section("Build", self.build_strategy)
        lines += self._strategy_section("Tests", self.test_strategy)
        lines += self._strategy_section("Deployment", self.deployment_strategy)
        lines += self._strategy_section("Health checks", self.health_check_strategy)
        lines += self._strategy_section("Rollback", self.rollback_strategy)

        if self.required_environment_variables:
            lines += [
                "## Environment variables",
                "",
                "| Variable | Required | Resolved | Source |",
                "| --- | --- | --- | --- |",
            ]
            for variable in self.required_environment_variables:
                resolved = "yes" if variable.resolved else f"no ({variable.unresolved_reason})"
                lines.append(
                    f"| `{variable.name}` | {'yes' if variable.required else 'no'} "
                    f"| {resolved} | {variable.source} |"
                )
            lines.append("")

        if self.required_services:
            lines += ["## Required services", ""]
            for service in self.required_services:
                lines.append(
                    f"- **{service.name}** — {service.purpose} "
                    f"(from: {', '.join(service.detected_from) or 'inferred'})"
                )
            lines.append("")

        if self.risks:
            lines += [
                "## Risks",
                "",
                "| Severity | Risk | Blocking | Mitigation |",
                "| --- | --- | --- | --- |",
            ]
            for risk in self.risks:
                lines.append(
                    f"| {risk.severity} | {risk.title} | {'yes' if risk.blocking else 'no'} "
                    f"| {risk.mitigation or '—'} |"
                )
            lines.append("")

        if self.approval_requirements:
            lines += ["## Approvals required", ""]
            for approval in self.approval_requirements:
                lines.append(f"- **{approval.action}** ({approval.risk_level}) — {approval.reason}")
            lines.append("")

        lines += ["## Ordered steps", ""]
        for step in self.ordered_steps:
            marker = " ⛔" if step.blocked else ""
            approval = " *(approval required)*" if step.requires_approval else ""
            manual = "" if step.automated else " *(manual)*"
            lines.append(
                f"{step.order}. **{step.title}**{marker}{approval}{manual} — {step.description}"
            )
            if step.command:
                lines.append(f"   ```\n   {step.command}\n   ```")
            if step.blocked_reason:
                lines.append(f"   > Blocked: {step.blocked_reason}")
        lines.append("")

        if self.readiness is not None:
            lines += [
                "## Readiness",
                "",
                f"Score {self.readiness.score}/100 — "
                f"{'ready' if self.readiness.ready else 'not ready'}",
                "",
            ]
            for check in self.readiness_checks:
                lines.append(f"- **{check.status}** `{check.name}` — {check.detail}")

        if self.notes:
            lines += ["", "## Notes", ""]
            lines += [f"- {note}" for note in self.notes]

        return "\n".join(lines)

    @staticmethod
    def _strategy_section(title: str, strategy: Strategy) -> list[str]:
        lines = [f"## {title}", "", f"**Approach:** `{strategy.approach}` — {strategy.summary}", ""]
        if strategy.rationale:
            lines += ["Why:", ""]
            lines += [f"- {item}" for item in strategy.rationale]
            lines.append("")
        if strategy.limitations:
            lines += ["Limitations:", ""]
            lines += [f"- {item}" for item in strategy.limitations]
            lines.append("")
        if strategy.recommended_changes:
            lines += ["Recommended changes (not applied automatically):", ""]
            lines += [f"- {item}" for item in strategy.recommended_changes]
            lines.append("")
        return lines


class DeploymentPlanRequest(GitHubRepositoryAnalysisRequest):
    """Body of ``POST /api/deployment/plan``.

    Inherits the repository identity and its validation, so a plan cannot be
    requested for a repository name that the GitHub tools would refuse.
    """

    include_issues: bool = Field(
        default=False,
        description="Also read open issues. The plan does not need them; useful for triage.",
    )

    deployment_target: DeploymentTarget | None = Field(
        default=None,
        description=(
            "Where the plan would deploy: omit for the local daemon. A remote "
            "target changes the plan's wording, commands and approval reasons "
            "to name that target; it does not change what the repository "
            "analysis finds."
        ),
    )

    @field_validator("issue_limit", mode="before")
    @classmethod
    def _bound_issue_limit(cls, value: Any) -> Any:
        """Clamp before the inherited range is checked.

        ``mode="before"`` matters: an after-validator runs *later* than the
        inherited ``le`` bound, so an over-large value would be rejected before
        the clamp could apply.
        """
        if isinstance(value, int):
            return min(value, 30)
        return value


class DeploymentPlanResponse(BaseModel):
    """Response body. Carries both representations of the same plan."""

    plan: RepositoryDeploymentPlan
    summary_markdown: str = Field(description="The same plan rendered for a human reviewer.")


class DeploymentPlanState(TypedDict, total=False):
    """LangGraph state for the unified deployment planning workflow."""

    owner: str
    repository: str
    requested_ref: str | None
    include_issues: bool
    issue_limit: int
    #: Where the plan deploys. None is the local daemon, which is what every
    #: plan was before targets existed.
    deployment_target: DeploymentTarget | None

    profile: object
    readiness: DeploymentReadiness
    readiness_checks: list[ReadinessCheck]
    ref: str | None

    container_assets: ContainerAssets
    environment_variables: list[EnvironmentVariable]
    services: list[RequiredService]
    build_strategy: Strategy
    test_strategy: Strategy
    deployment_strategy: Strategy
    health_check_strategy: Strategy
    rollback_strategy: Strategy

    risks: list[Risk]
    approval_requirements: list[ApprovalRequirement]
    steps: list[OrderedStep]
    blocked: bool
    blocked_by: list[str]
    requires_human_approval: bool
    notes: list[str]
    plan: RepositoryDeploymentPlan
