"""The EC2 deployment flow: configuration, target preparation, workflow wiring.

The SSH transport and the MCP scope are faked at their seams; everything
between them is the real code, including the real pre-flight and the real
workflow graph on the dry-run path. What these tests guard is the division of
labour: SSH decides whether the machine is ready, the scoped server runs the
Docker tools against the instance, approval gates both sides, and history gets
exactly one row per run whether the run stops at the door or finishes.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from app.agents import ec2_deployment
from app.agents.ec2_deployment import run_ec2_deployment
from app.core.config import EC2Settings, Settings, get_settings
from app.core.exceptions import ConfigurationError
from app.memory import DeploymentMemoryService, set_memory_service
from app.memory.in_memory import InMemoryMemoryStore
from app.models.deployment_record import DeploymentQuery, DeploymentStatus
from app.models.ec2_deployment import EC2DeploymentRequest

pytestmark = pytest.mark.anyio


@pytest.fixture
def ec2_env(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """A configured target with a key file that exists.

    Region is deliberately not set here: the fallback to ``AEGIS_AWS__REGION``
    from the repository ``.env`` is part of what is being asserted.
    """
    key_file = tmp_path / "aegis-dev-key.pem"
    key_file.write_text("dummy", encoding="utf-8")
    monkeypatch.setenv("AEGIS_EC2__ENABLED", "true")
    monkeypatch.setenv("AEGIS_EC2__INSTANCE_ID", "i-0abc123")
    monkeypatch.setenv("AEGIS_EC2__HOST", "203.0.113.10")
    monkeypatch.setenv("AEGIS_EC2__SSH_USER", "ec2-user")
    monkeypatch.setenv("AEGIS_EC2__SSH_KEY_FILE", str(key_file))
    monkeypatch.setenv("AEGIS_MCP__ENABLED", "true")
    get_settings.cache_clear()
    yield key_file
    get_settings.cache_clear()


@pytest.fixture
def memory() -> Iterator[DeploymentMemoryService]:
    """An in-memory history wired the way the lifespan wires one."""
    service = DeploymentMemoryService(InMemoryMemoryStore())
    set_memory_service(service)
    yield service
    set_memory_service(None)


class ScriptedSSH:
    """Answers each remote command with the next scripted result.

    Records every call so a test can assert *which* commands were launched —
    refusal means "no command was sent", not "the command failed".
    """

    def __init__(self, *results: Any, user: str = "ec2-user") -> None:
        self._results = list(results)
        self.calls: list[tuple[str, ...]] = []
        self.user = user
        self.host = "203.0.113.10"

    async def run(self, *argv: str, timeout: float | None = None) -> Any:
        self.calls.append(tuple(argv))
        if not self._results:
            raise AssertionError(f"unexpected remote command: {argv}")
        item = self._results.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def ok(*argv: str, stdout: str = "", stderr: str = "") -> Any:
    from app.services.ssh import SSHResult

    return SSHResult(argv=tuple(argv), exit_code=0, stdout=stdout, stderr=stderr)


def fail(*argv: str, code: int = 1, stderr: str = "", stdout: str = "") -> Any:
    from app.services.ssh import SSHResult

    return SSHResult(argv=tuple(argv), exit_code=code, stdout=stdout, stderr=stderr)


def _result(code: int = 0, stderr: str = "", stdout: str = "") -> Any:
    return fail("docker", "version", code=code, stderr=stderr, stdout=stdout)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


async def test_target_records_provenance_without_secrets(ec2_env: Any) -> None:
    """The history target says where, never what the key contains."""
    target = ec2_deployment._target(get_settings())

    assert target.kind == "ec2"
    assert target.host == "203.0.113.10"
    assert target.instance_id == "i-0abc123"
    assert target.ssh_user == "ec2-user"
    # Falls back to the AWS region configuration, not blank.
    assert target.region == "ap-south-1"
    assert target.key_file and target.key_file.endswith("aegis-dev-key.pem")

    payload = target.model_dump()
    assert set(payload) == {"kind", "host", "instance_id", "region", "ssh_user", "key_file"}


async def test_docker_server_command_prefers_configuration(ec2_env: Any) -> None:
    """The configured launch command wins over the fallback."""
    settings = get_settings()
    assert settings.mcp.servers.get("docker")

    assert ec2_deployment._docker_server_command(settings) == settings.mcp.servers["docker"]


async def test_docker_server_command_falls_back_to_this_checkout(
    ec2_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without configuration the command still names a script that exists."""
    monkeypatch.setenv("AEGIS_MCP__SERVERS", "")
    get_settings.cache_clear()

    command = ec2_deployment._docker_server_command(get_settings())

    assert "mcp_servers/docker/server.py" in command
    assert get_settings().repository_root_resolved.joinpath(
        "mcp_servers", "docker", "server.py"
    ).is_file()


