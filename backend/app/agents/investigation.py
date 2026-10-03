"""Evidence-gathering failure investigation.

When verification fails, something has to look at the wreckage before anyone
proposes a fix. This module is that step, and it is read-only by construction:
every tool it can reach is annotated read-only, so if this module ever tried to
restart something the MCP policy would refuse it before the call left the process.

What it inspects, and why each is worth the round trip:

- **Container status** — exit code, OOM flag and restart count separate "never
  started" from "started and died" from "crash-looping", which are three
  different problems with three different fixes.
- **Container logs** — the only place a cause is actually named. Everything else
  is inference; this is testimony.
- **Health endpoint** — distinguishes "not serving" from "serving the wrong
  thing", which look identical from outside.
- **Recent commits** — a deployment that broke after a merge has a far stronger
  suspect than one that has been failing since it was first built.
- **Repository configuration** — a Dockerfile with no ``HEALTHCHECK``, or an
  ``EXPOSE`` that disagrees with the published port, explains a whole class of
  failures without touching the container at all.

The reasoning is in :mod:`app.models.incident`. This module collects, and refuses
to conclude past its evidence: when nothing matches, it reports that rather than
producing a weak cause to fill the field.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from app.models.incident import (
    CONFIDENCE_ORDER,
    FixRecommendation,
    IncidentReport,
    Observation,
    SuspectedRootCause,
    derive_confidence,
    evidence_from_verification,
    match_signatures,
)
from app.models.mcp import ToolCallRequest
from app.models.verification import VerificationResult
from app.services.mcp_manager import get_manager

logger = logging.getLogger(__name__)

DOCKER_SERVER = "docker"
GITHUB_SERVER = "github"
REQUESTED_BY = "deployment-investigation"

#: Exit code 137 is 128+9: SIGKILL. With the OOM flag set it is an out-of-memory
#: kill; without it, something external killed the process and the cause lies
#: elsewhere.
EXIT_OOM_KILLED = 137

#: Recent commits inside this window are treated as suspect for a fresh failure.
RECENT_COMMIT_WINDOW_HOURS = 24


async def _read_only_tool(server: str, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Call a read-only MCP tool. Returns ``{"error": ...}`` instead of raising.

    An investigation that dies because one probe failed is worse than one that
    reports the probe failed. Every failure here becomes evidence about the
    investigation itself, not a crash.
    """
    try:
        result = await get_manager().call_tool(
            ToolCallRequest(
                tool_name=f"{server}.{tool}",
                arguments=arguments,
                requested_by=REQUESTED_BY,
            )
        )
    except Exception as exc:  # noqa: BLE001 - the point is to survive anything
        logger.warning("investigation tool failed", extra={"tool": f"{server}.{tool}"})
        return {"error": type(exc).__name__}

    if not result.success:
        return {"error": result.error_code or "tool_error"}

    content = result.content
    return content if isinstance(content, dict) else {"value": content}


# ---------------------------------------------------------------------------
# Evidence collection
# ---------------------------------------------------------------------------


