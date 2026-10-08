"""Deployment verification agent.

Checks seven things after a deployment, in a fixed order, using only read-only
Docker MCP tools. The order is not cosmetic: each check assumes the previous one
held. A container that does not exist cannot be probed for logs, and reporting
"logs are missing" as a log failure would bury the one fact that matters.

Read-only by construction. Every tool called here is annotated ``read_only_hint``
and none of them can start, stop or rebuild anything. Verification that mutates
the thing it is verifying is not verification.

The verdict has three states. ``WARNING`` exists because "running, answering, but
the log shows the database connection retrying" is neither a clean success nor a
failure, and forcing it into one of those two would mislead whoever reads it.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.core.exceptions import UpstreamError
from app.models.mcp import ToolCallRequest
from app.models.verification import (
    BENIGN_LOG_PATTERNS,
    CHECK_DESCRIPTIONS,
    CHECK_ORDER,
    FATAL_LOG_PATTERNS,
    CheckOutcome,
    Evidence,
    VerificationCheck,
    VerificationRequest,
    VerificationResult,
    VerificationState,
    VerificationStatus,
)
from app.services.mcp_manager import get_manager

logger = logging.getLogger(__name__)

DOCKER_SERVER = "docker"
REQUESTED_BY = "deployment-verification"

_FATAL_RE = tuple(re.compile(pattern, re.IGNORECASE) for pattern in FATAL_LOG_PATTERNS)
_BENIGN_RE = tuple(re.compile(pattern, re.IGNORECASE) for pattern in BENIGN_LOG_PATTERNS)

#: A body excerpt this long is enough to show what an endpoint returned without
#: putting an entire HTML error page into the audit trail.
EXCERPT_LIMIT = 300


async def _docker_tool(
    tool: str, arguments: dict[str, Any], request: VerificationRequest | None = None
) -> dict[str, Any]:
    """Call a read-only Docker tool through the policy-enforcing MCP layer.

    The server comes from the request, so verifying a remote target runs against
    the scoped server opened for that target instead of the local daemon.
    ``http_probe`` additionally gains the request's ``probe_host`` here rather
    than at each call site: one injection point means no probe can quietly
    forget it and report loopback health as if it were the target's.
    """
    server = request.docker_server if request is not None else DOCKER_SERVER
    if tool == "http_probe" and request is not None and request.probe_host:
        arguments = {**arguments, "host": request.probe_host}
    result = await get_manager().call_tool(
        ToolCallRequest(
            tool_name=f"{server}.{tool}",
            arguments=arguments,
            requested_by=REQUESTED_BY,
            reason="verifying a completed deployment",
        )
    )
    if not result.success:
        raise UpstreamError(
            result.error_message or f"Docker tool {tool!r} failed during verification."
        )
    return result.content if isinstance(result.content, dict) else {}


def _source(request: VerificationRequest | None, tool: str) -> str:
    """Evidence source naming the server that actually answered.

    Defaults to the local server so an evidence line read after the fact says
    which daemon the observation came from, rather than claiming ``docker.``
    for a probe that ran against a remote target.
    """
    server = request.docker_server if request is not None else DOCKER_SERVER
    return f"{server}.{tool}"


def _check(
    name: str, outcome: CheckOutcome, detail: str, *evidence: Evidence, skipped: str | None = None
) -> VerificationCheck:
    """Build one check, defaulting to the description when no detail is given."""
    return VerificationCheck(
        name=name,
        outcome=outcome,
        detail=detail or CHECK_DESCRIPTIONS.get(name, ""),
        evidence=list(evidence),
        skipped_reason=skipped,
    )


def _skipped(name: str, reason: str) -> VerificationCheck:
    """A check that could not run because a prerequisite was missing."""
    return VerificationCheck(
        name=name,
        outcome="skipped",
        detail=CHECK_DESCRIPTIONS.get(name, ""),
        skipped_reason=reason,
    )


def _not_applicable(name: str, reason: str) -> VerificationCheck:
    """A check that does not apply to this deployment, and is not a concern."""
    return VerificationCheck(
        name=name,
        outcome="not_applicable",
        detail=CHECK_DESCRIPTIONS.get(name, ""),
        skipped_reason=reason,
    )


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


async def check_container_exists(
    container: str, request: VerificationRequest
) -> tuple[dict[str, Any] | None, VerificationCheck]:
    """Confirm the container exists and capture its full record once.

    One call serves three checks. Re-reading the status per check would triple the
    MCP traffic and, worse, could observe three different moments in time.
    """
    try:
        status = await _docker_tool("container_status", {"name": container}, request)
    except UpstreamError as error:
        return None, _check(
            "container_exists",
            "fail",
            f"No container named {container!r}: {error}",
            Evidence(
                source=_source(request, "container_status"),
                detail=str(error)[:EXCERPT_LIMIT],
            ),
        )

    return status, _check(
        "container_exists",
        "pass",
        f"Container {container!r} exists.",
        Evidence(
            source=_source(request, "container_status"),
            value=str(status.get("status")),
            detail=f"image={status.get('image')} created={status.get('created')}",
        ),
    )


def check_container_running(
    status: dict[str, Any], request: VerificationRequest | None = None
) -> VerificationCheck:
    """The container must be running, and must not have been OOM-killed."""
    running = bool(status.get("running"))
    exit_code = status.get("exit_code")
    oom = bool(status.get("oom_killed"))

    if oom:
        return _check(
            "container_running",
            "fail",
            "The container was OOM-killed.",
            Evidence(source=_source(request, "container_status"), detail="OOMKilled=true"),
        )
    if not running:
        return _check(
            "container_running",
            "fail",
            f"The container is not running "
            f"(state={status.get('status')!r}, exit_code={exit_code}).",
            Evidence(
                source=_source(request, "container_status"),
                value=str(status.get("status")),
                detail=f"exit_code={exit_code} restart_count={status.get('restart_count')}",
            ),
        )
    return _check(
        "container_running",
        "pass",
        "The container is running.",
        Evidence(
            source=_source(request, "container_status"),
            value=str(status.get("status")),
            detail=f"started_at={status.get('started_at')}",
        ),
    )


async def check_port_available(
    status: dict[str, Any], request: VerificationRequest
) -> VerificationCheck:
    """The expected port must be published and actually answer.

    Both halves matter. A published port that nothing is listening on is a
    container that will pass every status check and fail every real request, so
    the port is confirmed by probing it rather than by reading the mapping.
    """
    expected = request.expected_port
    published = status.get("ports") or []

    if expected is None:
        observed = ", ".join(
            f"{item.get('host_port')}:{item.get('container_port')}" for item in published
        )
        if published:
            return _check(
                "port_available",
                "warn",
                f"No expected port was supplied; Docker reports {observed}.",
                Evidence(source=_source(request, "container_status"), detail=f"ports={observed}"),
            )
        return _not_applicable(
            "port_available",
            "No expected port was supplied and the container publishes none.",
        )

    host_ports = {item.get("host_port") for item in published}
    if expected not in host_ports:
        return _check(
            "port_available",
            "fail",
            f"Port {expected} is not published. Docker reports {sorted(host_ports) or 'no ports'}.",
            Evidence(
                source=_source(request, "container_status"),
                value=str(expected),
                detail=f"published={sorted(port for port in host_ports if port)}",
            ),
        )

    probe = await _docker_tool("http_probe", {"port": expected, "path": "/"}, request)

    if not probe.get("reachable"):
        return _check(
            "port_available",
            "fail",
            f"Port {expected} is published but nothing answered: "
            f"{probe.get('error') or 'no response'}.",
            Evidence(
                source=_source(request, "http_probe"),
                detail=str(probe.get("error"))[:EXCERPT_LIMIT],
                value=f"port={expected}",
            ),
        )

    return _check(
        "port_available",
        "pass",
        f"Port {expected} is published and answered HTTP {probe.get('status')} "
        f"in {probe.get('latency_ms')}ms.",
        Evidence(
            source=_source(request, "http_probe"),
            value=str(probe.get("status")),
            detail=f"port={expected} latency_ms={probe.get('latency_ms')}",
        ),
    )


async def check_health_endpoint(
    port: int | None, request: VerificationRequest
) -> tuple[dict[str, Any] | None, VerificationCheck, VerificationCheck]:
    """Probe the health endpoint, then check its status code.

    Returns the probe payload so the caller can reuse it: probing twice would
    produce two different answers for the same question, and a status check based
    on a different request than the reachability check is not evidence of anything.
    """
    if port is None:
        reason = "No published port to probe."
        return None, _skipped("health_endpoint", reason), _skipped("health_status_code", reason)

    probe = await _docker_tool("http_probe", {"port": port, "path": request.health_path}, request)

    if not probe.get("reachable"):
        return (
            probe,
            _check(
                "health_endpoint",
                "fail",
                f"{request.health_path} on port {port} did not respond: "
                f"{probe.get('error') or 'no response'}.",
                Evidence(
                    source=_source(request, "http_probe"),
                    detail=str(probe.get("error"))[:EXCERPT_LIMIT],
                ),
            ),
            _skipped("health_status_code", "The endpoint did not respond, so there is no status."),
        )

    reachable = _check(
        "health_endpoint",
        "pass",
        f"{request.health_path} responded in {probe.get('latency_ms')}ms.",
        Evidence(
            source=_source(request, "http_probe"),
            value=str(probe.get("status")),
            detail=f"content_type={probe.get('content_type')} url={probe.get('url')}",
        ),
    )
    return probe, reachable, _check_health_status(probe, request)


def _check_health_status(probe: dict[str, Any], request: VerificationRequest) -> VerificationCheck:
    """The endpoint must return the status the caller expects, not merely a status."""
    observed = probe.get("status")
    expected = request.expected_status
    body = (probe.get("body") or "")[:EXCERPT_LIMIT]

    if observed == expected:
        return _check(
            "health_status_code",
            "pass",
            f"{request.health_path} returned {observed} as expected.",
            Evidence(
                source=_source(request, "http_probe"),
                value=str(observed),
                detail=body if body else None,
            ),
        )

    # 2xx/3xx when a specific code was expected is a warning, not a failure: the
    # endpoint answered, just not the way the caller asked.
    if expected != 200 and isinstance(observed, int) and 200 <= observed < 400:
        return _check(
            "health_status_code",
            "warn",
            f"{request.health_path} returned {observed}, not the expected {expected}.",
            Evidence(
                source=_source(request, "http_probe"), value=str(observed), detail=body or None
            ),
        )

    return _check(
        "health_status_code",
        "fail",
        f"{request.health_path} returned {observed}, expected {expected}.",
        Evidence(source=_source(request, "http_probe"), value=str(observed), detail=body or None),
    )


async def check_image_health(
    container: str, request: VerificationRequest | None = None
) -> VerificationCheck:
    """Report the image's own HEALTHCHECK verdict alongside the HTTP probe.

    Not one of the seven required checks, but folded into the evidence: an image
    health check that disagrees with an HTTP probe is a strong signal, and hiding
    that disagreement would make the report look cleaner than reality.
    """
    try:
        health = await _docker_tool("container_health", {"name": container}, request)
    except UpstreamError as error:
        return _check(
            "health_status_code",
            "warn",
            f"The image's own HEALTHCHECK could not be read: {error}",
            Evidence(
                source=_source(request, "container_health"),
                detail=str(error)[:EXCERPT_LIMIT],
            ),
        )

    state = health.get("state")
    if state == "no_healthcheck":
        return _check(
            "health_status_code",
            "warn",
            "The image declares no HEALTHCHECK, so only the HTTP probe could judge this.",
            Evidence(source=_source(request, "container_health"), value=str(state)),
        )
    if state == "unhealthy":
        return _check(
            "health_status_code",
            "fail",
            f"The image's HEALTHCHECK reports unhealthy: {health.get('detail', '')}",
            Evidence(source=_source(request, "container_health"), value=str(state)),
        )
    return _check(
        "health_status_code",
        "pass",
        f"The image's HEALTHCHECK reports {state}.",
        Evidence(source=_source(request, "container_health"), value=str(state)),
    )


async def check_logs(container: str, request: VerificationRequest) -> VerificationCheck:
    """Scan the application log for fatal errors.

    Patterns are matched against specific failure signatures, and benign lines are
    excluded first. A verifier that flags every line containing the word "error"
    produces noise, and a noisy verifier gets switched off -- which is worse than
    no verifier at all.
    """
    if not request.include_logs:
        return _not_applicable("logs_clean", "Log scanning was not requested.")

    try:
        logs = await _docker_tool(
            "container_logs", {"name": container, "tail": request.log_tail}, request
        )
    except UpstreamError as error:
        return _check(
            "logs_clean",
            "warn",
            f"Logs could not be read: {error}",
            Evidence(source=_source(request, "container_logs"), detail=str(error)[:EXCERPT_LIMIT]),
        )

    lines = logs.get("logs") or []
    if not lines:
        return _check(
            "logs_clean",
            "warn",
            "The container produced no log output, so it could not be judged.",
            Evidence(source=_source(request, "container_logs"), detail="0 lines"),
        )

    offenders: list[tuple[int, str]] = []
    for number, line in enumerate(lines, start=1):
        if any(pattern.search(line) for pattern in _BENIGN_RE):
            continue
        if any(pattern.search(line) for pattern in _FATAL_RE):
            offenders.append((number, line.strip()))

    if not offenders:
        return _check(
            "logs_clean",
            "pass",
            f"No fatal errors in {len(lines)} log line(s).",
            Evidence(
                source=_source(request, "container_logs"),
                detail=f"{len(lines)} lines scanned"
                + (", truncated" if logs.get("truncated") else ""),
            ),
        )

    worst = offenders[:5]
    excerpt = "; ".join(f"L{n}: {text[:120]}" for n, text in worst)
    return _check(
        "logs_clean",
        "fail",
        f"{len(offenders)} log line(s) look fatal.",
        Evidence(
            source=_source(request, "container_logs"),
            detail=excerpt,
            value=str(len(offenders)),
        ),
    )


async def check_dependencies(request: VerificationRequest) -> VerificationCheck:
    """Check that declared dependencies are reachable *where that is testable*.

    Honesty about what is and is not testable is the point here. If a dependency
    lives in another container, Aegis cannot reach into it, so the check reports
    "not verified" rather than claiming success from an absence of evidence.
    """
    if not request.dependencies:
        return _not_applicable(
            "dependencies_reachable",
            "No service dependencies were declared for this deployment.",
        )

    verified: list[str] = []
    unverifiable: list[str] = []

    for dependency in request.dependencies:
        if ":" in dependency:
            host_port, _, container_port = dependency.partition(":")
            if not host_port.isdigit() or not container_port.isdigit():
                unverifiable.append(dependency)
                continue
            try:
                probe = await _docker_tool(
                    "http_probe", {"port": int(host_port), "path": "/"}, request
                )
            except UpstreamError:
                unverifiable.append(dependency)
                continue
            if probe.get("reachable"):
                verified.append(dependency)
            else:
                return _check(
                    "dependencies_reachable",
                    "fail",
                    f"Dependency {dependency!r} is not reachable: {probe.get('error')}.",
                    Evidence(
                        source=_source(request, "http_probe"),
                        detail=str(probe.get("error"))[:EXCERPT_LIMIT],
                        value=dependency,
                    ),
                )
        else:
            # A bare name means another container's service. Aegis does not reach
            # into other containers' networks, so say so instead of guessing.
            unverifiable.append(dependency)

    evidence = [
        Evidence(
            source=_source(request, "http_probe"),
            detail=f"reachable: {', '.join(verified)}" if verified else "nothing probed",
        )
    ]
    if unverifiable:
        return _check(
            "dependencies_reachable",
            "warn",
            "Verified "
            + (", ".join(verified) if verified else "no dependency")
            + f"; could not verify {', '.join(unverifiable)} from this host.",
            *evidence,
        )
    return _check(
        "dependencies_reachable",
        "pass",
        f"All {len(verified)} declared dependencies responded.",
        *evidence,
    )


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


def _verdict(
    checks: list[VerificationCheck],
) -> tuple[VerificationStatus, list[str], list[str], str]:
    """Turn the checks into a verdict.

    Any failure is ``FAILED``; otherwise any warn or skipped check makes it
    ``WARNING``. A check skipped for a missing prerequisite is a warning rather
    than a pass, because absence of evidence is not evidence of health. A check
    that does not apply at all is ignored.
    """
    failures = [
        f"{item.name}: {item.detail or 'failed'}" for item in checks if item.outcome == "fail"
    ]
    # `not_applicable` is excluded on purpose: treating "this does not apply" as a
    # concern would make WARNING the outcome of every healthy deployment.
    warnings = [
        f"{item.name}: {item.skipped_reason or item.detail}"
        for item in checks
        if item.outcome in {"warn", "skipped"}
    ]

    if failures:
        return (
            "FAILED",
            failures,
            warnings,
            (
                "Do not route traffic to this deployment. Inspect the failure evidence, "
                "then re-run the deployment or roll back."
            ),
        )
    if warnings:
        return (
            "WARNING",
            failures,
            warnings,
            (
                "The deployment is serving, but something could not be verified. "
                "Accept the risk explicitly or fix the gap before promoting it."
            ),
        )
    return (
        "SUCCESS",
        failures,
        warnings,
        ("All checks passed. The deployment is verified and can be promoted."),
    )


async def verify_deployment(request: VerificationRequest) -> VerificationResult:
    """Run the seven checks in order and return the verdict."""
    started = time.monotonic()
    checks: list[VerificationCheck] = []

    status_record, exists_check = await check_container_exists(request.container_name, request)
    checks.append(exists_check)

    if status_record is None:
        # Nothing else is answerable. Marking them failed would bury the cause.
        for name in CHECK_ORDER[1:]:
            checks.append(_skipped(name, "The container does not exist."))
        status, failures, warnings, recommendation = _verdict(checks)
        return _build_result(request, checks, status, failures, warnings, recommendation, started)

    checks.append(check_container_running(status_record, request))

    port_check = await check_port_available(status_record, request)
    checks.append(port_check)

    probe, reachable_check, status_code_check = await check_health_endpoint(
        request.expected_port, request
    )
    checks.append(reachable_check)
    checks.append(status_code_check)

    # Only add the image's own verdict when the HTTP checks actually ran, so a
    # second health opinion never masquerades as the status check.
    if probe is not None and probe.get("reachable"):
        checks.append(await check_image_health(request.container_name, request))

    checks.append(await check_logs(request.container_name, request))
    checks.append(await check_dependencies(request))

    checks.sort(key=lambda item: CHECK_ORDER.index(item.name) if item.name in CHECK_ORDER else 99)

    status, failures, warnings, recommendation = _verdict(checks)
    result = _build_result(request, checks, status, failures, warnings, recommendation, started)
    logger.info(
        "deployment verified",
        extra={
            "container": request.container_name,
            "status": result.status,
            "failures": len(failures),
            "warnings": len(warnings),
        },
    )
    return result


def _build_result(
    request: VerificationRequest,
    checks: list[VerificationCheck],
    status: VerificationStatus,
    failures: list[str],
    warnings: list[str],
    recommendation: str,
    started: float,
) -> VerificationResult:
    """Collect the flat evidence list the API contract also exposes."""
    evidence = [item for check in checks for item in check.evidence]
    return VerificationResult(
        status=status,
        checks=checks,
        failures=failures,
        warnings=warnings,
        evidence=evidence,
        recommendation=recommendation,
        container=request.container_name,
        duration_seconds=round(time.monotonic() - started, 4),
    )


# ---------------------------------------------------------------------------
# LangGraph VERIFY stage
# ---------------------------------------------------------------------------


async def verify_stage(state: VerificationState) -> dict[str, Any]:
    """The VERIFY node."""
    request = VerificationRequest(
        container_name=state["container_name"],
        expected_port=state.get("expected_port"),
        health_path=state.get("health_path", "/health"),
        expected_status=state.get("expected_status", 200),
        log_tail=state.get("log_tail", 200),
        dependencies=list(state.get("dependencies", [])),
        include_logs=state.get("include_logs", True),
        docker_server=state.get("docker_server", DOCKER_SERVER),
        probe_host=state.get("probe_host"),
    )
    result = await verify_deployment(request)
    return {
        "result": result,
        "status": result.status,
        "checks": result.checks,
        "evidence": result.evidence,
        "failures": result.failures,
        "warnings": result.warnings,
        "recommendation": result.recommendation,
    }


def route_after_verify(state: VerificationState) -> str:
    """A failed or warning verification goes to DEBUG; only SUCCESS ends.

    WARNING routes to debug as well: a deployment that answers but is provably
    unhappy is the case where a human should look, and ending quietly would
    discard the evidence that prompted the concern.
    """
    return "end" if state.get("status") == "SUCCESS" else "debug"


def build_verification_graph() -> CompiledStateGraph:
    """VERIFY → END on success, VERIFY → DEBUG otherwise."""
    builder = StateGraph(VerificationState)
    builder.add_node("verify", verify_stage)
    builder.add_node("debug", _debug_stub_node)
    builder.add_edge(START, "verify")
    builder.add_conditional_edges("verify", route_after_verify, {"debug": "debug", "end": END})
    builder.add_edge("debug", END)
    return builder.compile()


async def _debug_stub_node(state: VerificationState) -> dict[str, Any]:
    """Placeholder so the graph shape is real before the logic exists."""
    from app.agents.debug_agent import assess_failure

    assessment = await assess_failure(
        result=state.get("result"),
        stop_reason=state.get("stop_reason"),
        stop_task_id=state.get("stop_task_id"),
    )
    return {"debug": assessment}


verification_graph = build_verification_graph()


__all__ = [
    "REQUESTED_BY",
    "build_verification_graph",
    "check_container_exists",
    "check_container_running",
    "check_dependencies",
    "check_health_endpoint",
    "check_image_health",
    "check_logs",
    "check_port_available",
    "route_after_verify",
    "verify_deployment",
    "verify_stage",
    "verification_graph",
]
