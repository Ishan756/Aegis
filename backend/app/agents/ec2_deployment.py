"""Deploy to a configured EC2 instance over SSH and Docker-over-SSH.

    connect → prepare → [scoped Docker server] PLAN → EXECUTE → VERIFY → END

The division of labour follows what each transport is good at:

- **SSH** does the two things no MCP tool can do from here: check that the
  instance is reachable and make Docker ready on it (install or start, only with
  approval), and — during verification — probe the health endpoint from inside
  the instance so "the app is broken" can be told apart from "the firewall hides
  it". Every remote command is an argument list quoted once with ``shlex.join``;
  no request field is ever concatenated into a shell string.
- **The Docker MCP**, opened as a *scoped* server whose environment sets
  ``DOCKER_HOST=ssh://user@host``, does all image and container work. The shared
  local server is never repointed: a deployment to EC2 must not change where the
  next local deployment goes. The scoped server's tools are registered with the
  execution policy under their own name for the duration of the run, so build
  and start are still refused without approval — the same gate as locally.
- **SSH mutators bypass the MCP policy** because they are not MCP calls: the
  approval gate for them is the request's ``approve`` flag, checked here before
  any command is launched. A refusal is reported as a refused step rather than
  silently skipped.

History is written through the same workflow the local deployment uses, with the
target recorded so two runs of the same commit on different machines stay
distinguishable.
"""

from __future__ import annotations

import shlex
import sys
import time
from collections.abc import Awaitable, Callable
from typing import Any

from app.agents.deployment_workflow import expected_port, run_deployment_workflow
from app.core.config import EC2Settings, Settings, get_settings
from app.core.exceptions import ConfigurationError, ValidationError
from app.core.logging import get_logger
from app.memory import get_memory_service
from app.models.deployment_record import DeploymentRecord, DeploymentTarget
from app.models.docker import DeploymentStep, StepOutcome
from app.models.ec2_deployment import EC2DeploymentRequest, EC2DeploymentResult
from app.models.mcp import ToolCallRequest
from app.models.verification import Evidence, VerificationResult
from app.services.mcp_manager import get_manager
from app.services.ssh import SSHResult, SSHSession

logger = get_logger(__name__)

#: Name the scoped Docker server registers under. Distinct from the persistent
#: local ``docker`` server so both can be connected at once and a call can never
#: reach the wrong daemon by accident.
SCOPED_SERVER = "docker-ec2"

REQUESTED_BY = "ec2-deployment"

#: Timeouts for the individual remote commands. An install is the only slow one;
#: everything else should answer in seconds or be treated as broken.
VERSION_TIMEOUT = 30.0
DAEMON_TIMEOUT = 60.0
INSTALL_TIMEOUT = 600.0


# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------


def _target(settings: Settings) -> DeploymentTarget:
    """The deployment target as history should record it. No secret material."""
    ec2 = settings.ec2
    return DeploymentTarget(
        kind="ec2",
        host=ec2.host,
        instance_id=ec2.instance_id,
        region=ec2.region or settings.aws.region,
        ssh_user=ec2.ssh_user,
        key_file=str(ec2.ssh_key_file) if ec2.ssh_key_file else None,
    )


def _docker_server_command(settings: Settings) -> str:
    """Launch command for the Docker MCP server used against the target.

    The configured command wins so an operator can pin a server build; the
    fallback points at this checkout's server with the current interpreter,
    because a relative ``python ../mcp_servers/...`` only resolves when the
    backend happens to be started from ``backend/``.
    """
    configured = settings.mcp.servers.get("docker")
    if configured:
        return configured
    script = settings.repository_root_resolved / "mcp_servers" / "docker" / "server.py"
    if not script.is_file():
        raise ConfigurationError(
            "No Docker MCP server command is configured and none was found at "
            f"{script}. Add 'docker=...' to AEGIS_MCP__SERVERS.",
            details={"script": str(script)},
        )
    return f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}"


def _scoped_environment(settings: Settings, ssh: SSHSession) -> dict[str, str]:
    """Environment for the scoped Docker server.

    Three concerns in one dict: ``HOME`` points the server's own ``ssh``
    subprocess at the per-run SSH configuration (identity, host keys, batch
    mode), ``DOCKER_HOST`` aims the Docker CLI at the instance, and
    ``AEGIS_DOCKER__PROBE_HOSTS`` allowlists exactly one probe target — the
    instance — so the server's loopback-only default is widened by one deliberate
    host and no more. ``CONTEXT_ROOT`` keeps build contexts inside the
    repository; without it the server falls back to its working directory and
    every build path would be refused.
    """
    environment = dict(get_manager().forwarded_environment)
    environment.update(
        {
            "HOME": ssh.home,
            "DOCKER_HOST": f"ssh://{ssh.user}@{ssh.host}",
            "AEGIS_DOCKER__PROBE_HOSTS": ssh.host,
            "AEGIS_DOCKER__CONTEXT_ROOT": str(settings.docker.context_root_resolved),
        }
    )
    return environment