async def collect_container_evidence(container: str) -> tuple[list[Observation], dict[str, Any]]:
    """Container status, health and logs. Returns observations and a raw status."""
    observations: list[Observation] = []
    status: dict[str, Any] = {}

    payload = await _read_only_tool(DOCKER_SERVER, "container_status", {"name": container})
    if "error" in payload:
        observations.append(
            Observation(
                source="docker.container_status",
                detail=f"unavailable: {payload['error']}",
            )
        )
        return observations, status

    status = payload
    observations.append(
        Observation(
            source="docker.container_status",
            detail=(
                f"state={payload.get('status')} running={payload.get('running')} "
                f"exit_code={payload.get('exit_code')} "
                f"oom_killed={payload.get('oom_killed')} "
                f"restart_count={payload.get('restart_count')}"
            ),
            value=str(payload.get("status")),
        )
    )

    health = await _read_only_tool(DOCKER_SERVER, "container_health", {"name": container})
    if "error" in health:
        observations.append(
            Observation(
                source="docker.container_health",
                detail=f"unavailable: {health['error']}",
            )
        )
    else:
        observations.append(
            Observation(
                source="docker.container_health",
                detail=f"state={health.get('state')} detail={health.get('detail', '')}".strip(),
                value=str(health.get("state")),
            )
        )

    logs = await _read_only_tool(DOCKER_SERVER, "container_logs", {"name": container, "tail": 200})
    text = ""
    if "error" in logs:
        observations.append(
            Observation(source="docker.container_logs", detail=f"unavailable: {logs['error']}")
        )
    else:
        entries = logs.get("logs") or []
        text = "\n".join(str(entry) for entry in entries)
        observations.append(
            Observation(
                source="docker.container_logs",
                detail=f"{len(entries)} line(s) read",
                excerpt=text[-1500:] if text else None,
            )
        )

    status["_log_text"] = text
    return observations, status


async def collect_probe_evidence(container: str, port: int | None) -> list[Observation]:
    """Probe the published port, if we know it."""
    if port is None:
        return []

    payload = await _read_only_tool(
        DOCKER_SERVER, "http_probe", {"port": port, "path": "/", "timeout_seconds": 5.0}
    )
    if "error" in payload:
        return [Observation(source="docker.http_probe", detail=f"probe failed: {payload['error']}")]

    return [
        Observation(
            source="docker.http_probe",
            detail=(
                f"reachable={payload.get('reachable')} status={payload.get('status')} "
                f"latency_ms={payload.get('latency_ms')}"
            ),
            excerpt=(payload.get("body") or "")[:300] or None,
            value=str(payload.get("status")),
        )
    ]


async def collect_commit_evidence(repository: str | None) -> list[Observation]:
    """Recent commits, which are suspect only when the failure is recent.

    A repository whose last commit was six weeks ago cannot have caused a failure
    an hour ago, so the timestamps are checked rather than assumed.
    """
    if not repository or "/" not in repository:
        return []

    owner, _, name = repository.partition("/")
    payload = await _read_only_tool(
        GITHUB_SERVER, "list_commits", {"owner": owner, "repo": name, "limit": 5}
    )
    if "error" in payload:
        return [
            Observation(source="github.list_commits", detail=f"unavailable: {payload['error']}")
        ]

    commits = payload.get("commits") or payload.get("items") or []
    if not commits:
        return []

    now = datetime.now(UTC)
    recent: list[str] = []
    newest: datetime | None = None

    for commit in commits[:5]:
        message = str(commit.get("message", "")).splitlines()[0][:120]
        stamp = _parse_timestamp(commit.get("date") or commit.get("committed_at"))
        if stamp is not None and stamp > (newest or stamp):
            newest = stamp
        if stamp is not None and (now - stamp) <= _window():
            recent.append(f"{stamp.isoformat()} {message}")

    if not recent:
        newest_text = newest.isoformat() if newest else "unknown"
        return [
            Observation(
                source="github.list_commits",
                detail=(
                    f"no commit within {RECENT_COMMIT_WINDOW_HOURS}h; newest {newest_text}. "
                    "A stale repository is unlikely to be the cause of a fresh failure."
                ),
            )
        ]

    return [
        Observation(
            source="github.list_commits",
            detail=f"{len(recent)} commit(s) within {RECENT_COMMIT_WINDOW_HOURS}h",
            excerpt="\n".join(recent)[:600],
        )
    ]


