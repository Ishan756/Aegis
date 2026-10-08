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
from collections.abc import Awaitable, Callable
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.agents.debug_agent import DebugAssessment, assess_failure

# `verify_stage` is intentionally not imported: this module defines its own
# VERIFY node, because the workflow carries a ready-made VerificationRequest
# from PLAN rather than loose state keys.
from app.agents.deployment_verification import verify_deployment
from app.agents.execution_engine import execute_stage
from app.agents.recovery_workflow import run_recovery
from app.memory import get_memory_service
from app.models.deployment_record import DeploymentRecord, DeploymentTarget
from app.models.docker import DockerDeployRequest
from app.models.execution import Task
from app.models.incident import IncidentReport
from app.models.self_healing import RecoveryOutcome, SelfHealingPolicy
from app.models.verification import Evidence, VerificationRequest, VerificationResult

logger = logging.getLogger(__name__)

#: Who the workflow attributes Docker tool calls to when the request does not
#: name a server: the local daemon, which is what this workflow ran against
#: before remote targets existed.
DEFAULT_DOCKER_SERVER = "docker"


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

    # Remote-target plumbing. All three default for the local daemon, so a run
    # that never mentions them behaves exactly as it did before targets existed.
    docker_server: str
    probe_host: str | None
    target: DeploymentTarget | None
    #: Optional read-only diagnostics appended to VERIFY's evidence. Used by the
    #: EC2 flow for loopback and CloudWatch observations; must not mutate.
    evidence_hook: Callable[[], Awaitable[list[Evidence]]] | None

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

    # Recovery
    self_healing_policy: SelfHealingPolicy
    self_healing_human_approved: bool
    recovery: RecoveryOutcome
    incident: IncidentReport | None