def _step(
    stage: str,
    outcome: StepOutcome,
    *,
    detail: str,
    approved: bool | None = None,
    started: float,
) -> DeploymentStep:
    """One SSH-side step. ``tool`` stays None: no MCP tool was involved."""
    return DeploymentStep(
        stage=stage,  # type: ignore[arg-type]
        outcome=outcome,
        tool=None,
        approved=approved,
        detail=detail,
        duration_seconds=round(time.monotonic() - started, 3),
    )


def _excerpt(result: SSHResult, limit: int = 400) -> str:
    """The part of a failed command that explains it, capped for the record."""
    text = (result.stderr or result.stdout or "").strip()
    if result.timed_out:
        return f"timed out after {result.duration_seconds}s"
    return text[-limit:] or f"exit code {result.exit_code}"


# ---------------------------------------------------------------------------
# Target readiness
# ---------------------------------------------------------------------------


async def _connect_step(ssh: SSHSession) -> DeploymentStep:
    """Prove the instance answers over SSH before anything else is attempted."""
    started = time.monotonic()
    result = await ssh.run("uname", "-sr")
    if result.success:
        return _step(
            "target",
            "ok",
            detail=f"reached {ssh.user}@{ssh.host}: {result.stdout.strip() or 'ok'}",
            started=started,
        )
    return _step("target", "failed", detail=_excerpt(result), started=started)


async def _install_docker(ssh: SSHSession) -> SSHResult:
    """Install Docker using whichever package manager the target actually has.

    Tried in order rather than detected with a version probe: the probe would be
    another round trip and every branch would still have to handle its failure.
    A non-zero exit moves on, so the final result carries the last error — the
    one from the manager the target really uses.
    """
    attempts: list[tuple[str, ...]] = [
        ("sudo", "dnf", "install", "-y", "docker"),
        ("sudo", "yum", "install", "-y", "docker"),
    ]
    last = await ssh.run(*attempts[0], timeout=INSTALL_TIMEOUT)
    if last.success:
        return last
    last = await ssh.run(*attempts[1], timeout=INSTALL_TIMEOUT)
    if last.success:
        return last
    # Debian/Ubuntu: refresh the index first, then install the distro's package.
    await ssh.run("sudo", "apt-get", "update", "-y", timeout=INSTALL_TIMEOUT)
    return await ssh.run("sudo", "apt-get", "install", "-y", "docker.io", timeout=INSTALL_TIMEOUT)


