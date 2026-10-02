"""Planning agent contracts.

These schemas are the agent's boundary: the graph produces them, the API returns
them, and they are what a provider-specific LLM implementation must fill in.
"""

from __future__ import annotations

from typing import Literal, TypedDict

from pydantic import BaseModel, Field

TaskType = Literal["deploy", "rollback", "scale", "configure", "monitor", "unknown"]
RiskLevel = Literal["low", "medium", "high", "critical"]


class AgentPlanRequest(BaseModel):
    """Body of ``POST /api/agent/plan``."""

    request: str = Field(
        min_length=1,
        max_length=2000,
        description="Natural-language DevOps request, e.g. 'Deploy my Node app from main'.",
    )


class RequestAnalysis(BaseModel):
    """What the analyzer understood from the request."""

    task_type: TaskType = Field(description="Best guess at the kind of work requested.")
    runtime: str | None = Field(default=None, description="Detected stack, e.g. 'nodejs'.")
    branch: str | None = Field(default=None, description="Referenced git branch, if any.")
    environment: str | None = Field(default=None, description="Target environment, if named.")
    confidence: float = Field(ge=0.0, le=1.0, description="How sure the analyzer is.")
    notes: list[str] = Field(default_factory=list, description="Ambiguities worth surfacing.")


class PlanStep(BaseModel):
    """One ordered action in the plan. Never executed yet."""

    order: int = Field(ge=1, description="1-based position in the plan.")
    title: str
    description: str = Field(description="What this step would do.")
    tool: str | None = Field(
        default=None, description="Tool this step would call. Not invoked in this stage."
    )
    requires_approval: bool = Field(
        default=False, description="Whether this step mutates infrastructure."
    )


class DeploymentPlan(BaseModel):
    """The structured plan produced by the planner."""

    objective: str = Field(description="One-sentence restatement of the goal.")
    task_type: TaskType
    assumptions: list[str] = Field(default_factory=list)
    required_tools: list[str] = Field(
        default_factory=list, description="Tool names the plan expects to exist."
    )
    steps: list[PlanStep] = Field(min_length=1)
    risk_level: RiskLevel
    requires_human_approval: bool = Field(
        description="True when any step is destructive or high risk."
    )
    expected_verification: list[str] = Field(
        default_factory=list, description="How success would be confirmed after execution."
    )


class PlanValidation(BaseModel):
    """Outcome of the validator node."""

    is_valid: bool
    issues: list[str] = Field(default_factory=list)


class PlanResponse(BaseModel):
    """Response body of ``POST /api/agent/plan``."""

    plan: DeploymentPlan
    analysis: RequestAnalysis
    validation: PlanValidation


class PlanningState(TypedDict, total=False):
    """LangGraph state for the planning workflow.

    ``total=False`` because LangGraph merges each node's return value into the
    accumulated state, so nodes only return the keys they change.
    """

    request: str
    analysis: RequestAnalysis
    plan: DeploymentPlan
    validation: PlanValidation
