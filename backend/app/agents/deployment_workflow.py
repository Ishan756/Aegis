"""The deployment workflow: PLAN → EXECUTE → VERIFY → END.

===============================================  ==========================
Stage                                           What it does
===============================================  ==========================
PLAN   Turns a deployment request into tasks.   Decides, mutates nothing.
EXECUTE  Runs the tasks through MCP tools.      Policy-checked, audited.
VERIFY Proves the deployment works.             Read-only, seven checks.
DEBUG   Classifies a failure.                   Proposes; never acts.
END
===============================================  ==========================

Two branch points, and both are deliberate:

- **EXECUTE → DEBUG** when the run stops. A run that halted has no deployment to
  verify, so verifying it would report "container does not exist" and bury the
  real cause.
- **VERIFY → DEBUG** unless the verdict is ``SUCCESS``. A ``WARNING`` routes here
  too: the deployment is answering but something could not be confirmed, and
  ending quietly would discard the evidence that raised the concern.

The workflow does not implement self-healing. DEBUG produces hypotheses and
proposals and applies nothing, so a failed deployment ends in a report rather
than in an unrequested change.
"""

from __future__ import annotations

import logging
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.agents.debug_agent import DebugAssessment, assess_failure

# `verify_stage` is intentionally not imported: this module defines its own
# VERIFY node, because the workflow carries a ready-made VerificationRequest
# from PLAN rather than loose state keys.
from app.agents.deployment_verification import verify_deployment
from app.agents.execution_engine import execute_stage
from app.models.docker import DockerDeployRequest
from app.models.execution import Task
from app.models.verification import VerificationRequest, VerificationResult

logger = logging.getLogger(__name__)


class DeploymentWorkflowState(TypedDict, total=False):
    """Explicit workflow state.

    Declared rather than left as ``dict[str, Any]``: with a bare dict, whether
    seeded keys such as ``dry_run`` survive a node boundary is an implementation
    detail of the graph engine rather than something the schema states. Declaring
    the keys makes the contract explicit and testable.
    """

    request: DockerDeployRequest
    tasks: list[Task]
    verification_request: VerificationRequest
    plan_explanation: str

    approve: bool
    approval_reference: str | None
    stop_on_critical_failure: bool
    dry_run: bool

    execution: Any
    response: Any
    verification: VerificationResult
    result: VerificationResult
    status: str
    checks: list[Any]
    evidence: list[Any]
    failures: list[str]
    warnings: list[str]
    recommendation: str
    debug: DebugAssessment
    stopped: bool
    stop_reason: str | None
    stop_task_id: str | None


def build_plan(request: DockerDeployRequest) -> list[Task]:
    """PLAN: turn a deployment request into an ordered task list.

    Ordering encodes causality, not preference. Nothing that needs an image comes
    before the build, and nothing that needs a container comes before the run.
    """
    image = request.image
    container = request.container_name
    ports = list(request.ports)
    build_tool = "docker.build_image"
    return [
        Task(
            id="1",
            title="Check the Docker daemon is available",
            description="Confirm a working daemon before building anything.",
            tool="docker.docker_available",
            arguments={},
        ),
        Task(
            id="2",
            title="Build the container image",
            description="Build the image from the repository's Dockerfile.",
            tool=build_tool,
            arguments={
                # `build_image` takes `context_path` and `tag`, not `context`/`image`.
                "context_path": request.repository_path,
                "dockerfile": request.dockerfile,
                "tag": image,
            },
            requires_approval=True,
            critical=True,
            max_attempts=2,
            retry_backoff_seconds=2.0,
            timeout_seconds=900.0,
        ),
        Task(
            id="3",
            title="Start the container",
            description="Run the built image on the local daemon.",
            tool="docker.start_container",
            arguments={
                "image": image,
                "name": container,
                "ports": ports,
            },
            requires_approval=True,
            critical=True,
            timeout_seconds=180.0,
        ),
    ]


def expected_port(request: DockerDeployRequest) -> int | None:
    """The host port the deployment published, parsed from the request.

    ``"8080:8000"`` publishes 8080 on the host; ``"8000"`` publishes 8000. The
    verifier needs the host port because that is the one a client would dial.
    """
    for spec in request.ports:
        head = spec.split(":")[0]
        if head.isdigit():
            return int(head)
    return None