async def _ensure_docker(
    ssh: SSHSession, *, ec2: EC2Settings, approve: bool, dry_run: bool
) -> tuple[bool, DeploymentStep]:
    """Bring Docker to a usable state on the target, or say why it is not.

    Returns ``(ready, step)``. Every mutating branch is gated on ``approve``
    first — these commands never reach the MCP policy, so this check *is* the
    approval gate — and a dry run may only observe, never change anything.
    """
    started = time.monotonic()

    probe = await ssh.run("docker", "version", timeout=VERSION_TIMEOUT)
    if probe.success:
        versions = [
            line.strip()
            for line in probe.stdout.splitlines()
            if line.strip().startswith(("Version:", "Client:", "Server:"))
        ]
        return True, _step(
            "prepare",
            "ok",
            detail="Docker is ready. " + " ".join(versions[:2]),
            started=started,
        )

    stderr = (probe.stderr or probe.stdout or "").lower()
    missing = probe.exit_code == 127 or "command not found" in stderr
    denied = "permission denied" in stderr
    daemon_down = "cannot connect" in stderr or "daemon" in stderr

    if missing:
        reason = "Docker is not installed on the target"
        if dry_run:
            return False, _step(
                "prepare",
                "skipped",
                detail=f"dry run: {reason}. A real run would install it (requires approve=true).",
                started=started,
            )
        if not ec2.install_docker:
            return False, _step(
                "prepare",
                "failed",
                detail=f"{reason} and AEGIS_EC2__INSTALL_DOCKER is false.",
                started=started,
            )
        if not approve:
            return False, _step(
                "prepare",
                "refused",
                approved=False,
                detail=f"{reason}. Installing it requires approval (approve=true).",
                started=started,
            )

        install = await _install_docker(ssh)
        if not install.success:
            return False, _step(
                "prepare",
                "failed",
                approved=True,
                detail=f"could not install Docker: {_excerpt(install)}",
                started=started,
            )
        enable = await ssh.run(
            "sudo", "systemctl", "enable", "--now", "docker", timeout=DAEMON_TIMEOUT
        )
        if not enable.success:
            return False, _step(
                "prepare",
                "failed",
                approved=True,
                detail=f"Docker installed but the daemon could not be started: {_excerpt(enable)}",
                started=started,
            )

    elif denied:
        if dry_run:
            return False, _step(
                "prepare",
                "skipped",
                detail=(
                    "dry run: Docker is present but the SSH user cannot use it. "
                    "A real run would add the user to the docker group (requires approve=true)."
                ),
                started=started,
            )
        if not approve:
            return False, _step(
                "prepare",
                "refused",
                approved=False,
                detail=(
                    "The SSH user cannot access the Docker daemon. Granting docker-group "
                    "access requires approval (approve=true)."
                ),
                started=started,
            )
        group = await ssh.run("sudo", "usermod", "-aG", "docker", ssh.user, timeout=DAEMON_TIMEOUT)
        if not group.success:
            return False, _step(
                "prepare",
                "failed",
                approved=True,
                detail=f"could not grant Docker access: {_excerpt(group)}",
                started=started,
            )

    elif daemon_down:
        if dry_run:
            return False, _step(
                "prepare",
                "skipped",
                detail=(
                    "dry run: the Docker daemon is not running. A real run would "
                    "start it (requires approve=true)."
                ),
                started=started,
            )
        if not approve:
            return False, _step(
                "prepare",
                "refused",
                approved=False,
                detail=(
                    "The Docker daemon is stopped; starting it requires "
                    "approval (approve=true)."
                ),
                started=started,
            )
        enable = await ssh.run(
            "sudo", "systemctl", "enable", "--now", "docker", timeout=DAEMON_TIMEOUT
        )
        if not enable.success:
            return False, _step(
                "prepare",
                "failed",
                approved=True,
                detail=f"could not start the Docker daemon: {_excerpt(enable)}",
                started=started,
            )

    else:
        return False, _step(
            "prepare",
            "failed",
            detail=f"`docker version` failed: {_excerpt(probe)}",
            started=started,
        )

    # A group change only applies to new sessions, and every Docker-over-SSH
    # connection is one — so this re-probe is a genuine check, not a formality.
    recheck = await ssh.run("docker", "version", timeout=VERSION_TIMEOUT)
    if recheck.success:
        return True, _step(
            "prepare",
            "ok",
            approved=True if approve else None,
            detail="Docker is ready after preparation on the target.",
            started=started,
        )
    return False, _step(
        "prepare",
        "failed",
        approved=True if approve else None,
        detail=f"Docker is still not usable: {_excerpt(recheck)}",
        started=started,
    )


# ---------------------------------------------------------------------------
# Verification evidence
# ---------------------------------------------------------------------------


def _build_evidence_hook(
    ssh: SSHSession,
    request: EC2DeploymentRequest,
    target: DeploymentTarget,
    observed: dict[str, Any],
) -> Callable[[], Awaitable[list[Evidence]]]:
    """Read-only diagnostics appended to VERIFY's evidence before history is written.

    ``observed`` is filled in as a side effect so the caller can turn the
    loopback result into an actionable note after the workflow returns. Both
    observations are best-effort: a missing ``curl`` or an unconfigured AWS
    profile degrades to an evidence line saying so, never to a failed
    verification that was otherwise clean.
    """

    async def hook() -> list[Evidence]:
        evidence: list[Evidence] = []

        port = expected_port(request) or 0
        url = f"http://127.0.0.1:{port}{request.health_path}"
        status = await ssh.http_status(url, timeout=10.0)
        observed["loopback"] = status
        if status is None:
            evidence.append(
                Evidence(
                    source="ssh.http_status",
                    detail=(
                        f"could not probe {url} from inside the instance "
                        "(curl missing or no answer)"
                    ),
                )
            )
        else:
            evidence.append(
                Evidence(
                    source="ssh.http_status",
                    value=str(status),
                    detail=f"GET {url} answered from inside the instance (loopback)",
                )
            )

        if target.instance_id:
            evidence.append(await _cpu_metric_evidence(target))
        return evidence

    return hook


