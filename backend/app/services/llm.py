"""LLM access, isolated behind one interface.

Every provider-specific detail belongs behind :class:`PlanningLLM`. The graph and
the API only ever see that method, so swapping models — or moving to a different
provider — means writing one new class and changing :func:`get_planning_llm`.

The default implementation is :class:`HeuristicPlanningLLM`, which is fully
deterministic and makes no network call. That keeps the graph testable and lets
the project run with no API key; a real provider arrives in a later stage.
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Any, Protocol

from app.models.planning import DeploymentPlan, PlanStep, RequestAnalysis

# Words that follow "from"/"on" but are not branches.
_NON_BRANCH_WORDS = frozenset({"the", "a", "an", "my", "this", "our", "its", "new", "that"})

# Recognised environment names. Used both to detect the target environment and to
# stop "on production" being mistaken for a branch name.
_ENV_NAMES = ("production", "prod", "staging", "stage", "development", "dev", "qa", "test")
_ENV_NAMES_SET = frozenset(_ENV_NAMES)

_TASK_KEYWORDS: dict[str, tuple[str, ...]] = {
    "rollback": ("rollback", "roll back", "revert", "undo"),
    "scale": ("scale", "autoscal", "replica", "up the pods"),
    "configure": ("configure", "config", "set up", "setup", "install", "provision"),
    "monitor": ("monitor", "watch", "observe", "alert", "log"),
    "deploy": ("deploy", "release", "ship", "roll out", "rollout", "publish"),
}

# Task-specific step templates, appended after the shared opening steps.
# Keeping these as data rather than branching logic keeps the planner readable.
# Read-only steps never set requires_approval, which is what lets a monitor
# request come back needing no sign-off.
_TAIL_STEPS: dict[str, Any] = {
    "deploy": lambda target, env: [
        PlanStep(
            order=3,
            title="Build container image",
            description=f"Build an image for {target} and tag it for {env}.",
            tool="docker.build_image",
            requires_approval=True,
        ),
        PlanStep(
            order=4,
            title="Verify image",
            description="Start the image locally and confirm the service boots.",
            tool="docker.inspect_container",
        ),
    ],
    "rollback": lambda target, env: [
        PlanStep(
            order=3,
            title="Identify last known-good image",
            description="Find the most recent image tag that passed verification.",
            tool="docker.inspect_container",
        ),
        PlanStep(
            order=4,
            title="Redeploy previous image",
            description=f"Promote the previous image to {env}.",
            tool="docker.build_image",
            requires_approval=True,
        ),
    ],
    "scale": lambda target, env: [
        PlanStep(
            order=3,
            title="Update replica count",
            description=f"Adjust the replica count for {target} in {env}.",
            tool="kubernetes.scale",
            requires_approval=True,
        ),
        PlanStep(
            order=4,
            title="Confirm replicas healthy",
            description="Check that all replicas report ready.",
            tool="kubernetes.get_pods",
        ),
    ],
    "configure": lambda target, env: [
        PlanStep(
            order=3,
            title="Apply configuration",
            description=f"Apply the requested configuration to {target} in {env}.",
            tool="config.apply",
            requires_approval=True,
        ),
        PlanStep(
            order=4,
            title="Confirm configuration",
            description="Read the effective configuration back and diff it.",
            tool="config.get",
        ),
    ],
    "monitor": lambda target, env: [
        PlanStep(
            order=3,
            title="Collect logs",
            description=f"Gather recent logs for {target} in {env}.",
            tool="docker.read_logs",
        ),
        PlanStep(
            order=4,
            title="Summarise findings",
            description="Report errors, warnings, and anything unusual.",
        ),
    ],
    "unknown": lambda target, env: [
        PlanStep(
            order=3,
            title="Clarify the request",
            description="Ask the requester what outcome and target they have in mind.",
        ),
        PlanStep(
            order=4,
            title="Re-plan once clarified",
            description="Re-run this planner with the clarified request.",
        ),
    ],
}


class PlanningLLM(Protocol):
    """Turns a request plus its analysis into a structured plan."""

    def draft_plan(self, request: str, analysis: RequestAnalysis) -> DeploymentPlan: ...


class HeuristicPlanningLLM:
    """Deterministic planner used until a real provider is wired in.

    Derives the plan from the analyzer's findings rather than a model, so the
    same request always yields the same plan. This is what makes the graph
    testable without network access.
    """

    def draft_plan(self, request: str, analysis: RequestAnalysis) -> DeploymentPlan:
        task_type = analysis.task_type
        target = analysis.runtime or "application"
        environment = analysis.environment or "staging"
        branch = analysis.branch or "main"

        # Shared opening: no plan touches infrastructure before confirming source.
        steps = [
            PlanStep(
                order=1,
                title="Confirm repository and branch",
                description=(
                    f"Verify the repository is reachable and {branch} is "
                    "the intended source branch."
                ),
                tool="github.list_branches",
            ),
            PlanStep(
                order=2,
                title="Inspect build configuration",
                description=(
                    f"Read the build and dependency files to confirm how {target} is built."
                ),
                tool="github.read_file",
            ),
            *_TAIL_STEPS[task_type](target, environment),
        ]

        risk_level = self._risk(task_type, analysis.confidence)
        used_tools = [step.tool for step in steps if step.tool]
        needs_approval = any(step.requires_approval for step in steps)

        return DeploymentPlan(
            objective=self._objective(request, task_type, target, environment),
            task_type=task_type,
            assumptions=[
                f"The application is a {target} project.",
                f"The target environment is {environment}.",
                f"Source branch is {branch}.",
                "No secrets or infrastructure changes are needed beyond the steps listed.",
            ],
            required_tools=sorted(set(used_tools)),
            steps=steps,
            risk_level=risk_level,
            requires_human_approval=needs_approval or risk_level in {"high", "critical"},
            expected_verification=[
                f"The change is live in {environment}.",
                f"The {target} service responds on its health endpoint.",
                "No new errors appear in logs during startup.",
            ],
        )

    @staticmethod
    def _objective(request: str, task_type: str, target: str, environment: str) -> str:
        verb = {
            "deploy": "Deploy",
            "rollback": "Roll back",
            "scale": "Scale",
            "configure": "Configure",
            "monitor": "Monitor",
            "unknown": "Handle",
        }[task_type]
        return f"{verb} {target} to {environment}: {request.strip()}"

    @staticmethod
    def _risk(task_type: str, confidence: float) -> str:
        """Estimate risk from the task type, tempered by analyzer confidence."""
        base = {
            "rollback": "high",
            "deploy": "medium",
            "scale": "medium",
            "configure": "low",
            "monitor": "low",
            "unknown": "medium",
        }[task_type]
        # A low-confidence reading of the request is itself a risk.
        return "medium" if base == "low" and confidence < 0.5 else base


def classify_task_type(request: str) -> str:
    """Best-effort task classification by keyword.

    Shared by the analyzer and kept beside the planner so both agree on the
    vocabulary.
    """
    text = request.lower()
    for task_type, keywords in _TASK_KEYWORDS.items():
        if any(keyword in text for keyword in keywords):
            return task_type
    return "unknown"


def detect_runtime(request: str) -> str | None:
    """Detect an application stack mentioned in the request."""
    text = request.lower()
    runtimes = {
        "nodejs": ("node", "nodejs", "node.js", "npm", "yarn", "pnpm", "express", "next.js"),
        "python": ("python", "django", "flask", "fastapi", "pytest"),
        "go": ("golang", "go service", "go app"),
        "java": ("java", "spring", "maven", "gradle"),
        "rust": ("rust", "cargo"),
        "dotnet": ("dotnet", ".net", "csharp"),
    }
    for runtime, keywords in runtimes.items():
        if any(keyword in text for keyword in keywords):
            return runtime
    return None


def detect_branch(request: str) -> str | None:
    """Extract a git branch from phrases like ``the main branch`` or ``from develop``."""
    # "the main branch" / "release branch" — the branch name precedes the word.
    match = re.search(r"\b([A-Za-z0-9._/-]+)\s+branch\b", request, flags=re.IGNORECASE)
    if match and _usable_branch(match.group(1)):
        return match.group(1)

    # "from main" / "on develop".
    match = re.search(
        r"\b(?:from|on|branch|target)\s+([A-Za-z0-9._/-]+)", request, flags=re.IGNORECASE
    )
    return match.group(1) if match and _usable_branch(match.group(1)) else None


def _usable_branch(candidate: str) -> bool:
    """Reject words that matched the pattern but are not branch names."""
    candidate = candidate.strip("/")
    lowered = candidate.lower()
    # "on production" names an environment, not a branch.
    return bool(candidate) and lowered not in _NON_BRANCH_WORDS and lowered not in _ENV_NAMES_SET


def detect_environment(request: str) -> str | None:
    """Extract a target environment from the request."""
    match = re.search(
        rf"\b(?:to|in|on|into)\s+({'|'.join(_ENV_NAMES)})\b",
        request,
        flags=re.IGNORECASE,
    )
    return match.group(1).lower() if match else None


@lru_cache
def get_planning_llm() -> PlanningLLM:
    """Return the configured planner.

    Swap this single function to change provider or model.
    """
    return HeuristicPlanningLLM()
