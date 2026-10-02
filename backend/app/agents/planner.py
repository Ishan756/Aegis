"""DevOps planning graph.

    START → request_analyzer → planner → plan_validator → END

Every node is a plain function over :class:`PlanningState` and returns only the
keys it changes. Nothing here executes a tool: the planner *names* the tools a
step would use, and execution arrives in a later stage.

The graph object is built once at import time because compiling it is pure and
LangGraph graphs are safe to share.
"""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.core.logging import get_logger
from app.models.planning import DeploymentPlan, PlanningState, PlanValidation, RequestAnalysis
from app.services.llm import (
    classify_task_type,
    detect_branch,
    detect_environment,
    detect_runtime,
    get_planning_llm,
)

logger = get_logger(__name__)


def request_analyzer(state: PlanningState) -> dict[str, RequestAnalysis]:
    """Work out what kind of request this is before planning anything."""
    request = state["request"]
    task_type = classify_task_type(request)
    runtime = detect_runtime(request)
    branch = detect_branch(request)
    environment = detect_environment(request)

    notes: list[str] = []
    confidence = 0.9 if task_type != "unknown" else 0.4

    if task_type == "unknown":
        notes.append("Could not identify the task type from the request wording.")
    if runtime is None:
        notes.append("No application stack was named; assuming a generic application.")
    if environment is None:
        notes.append("No target environment was named; assuming staging.")
    if branch is None:
        notes.append("No branch was named; assuming main.")

    analysis = RequestAnalysis(
        task_type=task_type,  # type: ignore[arg-type]
        runtime=runtime,
        branch=branch,
        environment=environment,
        confidence=confidence,
        notes=notes,
    )

    logger.info(
        "request analyzed",
        extra={"task_type": analysis.task_type, "runtime": runtime, "branch": branch},
    )
    return {"analysis": analysis}


def planner(state: PlanningState) -> dict[str, DeploymentPlan]:
    """Draft the structured plan using the configured LLM implementation."""
    llm = get_planning_llm()
    plan = llm.draft_plan(state["request"], state["analysis"])
    logger.info(
        "plan drafted",
        extra={
            "task_type": plan.task_type,
            "risk_level": plan.risk_level,
            "step_count": len(plan.steps),
            "requires_human_approval": plan.requires_human_approval,
        },
    )
    return {"plan": plan}


def plan_validator(state: PlanningState) -> dict[str, PlanValidation]:
    """Check the plan is internally consistent before anyone acts on it.

    Cheap structural checks only. Catching a malformed plan here is the point:
    an untrustworthy plan must never reach an execution stage.
    """
    plan = state["plan"]
    issues: list[str] = []

    if not plan.objective.strip():
        issues.append("Plan has no objective.")

    orders = [step.order for step in plan.steps]
    if orders != list(range(1, len(plan.steps) + 1)):
        issues.append(f"Step order is not sequential from 1: {orders}.")

    declared = set(plan.required_tools)
    for step in plan.steps:
        if step.tool and step.tool not in declared:
            issues.append(f"Step {step.order} uses undeclared tool '{step.tool}'.")

    if plan.risk_level in {"high", "critical"} and not plan.requires_human_approval:
        issues.append("High-risk plan must require human approval.")

    if not plan.expected_verification:
        issues.append("Plan has no expected verification.")

    validation = PlanValidation(is_valid=not issues, issues=issues)

    if issues:
        logger.warning("plan validation failed", extra={"issues": issues})
    else:
        logger.info("plan validated", extra={"step_count": len(plan.steps)})

    return {"validation": validation}


def build_planning_graph() -> CompiledStateGraph:
    """Assemble and compile the planning graph."""
    graph = StateGraph(PlanningState)
    graph.add_node("request_analyzer", request_analyzer)
    graph.add_node("planner", planner)
    graph.add_node("plan_validator", plan_validator)

    graph.add_edge(START, "request_analyzer")
    graph.add_edge("request_analyzer", "planner")
    graph.add_edge("planner", "plan_validator")
    graph.add_edge("plan_validator", END)

    return graph.compile()


# Shared compiled graph; compiling has no side effects.
planning_graph = build_planning_graph()

__all__ = ["build_planning_graph", "planning_graph"]