async def _cpu_metric_evidence(target: DeploymentTarget) -> Evidence:
    """Best-effort CloudWatch CPU datapoint for the instance."""
    arguments: dict[str, Any] = {
        "namespace": "AWS/EC2",
        "metric_name": "CPUUtilization",
        "dimensions": {"InstanceId": target.instance_id},
        "period": 300,
        "statistics": ["Average"],
    }
    if target.region:
        arguments["region"] = target.region

    try:
        result = await get_manager().call_tool(
            ToolCallRequest(
                tool_name="aws.cloudwatch_get_metrics",
                arguments=arguments,
                requested_by=REQUESTED_BY,
                reason="collecting target metrics as verification evidence",
            )
        )
    except Exception as exc:  # noqa: BLE001 - diagnostics never fail the verdict
        return Evidence(source="aws.cloudwatch_get_metrics", detail=f"unavailable: {exc}")

    if not result.success:
        return Evidence(
            source="aws.cloudwatch_get_metrics",
            detail=f"unavailable: {result.error_code or result.error_message or 'call failed'}",
        )

    content = result.content if isinstance(result.content, dict) else {}
    datapoints = content.get("datapoints") or []
    if not datapoints:
        return Evidence(
            source="aws.cloudwatch_get_metrics",
            detail=(
                "no CPU datapoints in the window "
                "(instance too new or metrics not yet published)"
            ),
        )

    latest = max(datapoints, key=lambda point: str(point.get("Timestamp") or ""))
    average = latest.get("Average")
    return Evidence(
        source="aws.cloudwatch_get_metrics",
        value=f"{average:.2f}%" if isinstance(average, (int, float)) else str(average),
        detail=f"CPUUtilization (Average) on {target.instance_id}",
    )


def _probe_note(
    verification: VerificationResult | None, observed: dict[str, Any], port: int | None
) -> str | None:
    """Turn "answered inside, failed outside" into one actionable sentence.

    Without this the two observations sit in the evidence list and the reader has
    to correlate them; with it the response says which side is wrong.
    """
    if verification is None or observed.get("loopback") != 200 or not port:
        return None
    health = verification.check("health_endpoint")
    if health is None or health.outcome != "fail":
        return None
    return (
        f"The application answered on the instance itself (HTTP 200) but not through "
        f"port {port} from this host: allow that port in the instance's security group "
        f"for the deploying address."
    )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _preflight(settings: Settings) -> None:
    """Reject impossible deployments before any history row exists.

    A misconfiguration raises before the run is recorded: a missing key file or
    an unset target is a problem on this side (reported as 422 or a
    configuration error), and a history of "failed" runs would blame the
    target for a typo here.
    """
    ec2 = settings.ec2
    if not ec2.is_configured:
        raise ConfigurationError(
            "EC2 deployment is not configured. Set AEGIS_EC2__ENABLED=true, "
            "AEGIS_EC2__INSTANCE_ID, AEGIS_EC2__HOST and AEGIS_EC2__SSH_KEY_FILE.",
            details={
                "enabled": ec2.enabled,
                "instance_id_set": bool(ec2.instance_id),
                "host_set": bool(ec2.host),
                "key_file_set": bool(ec2.ssh_key_file),
            },
        )
    if not settings.mcp.enabled:
        raise ConfigurationError(
            "EC2 deployment needs MCP (AEGIS_MCP__ENABLED=true): the Docker "
            "tools run against the instance through a scoped MCP server."
        )
    key_file = ec2.ssh_key_file.expanduser()  # type: ignore[union-attr]
    if not key_file.is_file():
        raise ValidationError(
            "The configured SSH key file does not exist. Point "
            "AEGIS_EC2__SSH_KEY_FILE at the private key for the target instance.",
            details={"key_file": str(key_file)},
        )


async def _record_stopped(
    record: DeploymentRecord,
    request: EC2DeploymentRequest,
    target: DeploymentTarget,
    *,
    started_at: Any,
    stop_reason: str,
) -> None:
    """Record an attempt that never reached PLAN as a failed deployment."""
    await get_memory_service().record_deployment(
        deployment_id=record.deployment_id,
        request=request,
        target=target,
        stopped=True,
        stop_reason=stop_reason,
        started_at=started_at,
    )


def _failed_result(
    record: DeploymentRecord,
    target: DeploymentTarget,
    steps: list[DeploymentStep],
    notes: list[str],
    *,
    stop_reason: str,
) -> EC2DeploymentResult:
    return EC2DeploymentResult(
        deployment_id=record.deployment_id,
        target=target,
        succeeded=False,
        steps=steps,
        stopped=True,
        stop_reason=stop_reason,
        notes=notes,
    )