def _window() -> timedelta:
    return timedelta(hours=RECENT_COMMIT_WINDOW_HOURS)


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    text = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def collect_repository_evidence(
    repository_path: str | None, dockerfile: str = "Dockerfile"
) -> list[Observation]:
    """Read the deployment's own configuration. Filesystem reads only.

    Confined to the configured repository root by the caller; this function does
    no resolution of its own and simply refuses to read anything that is not a
    regular file.
    """
    if not repository_path:
        return []

    try:
        from app.core.config import get_settings

        root = get_settings().repository_root_resolved
        candidate = (root / repository_path).resolve()
    except Exception:  # noqa: BLE001
        return []

    # Containment is re-checked here rather than trusted from the caller, because
    # this is the one place a path from a request meets the filesystem.
    if not candidate.is_relative_to(root):
        return [
            Observation(
                source="repository.config",
                detail=f"path escapes the repository root and was not read: {repository_path!r}",
            )
        ]

    observations: list[Observation] = []
    dockerfile_path = candidate / dockerfile

    if not dockerfile_path.is_file():
        observations.append(
            Observation(
                source="repository.config",
                detail=f"no {dockerfile} at {dockerfile_path.name}",
            )
        )
        return observations

    try:
        text = dockerfile_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        observations.append(
            Observation(source="repository.config", detail=f"unreadable: {exc.strerror}")
        )
        return observations

    observations.append(
        Observation(
            source="repository.config",
            detail=f"{dockerfile} present, {len(text.splitlines())} line(s)",
        )
    )

    if not re.search(r"^\s*HEALTHCHECK\b", text, re.IGNORECASE | re.MULTILINE):
        observations.append(
            Observation(
                source="repository.config",
                detail=(
                    "no HEALTHCHECK in the Dockerfile, so Docker cannot report the "
                    "image healthy. Verification falls back to probing."
                ),
            )
        )

    exposed = [int(port) for port in re.findall(r"^\s*EXPOSE\s+(\d+)", text, re.MULTILINE)]
    if exposed:
        observations.append(
            Observation(
                source="repository.config",
                detail=f"EXPOSE {sorted(set(exposed))}",
                value=", ".join(str(port) for port in sorted(set(exposed))),
            )
        )

    return observations


# ---------------------------------------------------------------------------
# Reasoning
# ---------------------------------------------------------------------------


def _cause_from_evidence(
    observations: list[Observation], log_text: str, status: dict[str, Any]
) -> list[SuspectedRootCause]:
    """Turn observations into causes that each cite the observations behind them.

    Corroboration is per cause id: every observation that names a cause in
    ``supports`` counts, and a cause with nothing naming it is dropped rather than
    emitted with an empty evidence list.
    """
    supporting: dict[str, list[Observation]] = {}
    for observation in observations:
        for cause_id in observation.supports:
            supporting.setdefault(cause_id, []).append(observation)

    # Match signatures against logs and status, then tag the observations that
    # actually witnessed each signature.
    matches = match_signatures(log_text)
    for signature, line in matches:
        witness = Observation(
            source=f"signature:{signature.id}",
            detail=signature.cause,
            excerpt=line,
            supports=[signature.id],
        )
        supporting.setdefault(signature.id, []).append(witness)

    if status.get("oom_killed") or status.get("exit_code") == EXIT_OOM_KILLED:
        supporting.setdefault("out_of_memory", []).append(
            Observation(
                source="docker.container_status",
                detail=(
                    f"oom_killed={status.get('oom_killed')} exit_code={status.get('exit_code')}"
                ),
                supports=["out_of_memory"],
            )
        )

    causes: list[SuspectedRootCause] = []
    for signature, _line in matches:
        evidence = supporting.get(signature.id, [])
        if not evidence:
            continue
        causes.append(
            SuspectedRootCause(
                id=signature.id,
                cause=signature.cause,
                component=signature.component,
                evidence=evidence,
                confidence=derive_confidence(evidence, strength=signature.strength),
                confirm=signature.confirm,
                fix_category=signature.fix_category,
            )
        )

    # An exit code with no signature match is still evidence, just not of a cause
    # we recognise. Reported so a human is not left with nothing.
    if not causes and status.get("exit_code"):
        causes.append(
            SuspectedRootCause(
                id="unexplained_exit",
                cause=(
                    f"The container exited with code {status['exit_code']} and the log "
                    "matched no known failure signature."
                ),
                component="application",
                evidence=[
                    Observation(
                        source="docker.container_status",
                        detail=f"exit_code={status['exit_code']}",
                        supports=["unexplained_exit"],
                    )
                ],
                confidence="low",
                confirm="Read the full log; the cause is likely present but unrecognised.",
            )
        )

    return causes