def build_plan(
    request: DockerDeployRequest, *, docker_server: str = DEFAULT_DOCKER_SERVER
) -> list[Task]:
    """PLAN: turn a deployment request into an ordered task list.

    Ordering encodes causality, not preference. Nothing that needs an image comes
    before the build, and nothing that needs a container comes before the run.

    ``docker_server`` qualifies every tool name: a remote target's tasks name the
    scoped server opened for that target, so the execution engine cannot reach a
    remote daemon through the local server or vice versa.
    """
    image = request.image
    container = request.container_name
    ports = list(request.ports)
    build_tool = f"{docker_server}.build_image"
    return [
        Task(
            id="1",
            title="Check the Docker daemon is available",
            description="Confirm a working daemon before building anything.",
            tool=f"{docker_server}.docker_available",
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
            tool=f"{docker_server}.start_container",
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
    docker_server = state.get("docker_server") or DEFAULT_DOCKER_SERVER
    tasks = build_plan(request, docker_server=docker_server)
    return {
        "tasks": tasks,
        "verification_request": VerificationRequest(
            container_name=request.container_name,
            expected_port=expected_port(request),
            health_path=request.health_path,
            log_tail=request.log_tail,
            docker_server=docker_server,
            probe_host=state.get("probe_host"),
        ),
        "plan_explanation": (
            f"{len(tasks)} tasks: verify the daemon, build {request.image!r}, "
            f"start {request.container_name!r} on {docker_server}. "
            f"Every mutating task needs approval."
        ),
    }


async def verify_stage(state: DeploymentWorkflowState) -> dict[str, Any]:
    """The VERIFY node.

    Takes the ``VerificationRequest`` built during PLAN rather than rebuilding one
    from loose state keys, so the port and health path the plan promised are the
    ones actually checked.

    An ``evidence_hook`` runs here, inside VERIFY and before history is written,
    so target-specific observations (a probe from inside the instance, a
    CloudWatch datapoint) land in the same record as the checks they supplement.
    A hook that raises degrades to one warning of evidence rather than failing a
    verification that already succeeded.
    """
    request: VerificationRequest = state["verification_request"]
    result = await verify_deployment(request)

    hook: Callable[[], Awaitable[list[Evidence]]] | None = state.get("evidence_hook")
    if hook is not None:
        try:
            result.evidence.extend(await hook())
        except Exception as exc:  # noqa: BLE001 - diagnostics must not fail the verdict
            logger.warning("verification evidence hook failed", extra={"error": str(exc)})
            result.evidence.append(
                Evidence(
                    source="verification.evidence_hook",
                    detail=f"additional diagnostics were unavailable: {type(exc).__name__}",
                )
            )

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


async def recover_stage(state: DeploymentWorkflowState) -> dict[str, Any]:
    """RECOVER: bounded automatic repair, when policy permits it.

    A *separate* node after DEBUG rather than part of it, so the diagnosis is
    produced whether or not acting on it is allowed. When self-healing is
    disabled this still runs the investigation and returns immediately, which
    keeps "diagnose" and "act" separable at the API surface too.
    """
    request: DockerDeployRequest = state["request"]
    verification: VerificationResult | None = state.get("result")
    policy: SelfHealingPolicy = state.get("self_healing_policy") or SelfHealingPolicy()
    docker_server = state.get("docker_server") or DEFAULT_DOCKER_SERVER

    outcome = await run_recovery(
        policy,
        verification=verification,
        stop_reason=state.get("stop_reason"),
        stop_task_id=state.get("stop_task_id"),
        container=request.container_name,
        image=request.image,
        repository_path=request.repository_path,
        human_approved=bool(state.get("self_healing_human_approved")),
        verify=_build_verifier(
            request,
            docker_server=docker_server,
            probe_host=state.get("probe_host"),
        ),
        redeploy=_build_redeployer(request, docker_server=docker_server),
    )

    logger.info(
        "deployment workflow recovery finished",
        extra={
            "recovered": outcome.recovered,
            "attempts_used": outcome.attempts_used,
            "budget": outcome.budget,
            "escalated": outcome.escalated,
            "reason": str(outcome.escalation_reason) if outcome.escalation_reason else None,
        },
    )
    return {"recovery": outcome, "recovered": outcome.recovered, "incident": outcome.incident}


def _build_verifier(
    request: DockerDeployRequest,
    *,
    docker_server: str = DEFAULT_DOCKER_SERVER,
    probe_host: str | None = None,
) -> Any:
    """A verifier bound to this request's container, port and server."""
    from app.agents.deployment_verification import verify_deployment

    async def verify() -> VerificationResult:
        return await verify_deployment(
            VerificationRequest(
                container_name=request.container_name,
                expected_port=expected_port(request),
                health_path=request.health_path,
                log_tail=request.log_tail,
                docker_server=docker_server,
                probe_host=probe_host,
            )
        )

    return verify


def _build_redeployer(
    request: DockerDeployRequest, *, docker_server: str = DEFAULT_DOCKER_SERVER
) -> Any:
    """Redeploy this request's container, without rebuilding the image.

    Restart is the fix; rebuilding is a separate, riskier action that has to be
    approved on its own. Recompiling the image here would quietly escalate a
    restart into a rebuild.
    """
    from app.agents.execution_engine import execute_run
    from app.models.execution import ExecutionRequest, Task

    async def redeploy() -> Any:
        return await execute_run(
            ExecutionRequest(
                tasks=[
                    Task(
                        id="redeploy",
                        title=f"Restart {request.container_name}",
                        tool=f"{docker_server}.start_container",
                        arguments={
                            "image": request.image,
                            "name": request.container_name,
                            "ports": list(request.ports),
                        },
                        requires_approval=True,
                        timeout_seconds=180.0,
                    )
                ],
                approve=True,
                approval_reference="self-healing:restart",
            )
        )

    return redeploy


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
    builder.add_node("recover", recover_stage)

    builder.add_edge(START, "plan")
    builder.add_edge("plan", "execute")
    builder.add_conditional_edges(
        "execute", route_after_execute, {"verify": "verify", "debug": "debug", "end": END}
    )
    builder.add_conditional_edges("verify", route_after_verify, {"end": END, "debug": "debug"})
    builder.add_edge("debug", "recover")
    builder.add_edge("recover", END)

    return builder.compile()


deployment_workflow_graph = build_workflow_graph()


async def run_deployment_workflow(
    request: DockerDeployRequest,
    *,
    self_healing_policy: SelfHealingPolicy | None = None,
    self_healing_human_approved: bool = False,
    docker_server: str = DEFAULT_DOCKER_SERVER,
    probe_host: str | None = None,
    target: DeploymentTarget | None = None,
    evidence_hook: Callable[[], Awaitable[list[Evidence]]] | None = None,
    record: DeploymentRecord | None = None,
) -> dict[str, Any]:
    """Run PLAN → EXECUTE → VERIFY → END for one deployment.

    ``request.approve`` is threaded into the EXECUTE stage unchanged. The workflow
    never manufactures approval: a request without ``approve: true`` has its build
    and run refused, exactly as a direct deployment would.

    The three target parameters exist for remote deployments and default to the
    local daemon: ``docker_server`` qualifies every tool name the plan and the
    verifier use, ``probe_host`` says where the health endpoint is reachable
    from, and ``target`` is recorded in history so two runs of the same commit on
    different machines stay distinguishable. None of them can widen approval --
    the server only names *where* a call goes, and the policy still decides
    *whether* it may go.

    ``record`` lets a caller that already opened the history row continue it
    instead of opening a second: the EC2 flow begins one before its first SSH
    connection so a hang still leaves a trace, and two rows for one deployment
    would make history count runs that never happened.
    """
    memory = get_memory_service()

    # Recorded before the graph runs, so an interrupted or killed deployment still
    # appears in history. Without this, "it vanished" and "it never ran" look the
    # same from the history endpoint.
    if record is None:
        record = await memory.begin_deployment(request=request, target=target)
    started_at = record.started_at

    state: DeploymentWorkflowState = {
        "request": request,
        "approve": request.approve,
        "approval_reference": request.approval_reference,
        "stop_on_critical_failure": True,
        "dry_run": request.dry_run,
        "self_healing_policy": self_healing_policy or SelfHealingPolicy(),
        "self_healing_human_approved": self_healing_human_approved,
        "docker_server": docker_server,
        "probe_host": probe_host,
        "target": target,
        "evidence_hook": evidence_hook,
    }

    try:
        result = await deployment_workflow_graph.ainvoke(state)
    except BaseException:
        # The graph raised. Left as in_progress rather than marked failed: the
        # service does not know why it failed, and inventing a reason here would
        # put a false conclusion in the record. An interrupted run reads as
        # interrupted.
        raise

    await memory.record_deployment(
        deployment_id=record.deployment_id,
        request=request,
        tasks=result.get("tasks"),
        execution=result.get("response"),
        verification=result.get("result"),
        incident=result.get("incident"),
        recovery=result.get("recovery"),
        recovered=bool(result.get("recovered")),
        stopped=bool(result.get("stopped")),
        stop_reason=result.get("stop_reason"),
        started_at=started_at,
        target=target,
    )

    return {
        "deployment_id": record.deployment_id,
        "plan": result.get("tasks", []),
        "execution": result.get("response"),
        "verification": result.get("result"),
        "debug": result.get("debug"),
        "recovery": result.get("recovery"),
        "recovered": bool(result.get("recovered")),
        "incident": result.get("incident"),
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