async def plan_stage(state: DeploymentWorkflowState) -> dict[str, Any]:
    """The PLAN node."""
    request: DockerDeployRequest = state["request"]
    tasks = build_plan(request)
    return {
        "tasks": tasks,
        "verification_request": VerificationRequest(
            container_name=request.container_name,
            expected_port=expected_port(request),
            health_path="/health",
            log_tail=request.log_tail,
        ),
        "plan_explanation": (
            f"{len(tasks)} tasks: verify the daemon, build {request.image!r}, "
            f"start {request.container_name!r}. Every mutating task needs approval."
        ),
    }


async def verify_stage(state: DeploymentWorkflowState) -> dict[str, Any]:
    """The VERIFY node.

    Takes the ``VerificationRequest`` built during PLAN rather than rebuilding one
    from loose state keys, so the port and health path the plan promised are the
    ones actually checked.
    """
    request: VerificationRequest = state["verification_request"]
    result = await verify_deployment(request)
    return {
        "verification": result,
        "result": result,
        "status": result.status,
        "checks": result.checks,
        "evidence": result.evidence,
        "failures": result.failures,
        "warnings": result.warnings,
        "recommendation": result.recommendation,
    }


def route_after_execute(state: DeploymentWorkflowState) -> str:
    """A stopped run goes to DEBUG; a dry run ends without verifying anything.

    A dry run deliberately deploys nothing, so there is no container to verify.
    Running VERIFY anyway would report on a container that does not exist, which
    is a false failure rather than a useful one.
    """
    if state.get("dry_run"):
        return "end"
    return "debug" if state.get("stopped") else "verify"


async def debug_stage(state: DeploymentWorkflowState) -> dict[str, Any]:
    """The DEBUG node: classify the failure, propose, change nothing."""
    result: VerificationResult | None = state.get("result")
    assessment: DebugAssessment = await assess_failure(
        result=result,
        stop_reason=state.get("stop_reason"),
        stop_task_id=state.get("stop_task_id"),
    )
    logger.info(
        "deployment workflow reached debug",
        extra={"kind": assessment.kind, "applied": assessment.applied},
    )
    return {"debug": assessment}


def route_after_verify(state: DeploymentWorkflowState) -> str:
    """Only a clean SUCCESS ends the run."""
    return "end" if state.get("status") == "SUCCESS" else "debug"


def build_workflow_graph() -> CompiledStateGraph:
    """Assemble PLAN → EXECUTE → VERIFY → END with DEBUG on both failure paths."""
    builder = StateGraph(DeploymentWorkflowState)
    builder.add_node("plan", plan_stage)
    builder.add_node("execute", execute_stage)
    builder.add_node("verify", verify_stage)
    builder.add_node("debug", debug_stage)

    builder.add_edge(START, "plan")
    builder.add_edge("plan", "execute")
    builder.add_conditional_edges(
        "execute", route_after_execute, {"verify": "verify", "debug": "debug", "end": END}
    )
    builder.add_conditional_edges("verify", route_after_verify, {"end": END, "debug": "debug"})
    builder.add_edge("debug", END)

    return builder.compile()


deployment_workflow_graph = build_workflow_graph()


async def run_deployment_workflow(request: DockerDeployRequest) -> dict[str, Any]:
    """Run PLAN → EXECUTE → VERIFY → END for a local deployment.

    ``request.approve`` is threaded into the EXECUTE stage unchanged. The workflow
    never manufactures approval: a request without ``approve: true`` has its build
    and run refused, exactly as a direct deployment would.
    """
    state: DeploymentWorkflowState = {
        "request": request,
        "approve": request.approve,
        "approval_reference": request.approval_reference,
        "stop_on_critical_failure": True,
        "dry_run": request.dry_run,
    }
    result = await deployment_workflow_graph.ainvoke(state)
    return {
        "plan": result.get("tasks", []),
        "execution": result.get("response"),
        "verification": result.get("result"),
        "debug": result.get("debug"),
        "plan_explanation": result.get("plan_explanation"),
        "stopped": bool(result.get("stopped")),
        "stop_reason": result.get("stop_reason"),
    }


__all__ = [
    "DeploymentWorkflowState",
    "build_plan",
    "build_workflow_graph",
    "deployment_workflow_graph",
    "expected_port",
    "run_deployment_workflow",
]