def _best(causes: list[SuspectedRootCause]) -> SuspectedRootCause | None:
    """The best-supported cause, or None when there are none."""
    if not causes:
        return None
    return max(causes, key=lambda cause: CONFIDENCE_ORDER[cause.confidence])


def _recommend_fix(
    causes: list[SuspectedRootCause], container: str, image: str | None
) -> FixRecommendation | None:
    """Propose the least-invasive fix the evidence supports.

    Ordering is by how little the fix assumes: a restart cannot destroy data, a
    rebuild can, and a code change would need a human regardless. The least
    invasive option that addresses the cause wins.
    """
    primary = _best(causes)
    if primary is None:
        return None
    category = primary.fix_category

    if category == "configuration" and primary.id == "dependency_refused":
        return FixRecommendation(
            action="check_dependency",
            description="Confirm the backing service is running and accepting connections.",
            rationale=(
                "The log shows a refused connection. Restarting the application would "
                "restart it into the same failure."
            ),
            tool="docker.container_status",
            arguments={"name": container},
            requires_approval=True,
            reversible=True,
            manual_only=True,
        )

    if category == "configuration":
        return FixRecommendation(
            action="review_configuration",
            description="Review the deployment's configuration against what the app requires.",
            rationale=primary.cause,
            tool=None,
            arguments={},
            requires_approval=True,
            reversible=True,
            manual_only=True,
        )

    if category == "code":
        return FixRecommendation(
            action="review_code_change",
            description="Review the code change behind the crash.",
            rationale=primary.cause,
            tool=None,
            arguments={},
            requires_approval=True,
            reversible=False,
            manual_only=True,
        )

    if category == "build":
        return FixRecommendation(
            action="rebuild_image",
            description=f"Rebuild {image or 'the image'} and redeploy.",
            rationale=primary.cause,
            tool="docker.build_image",
            arguments={},
            requires_approval=True,
            reversible=False,
        )

    if category == "dependency":
        return FixRecommendation(
            action="retry_deployment",
            description="Retry the deployment once the dependency is available.",
            rationale=(
                "The dependency was unreachable, which is often a start-up ordering "
                "problem that a retry resolves."
            ),
            tool="docker.build_image",
            arguments={},
            requires_approval=True,
            reversible=True,
        )

    # application, or nothing recognised: a restart is the cheapest thing that
    # could plausibly help, and it destroys nothing.
    return FixRecommendation(
        action="restart_container",
        description=f"Restart {container} and re-verify.",
        rationale=(
            f"{primary.cause} A restart replaces a process that is already not "
            "working and preserves its logs, unlike a rebuild."
        ),
        tool="docker.stop_container",
        arguments={"name": container},
        requires_approval=True,
        reversible=True,
    )


