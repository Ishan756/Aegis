"""Local Docker deployment graph.

    START → inspect_repository → build_image → start_container
          → check_health → collect_logs → END

Each stage reaches Docker through the MCP tool manager, so this module holds no
Docker client, builds no command line and contains no subprocess call. The
execution policy is what separates the stages: ``build_image`` and
``start_container`` are medium risk, so at the default threshold they are refused
until a human approves them, and a refusal is reported rather than papered over.

Two properties are load-bearing:

- **The workflow never approves itself.** :attr:`ToolCallRequest.approval_granted`
  comes from the caller's request and nowhere else, so an agent cannot talk its
  way past the gate by setting the flag on its own request object.
- **A stage that fails does not silently continue into a fake success.** Each
  mutating stage short-circuits when its predecessor did not succeed, and the
  stages after a failure are recorded as ``skipped``. A deployment that never
  started reports exactly that, rather than reporting healthy with no logs.

Local only. Nothing here contacts AWS or any remote registry.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.core.config import get_settings
from app.core.exceptions import NotFoundError, PermissionDeniedError, UpstreamError, ValidationError
from app.core.logging import get_logger
from app.models.docker import (
    BuildOutcome,
    DeploymentResult,
    DeploymentStage,
    DeploymentStep,
    DockerAvailabilityResponse,
    DockerDeployRequest,
    DockerDeployState,
    HealthOutcome,
    LogOutcome,
    RunOutcome,
    StepOutcome,
)
from app.models.mcp import ToolCallRequest
from app.models.repository import RepositoryProfile
from app.services.mcp_manager import get_manager
from app.services.repository import detect_stack, scan_repository

logger = get_logger(__name__)

#: Server name the Docker tools are expected to be registered under.
DOCKER_SERVER = "docker"


class _ApprovalRequired(UpstreamError):
    """A mutating tool was refused because no human approved it."""


async def _call_tool(
    tool: str,
    arguments: dict[str, Any],
    *,
    requested_by: str,
    reason: str,
    approve: bool,
    approval_reference: str | None,
) -> dict[str, Any]:
    """Invoke a Docker tool through the MCP layer.

    ``approve`` is forwarded to the policy and never set by this module, so the
    gate cannot be bypassed from inside the workflow.
    """
    result = await get_manager().call_tool(
        ToolCallRequest(
            tool_name=f"{DOCKER_SERVER}.{tool}",
            arguments=arguments,
            requested_by=requested_by,
            reason=reason,
            approval_granted=approve,
            approval_reference=approval_reference if approve else None,
        )
    )

    if result.success:
        return result.content if isinstance(result.content, dict) else {}

    message = result.error_message or result.error_code or "unknown error"
    if result.error_code in {"policy_denied", "approval_required"}:
        raise _ApprovalRequired(f"{tool} was refused: {message}")
    raise UpstreamError(f"Docker tool {tool!r} failed: {message}")


def _step(
    stage: DeploymentStage,
    outcome: StepOutcome,
    *,
    tool: str | None = None,
    approved: bool | None = None,
    detail: str = "",
    duration_seconds: float | None = None,
) -> DeploymentStep:
    """Build a step record.

    Nodes must *return* their updates: LangGraph builds each node's input state
    from the previous node's return value, so mutating the dict that was passed
    in changes nothing downstream. Every node here therefore returns a plain dict
    and treats its state argument as read-only.
    """
    return DeploymentStep(
        stage=stage,
        outcome=outcome,
        tool=f"{DOCKER_SERVER}.{tool}" if tool else None,
        approved=approved,
        detail=detail,
        duration_seconds=duration_seconds,
    )


def _steps(state: DockerDeployState) -> list[DeploymentStep]:
    """A copy of the recorded steps, safe to append to."""
    return list(state.get("steps") or [])


def _notes(state: DockerDeployState) -> list[str]:
    """A copy of the accumulated notes, safe to append to."""
    return list(state.get("notes") or [])


def _resolve_repository_path(relative: str) -> Path:
    """Resolve the deployment target and confine it to the repository root.

    This is the first security boundary: an LLM-chosen path cannot be used to
    reach outside the configured repository root, and symlinks are resolved
    before the check so one cannot escape it either.
    """
    settings = get_settings()
    allowed_root = settings.repository_root_resolved

    if not relative or not relative.strip():
        # Path("") resolves to the working directory, which would deploy the
        # whole root instead of rejecting the request.
        raise ValidationError("A repository path is required.")

    candidate = Path(relative).expanduser()
    if not candidate.is_absolute():
        candidate = allowed_root / candidate
    try:
        resolved = candidate.resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValidationError("The supplied path could not be resolved.") from exc

    if resolved != allowed_root and allowed_root not in resolved.parents:
        raise PermissionDeniedError(
            "The requested path is outside the allowed repository root.",
            details={"repository_root": str(allowed_root)},
        )
    if not resolved.exists():
        raise NotFoundError("No such repository path.", details={"path": str(resolved)})
    if not resolved.is_dir():
        raise ValidationError("The repository path is not a directory.")
    return resolved


async def inspect_repository(state: DockerDeployState) -> dict[str, object]:
    """Confirm Docker works, then profile the target repository.

    Both checks happen before anything is built, so a missing daemon or a
    repository with no Dockerfile fails in seconds rather than after a build. The
    profile is produced by the same detector the local analyzer uses, so the two
    cannot disagree about what a repository contains.
    """
    target = _resolve_repository_path(state["repository_path"])

    availability = await _call_tool(
        "docker_available",
        {},
        requested_by="docker-deployment",
        reason="confirming the Docker daemon is reachable before deploying",
        approve=False,
        approval_reference=None,
    )
    if not availability.get("available"):
        raise UpstreamError(
            "Docker is not available: "
            f"{availability.get('reason') or 'the daemon did not respond.'}"
        )

    inventory = scan_repository(target)
    profile: RepositoryProfile = detect_stack(inventory)

    notes = _notes(state)
    notes.append(
        f"Deployed from {target} on {availability.get('os', 'unknown')}"
        f"/{availability.get('arch', 'unknown')} using Docker "
        f"{availability.get('server_version', 'unknown')}."
    )

    steps = _steps(state)
    steps.append(
        _step(
            "inspect",
            "ok",
            tool="docker_available",
            detail=(
                f"{profile.file_count} files, "
                f"{'has' if profile.has_dockerfile else 'no'} Dockerfile"
            ),
        )
    )

    if not profile.has_dockerfile:
        # Recorded rather than raised: a dry run should still be able to report
        # this, and a caller may be deploying an image built elsewhere.
        notes.append(
            f"No Dockerfile was found in {target}; the build step will fail unless one is added."
        )

    logger.info(
        "docker deployment inspected",
        extra={
            "target": str(target),
            "image": state["image"],
            "container": state["container_name"],
            "has_dockerfile": profile.has_dockerfile,
        },
    )
    return {
        "target": str(target),
        "profile": profile,
        "availability": availability,
        "steps": steps,
        "notes": notes,
    }


async def build_image(state: DockerDeployState) -> dict[str, object]:
    """Build the image, if this is not a dry run.

    This executes the Dockerfile's ``RUN`` steps, which is repository code. It is
    medium risk, so the policy requires approval, and a refusal is surfaced to
    the caller rather than treated as a skipped step.
    """
    steps = _steps(state)
    notes = _notes(state)

    if state.get("dry_run"):
        steps.append(_step("build", "skipped", detail="dry run: no image was built"))
        return {"steps": steps, "notes": notes}

    started = time.monotonic()
    target = state["target"]
    try:
        payload = await _call_tool(
            "build_image",
            {
                "context_path": target,
                "tag": state["image"],
                "dockerfile": state["dockerfile"],
            },
            requested_by="docker-deployment",
            reason=f"building {state['image']} from {target}",
            approve=bool(state.get("approve")),
            approval_reference=state.get("approval_reference"),
        )
    except _ApprovalRequired as exc:
        duration = round(time.monotonic() - started, 3)
        steps.append(
            _step(
                "build",
                "refused",
                tool="build_image",
                approved=False,
                detail=str(exc),
                duration_seconds=duration,
            )
        )
        notes.append(
            "The build was refused because it requires human approval. Re-submit "
            "with approve=true and an approval_reference to proceed."
        )
        return {
            "steps": steps,
            "notes": notes,
            "build": BuildOutcome(
                attempted=True, image=state["image"], context=target, error=str(exc)
            ),
        }
    except UpstreamError as exc:
        # A build that fails for a real reason (a bad Dockerfile, a missing base
        # image) is a result to report, not a crash. Raising here would abort the
        # graph and lose the build log that explains the failure.
        duration = round(time.monotonic() - started, 3)
        steps.append(
            _step(
                "build",
                "failed",
                tool="build_image",
                approved=bool(state.get("approve")),
                detail=str(exc),
                duration_seconds=duration,
            )
        )
        return {
            "steps": steps,
            "notes": notes,
            "build": BuildOutcome(
                attempted=True,
                success=False,
                image=state["image"],
                context=target,
                duration_seconds=duration,
                error=str(exc),
            ),
        }

    duration = round(time.monotonic() - started, 3)
    outcome = BuildOutcome(
        attempted=True,
        success=True,
        image=payload.get("image"),
        image_id=payload.get("image_id"),
        context=payload.get("context"),
        dockerfile=payload.get("dockerfile"),
        duration_seconds=duration,
        log_tail=str(payload.get("build_log_tail") or "").splitlines()[-40:],
        truncated=bool(payload.get("output_truncated")),
    )
    steps.append(
        _step(
            "build",
            "ok",
            tool="build_image",
            approved=bool(state.get("approve")),
            detail=f"built {outcome.image_id or outcome.image} in {duration}s",
            duration_seconds=duration,
        )
    )
    return {"steps": steps, "notes": notes, "build": outcome}


async def start_container(state: DockerDeployState) -> dict[str, object]:
    """Start the container, unless the build did not succeed.

    Nothing downstream runs when this fails, so a deployment that never started
    cannot report itself healthy.
    """
    steps = _steps(state)
    notes = _notes(state)

    if state.get("dry_run"):
        steps.append(
            _step("run", "skipped", detail=f"dry run: would start {state['container_name']}")
        )
        return {"steps": steps, "notes": notes}

    build: BuildOutcome | None = state.get("build")
    if build is None or not build.success:
        reason = (
            "no build attempt was made"
            if build is None
            else build.error or "the build did not succeed"
        )
        steps.append(_step("run", "skipped", detail=f"not started: {reason}"))
        return {"steps": steps, "notes": notes}

    started = time.monotonic()
    try:
        payload = await _call_tool(
            "start_container",
            {
                "image": state["image"],
                "name": state["container_name"],
                "ports": list(state.get("ports") or []),
            },
            requested_by="docker-deployment",
            reason=f"starting {state['container_name']} locally for verification",
            approve=bool(state.get("approve")),
            approval_reference=state.get("approval_reference"),
        )
    except _ApprovalRequired as exc:
        duration = round(time.monotonic() - started, 3)
        steps.append(
            _step(
                "run",
                "refused",
                tool="start_container",
                approved=False,
                detail=str(exc),
                duration_seconds=duration,
            )
        )
        notes.append("Starting the container was refused because it requires human approval.")
        return {
            "steps": steps,
            "notes": notes,
            "run": RunOutcome(attempted=True, container=state["container_name"], error=str(exc)),
        }
    except UpstreamError as exc:
        # Reported rather than raised: a port clash or a duplicate container name
        # is exactly the kind of failure the caller needs to see in the response,
        # and aborting here would hide the build that already succeeded.
        duration = round(time.monotonic() - started, 3)
        steps.append(
            _step(
                "run",
                "failed",
                tool="start_container",
                approved=bool(state.get("approve")),
                detail=str(exc),
                duration_seconds=duration,
            )
        )
        return {
            "steps": steps,
            "notes": notes,
            "run": RunOutcome(
                attempted=True,
                success=False,
                container=state["container_name"],
                image=state["image"],
                error=str(exc),
            ),
        }

    outcome = RunOutcome(
        attempted=True,
        success=True,
        container=payload.get("container"),
        container_id=payload.get("container_id"),
        image=payload.get("image"),
        ports=list(payload.get("ports") or []),
    )
    steps.append(
        _step(
            "run",
            "ok",
            tool="start_container",
            approved=bool(state.get("approve")),
            detail=f"started {outcome.container}",
            duration_seconds=round(time.monotonic() - started, 3),
        )
    )
    return {"steps": steps, "notes": notes, "run": outcome}


async def check_health(state: DockerDeployState) -> dict[str, object]:
    """Poll the container's health check until it settles or time runs out.

    Health comes from the image's own ``HEALTHCHECK``. There is no health command
    parameter anywhere in this path, because a health command would be arbitrary
    execution inside a container. An image with no health check reports
    ``no_healthcheck`` and is never counted as healthy.
    """
    steps = _steps(state)
    notes = _notes(state)

    run: RunOutcome | None = state.get("run")
    if state.get("dry_run") or run is None or not run.success:
        steps.append(_step("health", "skipped", detail="no container was started"))
        return {"steps": steps, "notes": notes}

    timeout = float(state.get("health_timeout_seconds") or 60.0)
    interval = float(state.get("health_poll_interval_seconds") or 2.0)
    deadline = time.monotonic() + timeout
    started = time.monotonic()

    detail = ""
    state_value = "unknown"
    failing = 0
    exit_code: int | None = None

    while True:
        try:
            payload = await _call_tool(
                "container_health",
                {"name": state["container_name"]},
                requested_by="docker-deployment",
                reason="waiting for the deployed container to report healthy",
                approve=False,
                approval_reference=None,
            )
        except UpstreamError as exc:
            steps.append(_step("health", "failed", tool="container_health", detail=str(exc)))
            return {
                "steps": steps,
                "notes": notes,
                "health": HealthOutcome(
                    checked=True, state="unknown", detail=str(exc), healthy=False
                ),
            }

        state_value = str(payload.get("health") or "unknown")
        detail = str(payload.get("detail") or "")
        failing = int(payload.get("failing_streak") or 0)

        # A pending or passing check settles immediately; a failing one settles
        # too, because Docker retries on its own schedule and re-polling here
        # would add load without deciding anything sooner.
        if state_value in {"healthy", "unhealthy", "no_healthcheck"}:
            break
        if time.monotonic() >= deadline:
            break
        await asyncio.sleep(min(interval, max(0.0, deadline - time.monotonic())))

    if state_value == "healthy":
        status = await _call_tool(
            "container_status",
            {"name": state["container_name"]},
            requested_by="docker-deployment",
            reason="reading the exit code and restart count of a healthy container",
            approve=False,
            approval_reference=None,
        )
        exit_code = status.get("exit_code")

    waited = round(time.monotonic() - started, 2)
    outcome = HealthOutcome(
        checked=True,
        state=state_value,  # type: ignore[arg-type]
        detail=detail,
        failing_streak=failing,
        waited_seconds=waited,
        exit_code=exit_code,
        healthy=state_value == "healthy",
    )
    steps.append(
        _step(
            "health",
            "ok" if outcome.healthy else "unhealthy",
            tool="container_health",
            detail=detail,
            duration_seconds=waited,
        )
    )

    if state_value == "starting":
        notes.append(
            f"The container was still 'starting' after {waited}s and never reported a "
            "result. Treat this deployment as unverified."
        )
    elif state_value == "no_healthcheck":
        notes.append(
            "The image defines no HEALTHCHECK, so 'running' was never verified as "
            "'working'. Add a HEALTHCHECK to the image."
        )

    return {"steps": steps, "notes": notes, "health": outcome}


async def collect_logs(state: DockerDeployState) -> dict[str, object]:
    """Collect capped container logs, which is how a failure gets explained."""
    steps = _steps(state)
    notes = _notes(state)

    run: RunOutcome | None = state.get("run")
    if run is None or not run.success:
        steps.append(_step("logs", "skipped", detail="no container was started"))
        return {"steps": steps, "notes": notes}

    try:
        payload = await _call_tool(
            "container_logs",
            {"name": state["container_name"], "tail": state["log_tail"]},
            requested_by="docker-deployment",
            reason="collecting logs from the deployed container",
            approve=False,
            approval_reference=None,
        )
    except UpstreamError as exc:
        steps.append(_step("logs", "failed", tool="container_logs", detail=str(exc)))
        return {
            "steps": steps,
            "notes": notes,
            "logs": LogOutcome(collected=False, error=str(exc)),
        }

    outcome = LogOutcome(
        collected=True,
        logs=str(payload.get("logs") or "").splitlines(),
        error_logs=str(payload.get("error_logs") or "").splitlines(),
        truncated=bool(payload.get("truncated")),
    )
    steps.append(
        _step(
            "logs",
            "ok",
            tool="container_logs",
            detail=(f"{len(outcome.logs)} log lines{', truncated' if outcome.truncated else ''}"),
        )
    )
    if outcome.truncated:
        notes.append("Container logs were truncated by the server's cap; this is a fragment.")
    return {"steps": steps, "notes": notes, "logs": outcome}


def build_docker_deployment_graph() -> CompiledStateGraph:
    """Assemble and compile the Docker deployment graph."""
    graph = StateGraph(DockerDeployState)
    graph.add_node("inspect_repository", inspect_repository)
    graph.add_node("build_image", build_image)
    graph.add_node("start_container", start_container)
    graph.add_node("check_health", check_health)
    graph.add_node("collect_logs", collect_logs)

    graph.add_edge(START, "inspect_repository")
    graph.add_edge("inspect_repository", "build_image")
    graph.add_edge("build_image", "start_container")
    graph.add_edge("start_container", "check_health")
    graph.add_edge("check_health", "collect_logs")
    graph.add_edge("collect_logs", END)

    return graph.compile()


#: Shared compiled graph; compiling has no side effects.
docker_deployment_graph = build_docker_deployment_graph()


def _succeeded(state: DockerDeployState) -> bool:
    """A deployment succeeded only if it started *and* reported healthy."""
    health: HealthOutcome | None = state.get("health")
    run: RunOutcome | None = state.get("run")
    return bool(run and run.success and health and health.healthy)


async def deploy_locally(request: DockerDeployRequest) -> DeploymentResult:
    """Run the workflow and return what happened at every stage."""
    state: DockerDeployState = {
        "repository_path": request.repository_path,
        "image": request.image,
        "container_name": request.container_name,
        "dockerfile": request.dockerfile,
        "ports": list(request.ports),
        "approve": request.approve,
        "approval_reference": request.approval_reference,
        "dry_run": request.dry_run,
        "health_timeout_seconds": request.health_timeout_seconds,
        "health_poll_interval_seconds": request.health_poll_interval_seconds,
        "log_tail": request.log_tail,
        "steps": [],
        "notes": [
            "This deployment targets the local Docker daemon only. Nothing was "
            "pushed to a registry or to AWS.",
        ],
        "started_at": datetime.now(UTC),
    }

    result = await docker_deployment_graph.ainvoke(state)
    health: HealthOutcome | None = result.get("health")
    run: RunOutcome | None = result.get("run")

    if not _succeeded(result):
        # Say which stage failed, because "did not succeed" alone leaves the
        # caller to diff the steps against the expected order themselves.
        failed = next(
            (
                step
                for step in reversed(result.get("steps") or [])
                if step.outcome in {"failed", "refused", "unhealthy"}
            ),
            None,
        )
        notes = list(result.get("notes") or [])
        if failed is not None:
            notes.append(
                f"The deployment did not succeed; stage {failed.stage!r} reported {failed.outcome}."
            )

    logger.info(
        "docker deployment finished",
        extra={
            "image": request.image,
            "container": request.container_name,
            "succeeded": _succeeded(result),
            "health": health.state if health else "unknown",
            "container_started": bool(run and run.success),
        },
    )

    return DeploymentResult(
        image=request.image,
        container=request.container_name,
        succeeded=_succeeded(result),
        profile=result.get("profile"),
        steps=list(result.get("steps") or []),
        build=result.get("build"),
        run=result.get("run"),
        health=result.get("health"),
        logs=result.get("logs"),
        notes=list(result.get("notes") or []),
    )


async def check_docker_availability() -> DockerAvailabilityResponse:
    """Report whether the Docker daemon is reachable."""
    payload = await _call_tool(
        "docker_available",
        {},
        requested_by="docker-deployment",
        reason="reporting Docker availability",
        approve=False,
        approval_reference=None,
    )
    return DockerAvailabilityResponse(
        available=bool(payload.get("available")),
        reason=payload.get("reason"),
        client_version=payload.get("client_version"),
        server_version=payload.get("server_version"),
        os=payload.get("os"),
        arch=payload.get("arch"),
        context_root=str(payload.get("context_root") or ""),
        notes=list(payload.get("notes") or []),
    )


__all__ = [
    "build_docker_deployment_graph",
    "check_docker_availability",
    "check_health",
    "collect_logs",
    "deploy_locally",
    "docker_deployment_graph",
    "inspect_repository",
    "start_container",
]