async def test_scoped_environment_names_the_instance(
    ec2_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The scope aims Docker at the instance and widens the probe allowlist by one host."""
    from types import SimpleNamespace

    monkeypatch.setattr(
        ec2_deployment,
        "get_manager",
        lambda: SimpleNamespace(forwarded_environment={"AEGIS_AWS__REGION": "ap-south-1"}),
    )
    settings = get_settings()
    ssh = SimpleNamespace(home="/run/ssh-abc", user="ec2-user", host="203.0.113.10")

    environment = ec2_deployment._scoped_environment(settings, ssh)

    assert environment["DOCKER_HOST"] == "ssh://ec2-user@203.0.113.10"
    assert environment["HOME"] == "/run/ssh-abc"
    assert environment["AEGIS_DOCKER__PROBE_HOSTS"] == "203.0.113.10"
    # The build boundary must be the repository, not whatever directory the
    # backend was started from; without it every build context is refused.
    assert environment["AEGIS_DOCKER__CONTEXT_ROOT"] == str(
        settings.repository_root_resolved
    )
    # Forwarded settings still arrive for the server's own tooling.
    assert environment["AEGIS_AWS__REGION"] == "ap-south-1"


# ---------------------------------------------------------------------------
# Target preparation
# ---------------------------------------------------------------------------


async def test_ready_docker_needs_no_preparation() -> None:
    ssh = ScriptedSSH(ok("docker", "version", stdout="Client: Version: 27.0.0"))

    ready, step = await ec2_deployment._ensure_docker(
        ssh, ec2=EC2Settings(), approve=False, dry_run=False
    )

    assert ready is True
    assert step.outcome == "ok"
    assert ssh.calls == [("docker", "version")]


async def test_missing_docker_without_approval_is_refused() -> None:
    """Refused means nothing was launched, not that a command failed."""
    ssh = ScriptedSSH(_result(code=127, stderr="bash: docker: command not found"))

    ready, step = await ec2_deployment._ensure_docker(
        ssh, ec2=EC2Settings(), approve=False, dry_run=False
    )

    assert ready is False
    assert step.outcome == "refused"
    assert step.approved is False
    assert "approve=true" in (step.detail or "")
    assert ssh.calls == [("docker", "version")]


async def test_missing_docker_in_a_dry_run_is_observed_not_installed() -> None:
    ssh = ScriptedSSH(_result(code=127, stderr="bash: docker: command not found"))

    ready, step = await ec2_deployment._ensure_docker(
        ssh, ec2=EC2Settings(), approve=True, dry_run=True
    )

    assert ready is False
    assert step.outcome == "skipped"
    assert ssh.calls == [("docker", "version")]


async def test_missing_docker_with_approval_is_installed() -> None:
    ssh = ScriptedSSH(
        _result(code=127, stderr="bash: docker: command not found"),
        fail("sudo", "dnf", "install", "-y", "docker", code=127),
        fail("sudo", "yum", "install", "-y", "docker", code=1),
        ok("sudo", "apt-get", "update", "-y"),
        ok("sudo", "apt-get", "install", "-y", "docker.io"),
        ok("sudo", "systemctl", "enable", "--now", "docker"),
        ok("docker", "version", stdout="Client: Version: 27.0.0"),
    )

    ready, step = await ec2_deployment._ensure_docker(
        ssh, ec2=EC2Settings(install_docker=True), approve=True, dry_run=False
    )

    assert ready is True
    assert step.outcome == "ok"
    assert step.approved is True
    # The readiness re-probe runs last, so "installed" is verified, not assumed.
    assert ssh.calls[-1] == ("docker", "version")
    assert len(ssh.calls) == 7


async def test_permission_denied_with_approval_grants_group_access() -> None:
    ssh = ScriptedSSH(
        _result(code=1, stderr="permission denied while trying to connect to the Docker daemon"),
        ok("sudo", "usermod", "-aG", "docker", "ec2-user"),
        ok("docker", "version"),
    )

    ready, step = await ec2_deployment._ensure_docker(
        ssh, ec2=EC2Settings(), approve=True, dry_run=False
    )

    assert ready is True
    assert ("sudo", "usermod", "-aG", "docker", "ec2-user") in ssh.calls


async def test_stopped_daemon_without_approval_is_refused() -> None:
    ssh = ScriptedSSH(_result(code=1, stderr="Cannot connect to the Docker daemon"))

    ready, step = await ec2_deployment._ensure_docker(
        ssh, ec2=EC2Settings(), approve=False, dry_run=False
    )

    assert ready is False
    assert step.outcome == "refused"
    assert ssh.calls == [("docker", "version")]


# ---------------------------------------------------------------------------
# The run itself
# ---------------------------------------------------------------------------


class UnreachableSession:
    """An SSH session whose host never answers."""

    def __init__(self, **kwargs: Any) -> None:
        self.__dict__.update(kwargs)

    async def __aenter__(self) -> UnreachableSession:
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    async def run(self, *argv: str, timeout: float | None = None) -> Any:
        return fail(*argv, code=255, stderr="ssh: connect to host: No route to host")


class ReadySession(UnreachableSession):
    """Reachable, Docker already usable — then everything else is workflow."""

    async def run(self, *argv: str, timeout: float | None = None) -> Any:
        if argv[:1] == ("docker",):
            return ok(*argv, stdout="Client: Version: 27.0.0")
        return ok(*argv, stdout="Linux 6.1.0")


async def test_unreachable_target_is_recorded_once_as_failed(
    ec2_env: Any, memory: DeploymentMemoryService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ec2_deployment, "SSHSession", UnreachableSession)

    result = await run_ec2_deployment(
        EC2DeploymentRequest(repository_path="examples/sample_app")
    )

    assert result.stopped is True
    assert result.succeeded is False
    assert "unreachable" in (result.stop_reason or "")
    assert result.steps and result.steps[0].stage == "target"
    assert result.steps[0].outcome == "failed"
    assert result.target.host == "203.0.113.10"

    detail = await memory.get_deployment(result.deployment_id)
    assert detail is not None
    assert detail.status == DeploymentStatus.FAILED
    assert detail.target is not None and detail.target.host == "203.0.113.10"

    listing = await memory.list_deployments(DeploymentQuery())
    assert listing.total == 1, "one attempt must produce exactly one history row"


async def test_disabled_target_is_rejected_before_history_exists(
    memory: DeploymentMemoryService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A misconfiguration on this side is not the target's failure."""
    monkeypatch.setenv("AEGIS_EC2__ENABLED", "false")
    get_settings.cache_clear()

    with pytest.raises(ConfigurationError):
        await run_ec2_deployment(
            EC2DeploymentRequest(repository_path="examples/sample_app")
        )

    listing = await memory.list_deployments(DeploymentQuery())
    assert listing.total == 0


async def test_dry_run_reaches_the_plan_without_opening_a_scope(
    ec2_env: Any, memory: DeploymentMemoryService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dry run calls no tool, so the scope is never opened — and still plans."""
    monkeypatch.setattr(ec2_deployment, "SSHSession", ReadySession)

    result = await run_ec2_deployment(
        EC2DeploymentRequest(repository_path="examples/sample_app", dry_run=True)
    )

    assert result.stopped is False
    assert result.succeeded is False, "a dry run deployed nothing, so it is not a success"
    assert result.verification is None
    assert result.plan, "the plan is the deliverable of a dry run"
    assert all(task.tool.startswith("docker-ec2.") for task in result.plan)
    assert "docker-ec2" in (result.plan_explanation or "")
    assert [step.stage for step in result.steps] == ["target", "prepare"]
    assert all(step.outcome == "ok" for step in result.steps)

    detail = await memory.get_deployment(result.deployment_id)
    assert detail is not None
    assert detail.status == DeploymentStatus.DRY_RUN

    listing = await memory.list_deployments(DeploymentQuery())
    assert listing.total == 1, "the workflow must continue the row, not open a second"


# ---------------------------------------------------------------------------
# The endpoint
# ---------------------------------------------------------------------------


async def test_ec2_route_is_registered(client: Any) -> None:
    schema = client.get("/openapi.json").json()
    assert "/api/deployment/ec2" in schema["paths"]
    assert "post" in schema["paths"]["/api/deployment/ec2"]


async def test_ec2_route_refuses_when_the_target_is_not_configured(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The unconfigured baseline is an explicit configuration error, not a crash."""
    monkeypatch.setenv("AEGIS_EC2__ENABLED", "false")
    get_settings.cache_clear()

    response = client.post("/api/deployment/ec2", json={"repository_path": "examples/sample_app"})

    assert response.status_code == 500
    body = response.json()
    assert body["error"]["code"] == "configuration_error"
    assert "EC2 deployment is not configured" in body["error"]["message"]
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------


def test_probe_note_explains_a_port_the_firewall_hides() -> None:
    """Answering inside but not outside is a security-group problem, and says so."""
    from app.models.verification import VerificationCheck, VerificationResult

    verification = VerificationResult(
        status="WARNING",
        checks=[VerificationCheck(name="health_endpoint", outcome="fail")],
    )

    note = ec2_deployment._probe_note(verification, {"loopback": 200}, 8080)

    assert note is not None
    assert "security group" in note
    assert "8080" in note


def test_probe_note_stays_silent_when_the_inside_probe_also_failed() -> None:
    from app.models.verification import VerificationCheck, VerificationResult

    verification = VerificationResult(
        status="FAILED",
        checks=[VerificationCheck(name="health_endpoint", outcome="fail")],
    )

    assert ec2_deployment._probe_note(verification, {"loopback": None}, 8080) is None
    assert ec2_deployment._probe_note(verification, {"loopback": 502}, 8080) is None
    assert ec2_deployment._probe_note(None, {"loopback": 200}, 8080) is None


def test_settings_shape_matches_what_the_flow_reads() -> None:
    """The route's configuration contract is enforced by the settings model."""
    settings = Settings(_env_file=None)
    ec2 = settings.ec2
    assert ec2.is_configured is False, "a fresh process must not claim a target"
    assert ec2.host_port == 8080
    assert ec2.container_port == 8000
    assert ec2.health_path == "/healthz"
    assert ec2.install_docker is True