async def run_ec2_deployment(request: EC2DeploymentRequest) -> EC2DeploymentResult:
    """Deploy ``request`` to the configured EC2 instance and verify the result.

    Approval is the caller's ``approve`` and nothing else: the MCP policy
    evaluates build and start through the scoped server exactly as it would
    locally, and the SSH preparation commands check the same flag before they
    are launched. A run without approval reports refused steps rather than
    quietly doing nothing.
    """
    settings = get_settings()
    _preflight(settings)
    ec2 = settings.ec2
    target = _target(settings)

    memory = get_memory_service()
    # Before the first connection, so a run that hangs on SSH still appears.
    record = await memory.begin_deployment(request=request, target=target)
    started_at = record.started_at

    steps: list[DeploymentStep] = []
    notes: list[str] = [
        "The image is built and run on the instance through Docker over SSH; "
        "nothing was pushed to a registry.",
    ]
    observed: dict[str, Any] = {}

    async with SSHSession(
        host=ec2.host,  # type: ignore[arg-type]
        user=ec2.ssh_user,
        key_file=ec2.ssh_key_file,  # type: ignore[arg-type]
        timeout_seconds=ec2.ssh_timeout_seconds,
    ) as ssh:
        connect = await _connect_step(ssh)
        steps.append(connect)
        if connect.outcome != "ok":
            stop_reason = f"the target was unreachable: {connect.detail}"
            await _record_stopped(
                record, request, target, started_at=started_at, stop_reason=stop_reason
            )
            return _failed_result(record, target, steps, notes, stop_reason=stop_reason)

        ready, prepare = await _ensure_docker(
            ssh, ec2=ec2, approve=request.approve, dry_run=request.dry_run
        )
        steps.append(prepare)
        if not ready and not request.dry_run:
            stop_reason = f"the target was not ready: {prepare.detail}"
            await _record_stopped(
                record, request, target, started_at=started_at, stop_reason=stop_reason
            )
            return _failed_result(record, target, steps, notes, stop_reason=stop_reason)

        async def workflow() -> dict[str, Any]:
            return await run_deployment_workflow(
                request,
                docker_server=SCOPED_SERVER,
                probe_host=ec2.host,
                target=target,
                evidence_hook=_build_evidence_hook(ssh, request, target, observed),
                self_healing_policy=settings.self_healing.to_policy(),
                self_healing_human_approved=False,
                # The row opened before the first SSH connection continues here;
                # a second begin would make history count one run as two.
                record=record,
            )

        if request.dry_run:
            # A dry run calls no tool at all, so there is nothing for the scoped
            # server to serve: opening a subprocess for it would only add a
            # failure mode to a run that provably touches nothing.
            outcome = await workflow()
        else:
            async with get_manager().scoped_server(
                SCOPED_SERVER,
                command=_docker_server_command(settings),
                env=_scoped_environment(settings, ssh),
            ):
                outcome = await workflow()

    verification: VerificationResult | None = outcome.get("verification")
    succeeded = bool(verification and verification.status == "SUCCESS") and not outcome.get(
        "stopped"
    )

    port = expected_port(request)
    note = _probe_note(verification, observed, port)
    if note:
        notes.append(note)
    if prepare.outcome == "ok" and prepare.approved:
        notes.append("Docker was prepared on the target as part of this run (approved).")

    logger.info(
        "ec2 deployment finished",
        extra={
            "deployment_id": outcome.get("deployment_id"),
            "host": ec2.host,
            "instance_id": ec2.instance_id,
            "succeeded": succeeded,
            "verification": verification.status if verification else None,
        },
    )

    return EC2DeploymentResult(
        deployment_id=str(outcome.get("deployment_id") or record.deployment_id),
        target=target,
        succeeded=succeeded,
        steps=steps,
        plan=outcome.get("plan") or [],
        execution=outcome.get("execution"),
        verification=verification,
        debug=outcome.get("debug"),
        recovery=outcome.get("recovery"),
        recovered=bool(outcome.get("recovered")),
        incident=outcome.get("incident"),
        plan_explanation=outcome.get("plan_explanation"),
        stopped=bool(outcome.get("stopped")),
        stop_reason=outcome.get("stop_reason"),
        notes=notes,
    )


__all__ = ["REQUESTED_BY", "SCOPED_SERVER", "run_ec2_deployment"]