async def investigate_failure(
    result: VerificationResult | None = None,
    *,
    container: str | None = None,
    image: str | None = None,
    repository_path: str | None = None,
    repository: str | None = None,
    stop_reason: str | None = None,
    stop_task_id: str | None = None,
) -> IncidentReport:
    """Investigate a failure and return an evidence-backed report.

    Read-only. Investigating must never change the thing being investigated: an
    agent that restarts the container while diagnosing destroys the exit code and
    log that were the only evidence.
    """
    observations: list[Observation] = []
    status: dict[str, Any] = {}
    log_text = ""

    name = container or (result.container if result else None)
    port = _expected_port(result)

    if name:
        container_observations, status = await collect_container_evidence(name)
        observations.extend(container_observations)
        log_text = str(status.pop("_log_text", "") or "")
        observations.extend(await collect_probe_evidence(name, port))
    elif stop_reason:
        observations.append(
            Observation(
                source="execution.audit",
                detail=f"task {stop_task_id or '?'} stopped: {stop_reason}",
            )
        )

    observations.extend(collect_repository_evidence(repository_path))
    observations.extend(await collect_commit_evidence(repository))

    if result is not None:
        observations.extend(evidence_from_verification(result.evidence))

    causes = _cause_from_evidence(observations, log_text, status)

    # Tag every observation that a recognised cause is relevant to, so the
    # corroboration count reflects genuinely independent witnesses.
    if causes:
        cause_ids = {cause.id for cause in causes}
        for observation in observations:
            if observation.source.startswith("signature:"):
                continue
            if observation.supports:
                continue
            if any(cause_id in log_text for cause_id in cause_ids):
                observation.supports = sorted(
                    cause_id for cause_id in cause_ids if cause_id in log_text
                )

    primary = _best(causes)
    confidence = primary.confidence if primary else "low"
    recommendation = _recommend_fix(causes, name or "the container", image)

    # Automatic action needs both a cause and a remedy, and enough confidence in
    # both. Anything less escalates.
    automatic_safe = bool(
        primary
        and recommendation
        and confidence in {"medium", "high"}
        and not recommendation.manual_only
        and recommendation.action in {"restart_container", "rebuild_image", "retry_deployment"}
    )

    symptom = _symptom(result, stop_reason, status)
    component = primary.component if primary else "unknown"

    report = IncidentReport(
        incident_id=f"{name or 'deployment'}-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}",
        symptom=symptom,
        evidence=observations,
        suspected_root_causes=causes,
        confidence=confidence,
        affected_component=component,
        recommended_fix=recommendation,
        automatic_fix_safe=automatic_safe,
        next_action=(
            f"Confirm the {primary.id} hypothesis: {primary.confirm}"
            if primary and primary.confirm
            else "Investigate manually; the evidence did not establish a cause."
        ),
        repository=repository,
        investigated_at=datetime.now(UTC).isoformat(),
        inconclusive=not causes,
    )

    logger.info(
        "failure investigated",
        extra={
            "incident_id": report.incident_id,
            "confidence": report.confidence,
            "causes": len(report.suspected_root_causes),
            "observations": len(report.evidence),
            "automatic_fix_safe": report.automatic_fix_safe,
        },
    )
    return report


def _expected_port(result: VerificationResult | None) -> int | None:
    if result is None:
        return None
    # `check()` returns None for a check that never ran -- a stopped execution has
    # no port check at all -- so this cannot assume the check is present.
    check = result.check("port_available")
    if check is None:
        return None
    for evidence in check.evidence:
        if evidence.value and evidence.value.isdigit():
            return int(evidence.value)
        match = re.search(r"\b(\d{2,5})\b", evidence.detail or "")
        if match:
            return int(match.group(1))
    return None


def _symptom(
    result: VerificationResult | None, stop_reason: str | None, status: dict[str, Any]
) -> str:
    if stop_reason and result is None:
        return f"Deployment execution stopped: {stop_reason}"
    if result is None:
        return "Deployment failed with no verification result available."

    parts = [f"Verification returned {result.status}"]
    if result.failures:
        parts.append(f"failed: {', '.join(result.failures)}")
    if result.warnings:
        parts.append(f"warned: {', '.join(result.warnings)}")
    if status.get("exit_code") not in (None, 0):
        parts.append(f"exit code {status['exit_code']}")
    if status.get("oom_killed"):
        parts.append("killed for memory")
    if status.get("restart_count"):
        parts.append(f"restarted {status['restart_count']} time(s)")
    return "; ".join(parts)


__all__ = [
    "RECENT_COMMIT_WINDOW_HOURS",
    "collect_commit_evidence",
    "collect_container_evidence",
    "collect_probe_evidence",
    "collect_repository_evidence",
    "investigate_failure",
]
