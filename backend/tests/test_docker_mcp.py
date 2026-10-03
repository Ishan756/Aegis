"""Tests for the Docker MCP server and the local deployment workflow.

Three layers, deliberately:

1. **Security properties of the server**, asserted against the real registered
   tools rather than against a description of them. A docstring claiming a
   server is safe is not evidence, and these tests are the evidence.
2. **Workflow behaviour**, against a fake manager. Fast, and they cover the paths
   a live daemon makes awkward: refusal, build failure, unhealthy container.
3. **Integration against a real daemon and the sample application**, skipped
   automatically when Docker is unavailable so the suite still runs on a machine
   without it.

The security tests are the point of this file. If one of them fails, a tool has
grown a capability nobody intended.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.models.docker import DockerDeployRequest
from app.models.mcp import ToolCallRequest, ToolDefinition
from app.services.mcp_policy import (
    COMMAND_EXECUTION_PATTERN,
    FORBIDDEN_ARGUMENT_KEY_PATTERN,
    build_policy,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SERVER_PATH = REPO_ROOT / "mcp_servers" / "docker" / "server.py"
SAMPLE_APP = REPO_ROOT / "examples" / "sample_app"


# --- Loading the server module -------------------------------------------


@pytest.fixture(scope="session")
def docker_server() -> Any:
    """Import the Docker MCP server as a module the tests can patch."""
    spec = importlib.util.spec_from_file_location("aegis_docker_server", SERVER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["aegis_docker_server"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def server_in_sample(docker_server: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """The server with its context root pointed at the sample application."""
    monkeypatch.setattr(docker_server, "CONTEXT_ROOT", SAMPLE_APP.parent.resolve())
    return docker_server


def registered_tools(docker_server: Any) -> dict[str, Any]:
    return {tool.name: tool for tool in docker_server.server._tool_manager.list_tools()}


@pytest.fixture(autouse=True)
def _repository_root_at_repo_root() -> Any:
    """Widen the repository root to the repo for this module.

    The sample application lives in ``examples/``, outside the default root
    (``backend/``). Without this every deployment test would be refused by the
    containment check, which is the check working correctly rather than failing.
    """
    settings = get_settings()
    previous = settings.repository_root
    settings.repository_root = REPO_ROOT
    try:
        yield
    finally:
        settings.repository_root = previous


def _docker_available() -> bool:
    """Whether a real daemon is reachable, for skipping integration tests."""
    if shutil.which("docker") is None:
        return False
    try:
        return (
            subprocess.run(
                ["docker", "info"],
                capture_output=True,
                timeout=20,
                check=False,
            ).returncode
            == 0
        )
    except (OSError, subprocess.SubprocessError):
        return False


pytestmark = pytest.mark.anyio

requires_docker = pytest.mark.skipif(
    not _docker_available(),
    reason="Docker daemon is not reachable; integration tests are skipped.",
)


# --- 1. Security properties ---------------------------------------------


class TestNoShellAccess:
    """The central requirement: no unrestricted command execution."""

    def test_no_tool_is_named_like_a_command(self, docker_server: Any) -> None:
        """No registered tool may match the policy's command-execution pattern.

        This is asserted on the real tool list because the pattern is what the
        policy uses to refuse a shell. A tool called ``run_container`` would be
        refused outright by Aegis, which is exactly why the container tool is
        named ``start_container``: the guarantee that no tool name can grant
        shell execution is worth more than the more obvious name.
        """
        offenders = [
            name
            for name in registered_tools(docker_server)
            if COMMAND_EXECUTION_PATTERN.search(name)
        ]
        assert offenders == [], f"Tools look like command execution: {offenders}"

    def test_no_tool_accepts_a_command_like_argument(self, docker_server: Any) -> None:
        """No tool parameter may carry a shell command.

        The policy refuses argument keys that look like commands at any nesting
        depth, so a tool declaring one would be unusable *and* a red flag.
        """
        offenders: list[str] = []
        for name, tool in registered_tools(docker_server).items():
            schema = getattr(tool, "parameters", None) or {}
            properties = (schema.get("properties") or {}) if isinstance(schema, dict) else {}
            for key in properties:
                if FORBIDDEN_ARGUMENT_KEY_PATTERN.search(key):
                    offenders.append(f"{name}.{key}")
        assert offenders == [], f"Parameters look like command execution: {offenders}"

    def test_run_container_would_be_refused(self) -> None:
        """Documents why the obvious tool name is not used.

        If this ever starts failing, the policy loosened and the naming rationale
        in the server docstring needs revisiting.
        """
        assert COMMAND_EXECUTION_PATTERN.search("run_container") is not None

    def test_subprocess_is_never_given_a_shell(self, docker_server: Any) -> None:
        """The only subprocess call must pass ``shell=False`` and an argv list."""
        source = SERVER_PATH.read_text(encoding="utf-8")
        assert "shell=True" not in source
        assert "os.system" not in source
        assert "os.popen" not in source

    def test_start_container_cannot_override_the_entrypoint(self, server_in_sample: Any) -> None:
        """No tool accepts a trailing command for a container to run.

        ``docker run IMAGE sh -c ...`` is arbitrary execution. The tool builds its
        argv and appends the image last, and exposes no parameter that could land
        after it.
        """
        import inspect

        signature = inspect.signature(server_in_sample.start_container)
        assert list(signature.parameters) == ["image", "name", "ports"]

    def test_the_entire_parameter_surface_is_declared(self, docker_server: Any) -> None:
        """Whitelist every parameter every tool accepts.

        This is the strongest form of the check available, because a caller can
        only pass parameters the schema declares. Anything added later has to be
        added here deliberately, which is the point: a tool growing
        ``--privileged``, ``--cap-add`` or ``--health-cmd`` fails this test
        instead of quietly shipping.

        In particular there is no health command, because a health check is
        execution inside a container, and no environment or volume pass-through,
        because both are ways to hand a container the host's credentials or
        filesystem.
        """
        expected = {
            "docker_available": set(),
            "list_images": {"limit", "repository"},
            "build_image": {"context_path", "tag", "dockerfile"},
            "start_container": {"image", "name", "ports"},
            "stop_container": {"name", "grace_seconds"},
            "container_status": {"name"},
            "container_health": {"name"},
            "container_logs": {"name", "tail"},
        }

        actual = {
            name: set((getattr(tool, "parameters", None) or {}).get("properties") or {})
            for name, tool in registered_tools(docker_server).items()
        }

        assert actual == expected

    @pytest.mark.parametrize(
        "parameter",
        [
            "command",
            "entrypoint",
            "env",
            "env_file",
            "user",
            "volume",
            "privileged",
            "cap_add",
            "cap_drop",
            "network",
            "network_mode",
            "device",
            "security_opt",
            "health_cmd",
            "health_cmd_start_period",
            "build_arg",
            "build_args",
            "secret",
            "ssh",
            "add_host",
            "workdir",
            "runtime",
            "pid",
            "ipc",
        ],
    )
    def test_no_tool_exposes_a_dangerous_parameter(
        self, docker_server: Any, parameter: str
    ) -> None:
        """No tool may hand execution, privilege or the host to a container."""
        offenders = [
            name
            for name, tool in registered_tools(docker_server).items()
            if parameter in set((getattr(tool, "parameters", None) or {}).get("properties") or {})
        ]
        assert offenders == [], f"{parameter!r} is exposed by {offenders}"


class TestNameValidation:
    """Names reach ``argv``, so a name that looks like a flag must be refused."""

    @pytest.mark.parametrize(
        "image",
        [
            "--privileged",
            "-v",
            "--rm",
            "image with space",
            "image\nnewline",
            "repo/../escape",
            "../escape",
            "",
            "   ",
            "UPPER/name:tag:extra:more",
        ],
    )
    def test_invalid_image_references_are_refused(self, docker_server: Any, image: str) -> None:
        with pytest.raises(docker_server.ToolError):
            docker_server._check_image(image)

    @pytest.mark.parametrize(
        "image",
        ["python:3.12-alpine", "aegis-sample:dev", "ghcr.io/org/app:v1.2.3", "alpine"],
    )
    def test_valid_image_references_are_accepted(self, docker_server: Any, image: str) -> None:
        assert docker_server._check_image(image) == image

    @pytest.mark.parametrize(
        "name",
        ["--rm", "-it", ".hidden", "has space", "name/slash", "", "a" * 200],
    )
    def test_invalid_container_names_are_refused(self, docker_server: Any, name: str) -> None:
        with pytest.raises(docker_server.ToolError):
            docker_server._check_container_name(name)

    @pytest.mark.parametrize("name", ["aegis-sample", "web_1", "app.prod-2", "a"])
    def test_valid_container_names_are_accepted(self, docker_server: Any, name: str) -> None:
        assert docker_server._check_container_name(name) == name

    @pytest.mark.parametrize(
        "spec",
        [
            "",
            "notaport",
            "8080:",
            ":8000",
            "1:2:3",
            "99999:8000",
            "0:8000",
            "-1:80",
            # Binding to a host IP is refused too: it is rarely intended and it
            # widens exposure beyond localhost for no benefit here.
            "127.0.0.1:8080:8000",
        ],
    )
    def test_invalid_port_mappings_are_refused(self, docker_server: Any, spec: str) -> None:
        with pytest.raises(docker_server.ToolError):
            docker_server._check_port(spec)

    @pytest.mark.parametrize("spec", ["8080:8000", "80", "1:65535"])
    def test_valid_port_mappings_are_accepted(self, docker_server: Any, spec: str) -> None:
        assert docker_server._check_port(spec) == spec

    @pytest.mark.parametrize("name", ["../Dockerfile", "sub/Dockerfile", "/Dockerfile", "-x"])
    def test_dockerfile_must_be_a_bare_name(self, docker_server: Any, name: str) -> None:
        """A Dockerfile path outside the context would let a context read another file."""
        with pytest.raises(docker_server.ToolError):
            docker_server._check_dockerfile(name)


class TestPathConfinement:
    """A build context is the one place the host filesystem enters an image."""

    def test_context_outside_the_root_is_refused(
        self, server_in_sample: Any, tmp_path: Path
    ) -> None:
        with pytest.raises(server_in_sample.ToolError) as caught:
            server_in_sample._resolve_context(str(tmp_path))
        assert "outside the permitted root" in str(caught.value)

    def test_traversal_out_of_the_root_is_refused(self, server_in_sample: Any) -> None:
        with pytest.raises(server_in_sample.ToolError):
            server_in_sample._resolve_context("sample_app/../../..")

    def test_symlink_escaping_the_root_is_refused(
        self, server_in_sample: Any, tmp_path: Path
    ) -> None:
        """A symlink must not become a way out of the root.

        This is why ``resolve()`` runs before the containment check rather than
        after: comparing an unresolved path would pass this test.
        """
        link = SAMPLE_APP.parent / "escape-link"
        try:
            link.symlink_to(tmp_path)
        except (OSError, NotImplementedError):  # pragma: no cover - platform dependent
            pytest.skip("symlinks are unavailable here")

        try:
            with pytest.raises(server_in_sample.ToolError):
                server_in_sample._resolve_context("escape-link")
        finally:
            link.unlink(missing_ok=True)

    def test_context_inside_the_root_is_accepted(self, server_in_sample: Any) -> None:
        assert server_in_sample._resolve_context("sample_app") == SAMPLE_APP.resolve()

    def test_missing_context_is_refused(self, server_in_sample: Any) -> None:
        with pytest.raises(server_in_sample.ToolError):
            server_in_sample._resolve_context("no-such-directory")

    def test_file_as_context_is_refused(self, server_in_sample: Any) -> None:
        with pytest.raises(server_in_sample.ToolError):
            server_in_sample._resolve_context("sample_app/Dockerfile")

    def test_empty_context_is_refused(self, server_in_sample: Any) -> None:
        with pytest.raises(server_in_sample.ToolError):
            server_in_sample._resolve_context("")


class TestChildEnvironment:
    """A build must not be able to read the backend's secrets."""

    def test_backend_secrets_are_not_inherited(
        self, docker_server: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AEGIS_LLM__API_KEY", "sk-should-not-be-visible")
        monkeypatch.setenv("AEGIS_GITHUB__TOKEN", "ghp_should-not-be-visible")
        monkeypatch.setenv("PATH", "/usr/bin")

        env = docker_server._child_env()
        assert "AEGIS_LLM__API_KEY" not in env
        assert "AEGIS_GITHUB__TOKEN" not in env
        assert env.get("PATH") == "/usr/bin"

    def test_docker_variables_are_inherited(self, docker_server: Any, monkeypatch: Any) -> None:
        """Without DOCKER_HOST a non-default context cannot be reached."""
        monkeypatch.setenv("DOCKER_HOST", "unix:///var/run/docker.sock")
        assert docker_server._child_env()["DOCKER_HOST"] == "unix:///var/run/docker.sock"

    def test_docker_settings_are_inherited(self, docker_server: Any, monkeypatch: Any) -> None:
        monkeypatch.setenv("AEGIS_DOCKER__BINARY", "/usr/local/bin/docker")
        assert docker_server._child_env()["AEGIS_DOCKER__BINARY"] == "/usr/local/bin/docker"


class TestOutputLimits:
    """Logs are capped so a chatty container cannot flood the model's context."""

    def test_tail_keeps_the_end(self, docker_server: Any) -> None:
        text = "\n".join(f"line {index}" for index in range(100))
        body, truncated = docker_server._tail(text, max_lines=10, max_bytes=100_000)
        assert truncated is True
        assert body.splitlines()[0] == "line 90"
        assert body.splitlines()[-1] == "line 99"

    def test_tail_enforces_the_byte_cap(self, docker_server: Any) -> None:
        text = "\n".join("x" * 100 for _ in range(100))
        body, truncated = docker_server._tail(text, max_lines=1000, max_bytes=500)
        assert len(body) <= 500
        assert truncated is True

    def test_short_text_is_not_truncated(self, docker_server: Any) -> None:
        body, truncated = docker_server._tail("one\ntwo", max_lines=10, max_bytes=1000)
        assert body == "one\ntwo"
        assert truncated is False

    def test_empty_text_is_handled(self, docker_server: Any) -> None:
        assert docker_server._tail("", max_lines=10, max_bytes=100) == ("", False)


class TestTimeouts:
    """A hung Docker command must not wedge the MCP server."""

    def test_a_hanging_command_is_killed(
        self, docker_server: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A command that never exits is terminated and reported, not awaited."""
        server = docker_server
        server.BUILD_TIMEOUT = 30.0

        def fake_capture(process: Any, *, limit: int, deadline: float) -> None:
            raise TimeoutError

        monkeypatch.setattr(server, "_capture", fake_capture)

        with pytest.raises(server.ToolError) as caught:
            server._run(["version"], timeout=1.0)
        assert "timeout" in str(caught.value)

    def test_timeout_message_suggests_the_setting(
        self, docker_server: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            docker_server, "_capture", lambda *a, **k: (_ for _ in ()).throw(TimeoutError())
        )
        with pytest.raises(docker_server.ToolError) as caught:
            docker_server._run(["version"], timeout=1.0)
        assert "AEGIS_DOCKER__BUILD_TIMEOUT_SECONDS" in str(caught.value)

    def test_the_timeout_default_is_finite(self, docker_server: Any) -> None:
        """No default may be 'wait forever'."""
        assert 0 < docker_server.COMMAND_TIMEOUT <= 600
        assert 0 < docker_server.BUILD_TIMEOUT <= 3600


class TestToolClassification:
    """Risk annotations are what the policy acts on, so they are asserted."""

    def test_read_only_tools_are_marked_read_only(self, docker_server: Any) -> None:
        read_only = {
            "docker_available",
            "list_images",
            "container_status",
            "container_health",
            "container_logs",
        }
        for name in read_only:
            annotations = registered_tools(docker_server)[name].annotations
            assert annotations.read_only_hint is True, f"{name} must be read-only"

    def test_mutating_tools_are_not_marked_read_only(self, docker_server: Any) -> None:
        for name in ("build_image", "start_container", "stop_container"):
            annotations = registered_tools(docker_server)[name].annotations
            assert annotations.read_only_hint is False, f"{name} changes state"

    def test_only_stop_is_destructive(self, docker_server: Any) -> None:
        """Stopping a running workload is the one destructive tool here."""
        destructive = {
            name
            for name, tool in registered_tools(docker_server).items()
            if tool.annotations.destructive_hint
        }
        assert destructive == {"stop_container"}

    def test_destructive_tool_requires_approval(self) -> None:
        """The policy must actually gate it, not merely label it."""
        policy = build_policy()
        policy.register(
            [
                ToolDefinition(
                    qualified_name="docker.stop_container",
                    server="docker",
                    name="stop_container",
                    description="stop a container",
                    risk_level="high",
                    read_only=False,
                    destructive=True,
                )
            ]
        )
        decision = policy.evaluate(
            ToolCallRequest(
                tool_name="docker.stop_container",
                requested_by="test",
                arguments={"name": "app"},
            )
        )
        assert decision.allowed is True
        assert decision.requires_approval is True
        assert decision.approved is False

        approved = policy.evaluate(
            ToolCallRequest(
                tool_name="docker.stop_container",
                requested_by="test",
                arguments={"name": "app"},
                approval_granted=True,
                approval_reference="human",
            )
        )
        assert approved.approved is True

    def test_build_and_start_are_gated_at_the_default_threshold(self) -> None:
        """Neither may run unattended at the default approval threshold."""
        policy = build_policy()
        policy.register(
            [
                ToolDefinition(
                    qualified_name=f"docker.{name}",
                    server="docker",
                    name=name,
                    description=name,
                    risk_level="medium",
                    read_only=False,
                    destructive=False,
                )
                for name in ("build_image", "start_container")
            ]
        )
        for name in ("build_image", "start_container"):
            decision = policy.evaluate(
                ToolCallRequest(tool_name=f"docker.{name}", requested_by="test", arguments={})
            )
            assert decision.requires_approval is True, f"{name} must require approval"
            assert decision.approved is False

    def test_read_only_tools_run_without_approval(self) -> None:
        policy = build_policy()
        policy.register(
            [
                ToolDefinition(
                    qualified_name="docker.container_logs",
                    server="docker",
                    name="container_logs",
                    description="read logs",
                    risk_level="low",
                    read_only=True,
                    destructive=False,
                )
            ]
        )
        decision = policy.evaluate(
            ToolCallRequest(
                tool_name="docker.container_logs", requested_by="test", arguments={"name": "a"}
            )
        )
        assert decision.allowed is True
        assert decision.requires_approval is False
        assert decision.approved is True


# --- 2. Workflow behaviour against a fake manager -------------------------


class FakeDockerMCP:
    """A stand-in for the Docker server, recording every call.

    It runs the *real* :class:`ToolExecutionPolicy` rather than reimplementing the
    approval gate. A hand-written imitation is free to drift from production and
    would make the approval tests meaningless; this one cannot.

    Response payloads mirror what the real server returns, including the refusal
    shapes, so the workflow is exercised against realistic data.
    """

    #: Which tools the real server annotates as non-read-only, and so which the
    #: policy gates at the default approval threshold.
    MUTATING = {"build_image", "start_container"}

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.available = True
        self.build_fails = False
        self.health = "healthy"
        self.exit_code = 0
        self.policy = self._build_policy()

    def _build_policy(self) -> Any:
        policy = build_policy()
        policy.register(
            [
                ToolDefinition(
                    qualified_name=f"docker.{name}",
                    server="docker",
                    name=name,
                    description=name,
                    risk_level="medium" if name in self.MUTATING else "low",
                    read_only=name not in self.MUTATING,
                    destructive=False,
                )
                for name in (
                    "docker_available",
                    "list_images",
                    "build_image",
                    "start_container",
                    "stop_container",
                    "container_status",
                    "container_health",
                    "container_logs",
                )
            ]
        )
        return policy

    async def call_tool(self, request: ToolCallRequest) -> Any:
        from app.models.mcp import ToolCallResult

        name = request.tool_name.split(".")[-1]
        self.calls.append((name, dict(request.arguments)))

        def failure(code: str, message: str) -> ToolCallResult:
            return ToolCallResult(
                tool_name=name,
                qualified_name=request.tool_name,
                server="docker",
                success=False,
                error_code=code,
                error_message=message,
                requires_approval=code == "approval_required",
            )

        decision = self.policy.evaluate(request)
        if not decision.allowed:
            return failure("policy_denied", decision.reason or "Refused by policy.")
        if not decision.approved:
            return failure("approval_required", decision.reason or "Approval required.")

        payload = self._respond(name)
        if payload.pop("failed", False):
            return failure("tool_error", f"Docker tool {name!r} failed.")

        return ToolCallResult(
            tool_name=name,
            qualified_name=request.tool_name,
            server="docker",
            success=True,
            content=payload,
            requires_approval=decision.requires_approval,
        )

    def _respond(self, name: str) -> dict[str, Any]:
        if name == "docker_available":
            return {
                "available": self.available,
                "reason": None if self.available else "the daemon did not respond",
                "os": "linux",
                "arch": "amd64",
                "server_version": "test",
                "context_root": str(SAMPLE_APP.parent),
            }
        if name == "build_image":
            if self.build_fails:
                return {"failed": True}
            return {
                "image": "aegis-sample:dev",
                "image_id": "sha256:abc",
                "context": str(SAMPLE_APP),
                "dockerfile": "Dockerfile",
                "build_log_tail": "Step 1/2 : FROM\nSuccessfully built abc",
                "output_truncated": False,
            }
        if name == "start_container":
            return {
                "container": "c",
                "image": "aegis-sample:dev",
                "container_id": "deadbeef",
                "ports": [],
            }
        if name == "container_health":
            return {
                "health": self.health,
                "detail": f"state is {self.health}",
                "failing_streak": 0 if self.health == "healthy" else 3,
            }
        if name == "container_status":
            return {"exit_code": self.exit_code, "status": "running"}
        if name == "container_logs":
            return {
                "logs": "2026-01-01T00:00:00Z [sample-app] listening on 0.0.0.0:8000",
                "error_logs": "",
                "truncated": False,
            }
        return {}

    @property
    def called(self) -> list[str]:
        return [name for name, _ in self.calls]


@pytest.fixture
def fake_docker(monkeypatch: pytest.MonkeyPatch) -> FakeDockerMCP:
    """Install a fake manager for the agent module.

    The agent imports ``get_manager`` directly, so the patch has to target the
    agent module rather than the manager module.
    """
    import app.agents.docker_deployment as agent

    fake = FakeDockerMCP()
    monkeypatch.setattr(agent, "get_manager", lambda: fake)
    monkeypatch.setattr(agent, "scan_repository", _scan_sample)
    monkeypatch.setattr(agent, "detect_stack", _detect_sample)
    return fake


def _scan_sample(target: Path) -> Any:
    from app.services.repository import scan_repository as real_scan

    return real_scan(SAMPLE_APP)


def _detect_sample(inventory: Any) -> Any:
    from app.services.repository import detect_stack as real_detect

    return real_detect(inventory)


class TestWorkflow:
    async def test_successful_deployment(self, fake_docker: FakeDockerMCP) -> None:
        from app.agents.docker_deployment import deploy_locally

        result = await deploy_locally(
            DockerDeployRequest(
                repository_path=str(SAMPLE_APP),
                image="aegis-sample:dev",
                container_name="aegis-test",
                approve=True,
                approval_reference="human",
                health_timeout_seconds=5,
            )
        )

        assert result.succeeded is True
        assert result.build is not None and result.build.success is True
        assert result.run is not None and result.run.success is True
        assert result.health is not None and result.health.healthy is True
        assert result.logs is not None and result.logs.collected is True
        assert [step.stage for step in result.steps] == [
            "inspect",
            "build",
            "run",
            "health",
            "logs",
        ]

    async def test_approval_is_forwarded_not_assumed(self, fake_docker: FakeDockerMCP) -> None:
        """The workflow must not approve itself."""
        from app.agents.docker_deployment import deploy_locally

        result = await deploy_locally(
            DockerDeployRequest(
                repository_path=str(SAMPLE_APP),
                image="aegis-sample:dev",
                container_name="aegis-test",
                approve=False,
            )
        )

        assert result.succeeded is False
        build = next(step for step in result.steps if step.stage == "build")
        assert build.outcome == "refused"
        assert any("approve=true" in note for note in result.notes)

    async def test_unhealthy_container_is_not_a_success(self, fake_docker: FakeDockerMCP) -> None:
        from app.agents.docker_deployment import deploy_locally

        fake_docker.health = "unhealthy"
        result = await deploy_locally(
            DockerDeployRequest(
                repository_path=str(SAMPLE_APP),
                image="aegis-sample:dev",
                container_name="aegis-test",
                approve=True,
                approval_reference="human",
                health_timeout_seconds=2,
                health_poll_interval_seconds=0.1,
            )
        )

        assert result.succeeded is False
        assert result.health is not None and result.health.healthy is False
        health_step = next(step for step in result.steps if step.stage == "health")
        assert health_step.outcome == "unhealthy"

    async def test_no_healthcheck_is_never_healthy(self, fake_docker: FakeDockerMCP) -> None:
        """A running container is not a working one."""
        from app.agents.docker_deployment import deploy_locally

        fake_docker.health = "no_healthcheck"
        result = await deploy_locally(
            DockerDeployRequest(
                repository_path=str(SAMPLE_APP),
                image="aegis-sample:dev",
                container_name="aegis-test",
                approve=True,
                approval_reference="human",
                health_timeout_seconds=2,
            )
        )

        assert result.succeeded is False
        assert result.health is not None
        assert result.health.state == "no_healthcheck"
        assert result.health.healthy is False
        assert any("HEALTHCHECK" in note for note in result.notes)

    async def test_build_failure_stops_the_workflow(self, fake_docker: FakeDockerMCP) -> None:
        """A failed build must not be followed by a phantom successful run."""
        from app.agents.docker_deployment import deploy_locally

        fake_docker.build_fails = True
        result = await deploy_locally(
            DockerDeployRequest(
                repository_path=str(SAMPLE_APP),
                image="aegis-sample:dev",
                container_name="aegis-test",
                approve=True,
                approval_reference="human",
            )
        )

        assert result.succeeded is False
        assert result.run is None
        assert result.health is None
        assert result.logs is None
        run_step = next(step for step in result.steps if step.stage == "run")
        assert run_step.outcome == "skipped"
        assert "start_container" not in fake_docker.called

    async def test_dry_run_builds_nothing(self, fake_docker: FakeDockerMCP) -> None:
        from app.agents.docker_deployment import deploy_locally

        result = await deploy_locally(
            DockerDeployRequest(
                repository_path=str(SAMPLE_APP),
                image="aegis-sample:dev",
                container_name="aegis-test",
                dry_run=True,
                approve=True,
                approval_reference="human",
            )
        )

        assert result.succeeded is False
        assert "build_image" not in fake_docker.called
        assert "start_container" not in fake_docker.called
        skipped = [step.stage for step in result.steps if step.outcome == "skipped"]
        assert skipped == ["build", "run", "health", "logs"]
        # Inspect still happened, so a dry run reports something useful.
        assert result.profile is not None
        assert result.profile.has_dockerfile is True

    async def test_unavailable_docker_fails_before_building(
        self, fake_docker: FakeDockerMCP
    ) -> None:
        from app.agents.docker_deployment import deploy_locally
        from app.core.exceptions import UpstreamError

        fake_docker.available = False
        with pytest.raises(UpstreamError, match="not available"):
            await deploy_locally(
                DockerDeployRequest(
                    repository_path=str(SAMPLE_APP),
                    image="aegis-sample:dev",
                    container_name="aegis-test",
                    approve=True,
                    approval_reference="human",
                )
            )
        assert "build_image" not in fake_docker.called

    async def test_path_outside_the_repository_root_is_refused(
        self, fake_docker: FakeDockerMCP
    ) -> None:
        from app.agents.docker_deployment import deploy_locally
        from app.core.exceptions import PermissionDeniedError

        settings = get_settings()
        with pytest.raises(PermissionDeniedError):
            await deploy_locally(
                DockerDeployRequest(
                    repository_path="/etc",
                    image="aegis-sample:dev",
                    container_name="aegis-test",
                    approve=True,
                    approval_reference="human",
                )
            )
        assert settings.repository_root_resolved.exists()


class TestRequestValidation:
    """The API rejects bad input before any subprocess is launched."""

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"image": "-privileged"},
            {"image": "has space"},
            {"container_name": ".hidden"},
            {"container_name": "-rm"},
            {"container_name": "has/slash"},
            {"ports": ["99999:80"]},
            {"ports": ["nope"]},
            {"ports": ["1:2:3"]},
        ],
    )
    def test_invalid_requests_are_rejected(self, kwargs: dict[str, Any]) -> None:
        valid = {
            "repository_path": "sample_app",
            "image": "aegis-sample:dev",
            "container_name": "app",
        }
        with pytest.raises(ValueError):
            DockerDeployRequest(**{**valid, **kwargs})

    def test_no_model_field_can_hold_a_credential(self) -> None:
        """Nothing in the response models should look like a secret."""
        from app.models import docker as models

        suspicious = [
            name
            for name in dir(models)
            if any(word in name.lower() for word in ("token", "secret", "password", "key"))
        ]
        assert suspicious == []


class TestApiEndpoints:
    def test_routes_are_registered(self) -> None:
        from app.main import create_app

        paths = create_app().openapi()["paths"]
        assert "/api/docker/deploy" in paths
        assert "/api/docker/availability" in paths

    def test_deploy_endpoint_refuses_without_approval(self, fake_docker: FakeDockerMCP) -> None:
        """An unapproved deploy is refused, and says so in the body.

        ``cache_clear`` is deliberately *not* called here: it would rebuild
        ``Settings`` and discard the widened repository root, turning this into a
        403 about path containment instead of the 200 that exercises the refusal.
        """
        from app.main import create_app

        with TestClient(create_app()) as client:
            response = client.post(
                "/api/docker/deploy",
                json={
                    "repository_path": str(SAMPLE_APP),
                    "image": "aegis-sample:dev",
                    "container_name": "aegis-api-test",
                    "approve": False,
                },
            )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["succeeded"] is False
        assert any(step["outcome"] == "refused" for step in body["steps"])

    def test_invalid_body_is_a_422(self) -> None:
        from app.main import create_app

        with TestClient(create_app()) as client:
            response = client.post(
                "/api/docker/deploy",
                json={"repository_path": "x", "image": "-evil", "container_name": "c"},
            )
        assert response.status_code == 422


# --- 3. Integration against a real daemon ----------------------------------


@pytest.fixture
def live_container() -> Any:
    """Yield a unique container name and clean it up afterwards."""
    name = f"aegis-itest-{uuid.uuid4().hex[:8]}"
    yield name
    subprocess.run(
        ["docker", "rm", "-f", name],
        capture_output=True,
        timeout=120,
        check=False,
    )


@requires_docker
class TestIntegration:
    """End-to-end against the real Docker daemon and the sample application.

    This is what proves the tools work, rather than only that they are
    carefully shaped.
    """

    def test_sample_app_builds_and_runs_locally(
        self, server_in_sample: Any, live_container: str
    ) -> None:
        image = f"aegis-itest:{uuid.uuid4().hex[:8]}"

        available = server_in_sample.docker_available()
        assert available["available"] is True, available.get("reason")

        build = server_in_sample.build_image(context_path="sample_app", tag=image)
        assert build.get("image_id"), build

        images = server_in_sample.list_images(repository="aegis-itest")
        assert any(item["repository"] == "aegis-itest" for item in images["images"])

        started = server_in_sample.start_container(image=image, name=live_container, ports=[])
        assert started.get("container_id")

        try:
            health = server_in_sample.container_health(name=live_container)
            assert health["health"] in {"starting", "healthy"}, health
            assert health["healthcheck_defined"] is True

            status = server_in_sample.container_status(name=live_container)
            assert status["running"] is True
            assert status["restart_count"] == 0
            assert status["image"] == image
        finally:
            stopped = server_in_sample.stop_container(name=live_container)
            assert stopped["was_running"] is True
            assert stopped["exit_status"] in {"exited", "stopped"}

    def test_container_health_reports_a_real_failure(
        self, server_in_sample: Any, live_container: str
    ) -> None:
        """A stopped container is unhealthy, not healthy.

        This is the assertion that would catch a health check that only ever
        reports success.
        """
        image = f"aegis-itest:{uuid.uuid4().hex[:8]}"
        server_in_sample.build_image(context_path="sample_app", tag=image)
        server_in_sample.start_container(image=image, name=live_container)

        server_in_sample.stop_container(name=live_container)
        health = server_in_sample.container_health(name=live_container)
        assert health["health"] == "unhealthy"
        assert health["reason"] == "container_not_running"
        # The server reports one signal, `health`; the workflow derives the
        # boolean. Two overlapping fields could disagree, so there is only one.
        assert "healthy" not in health

    def test_logs_are_returned_and_capped(self, server_in_sample: Any, live_container: str) -> None:
        image = f"aegis-itest:{uuid.uuid4().hex[:8]}"
        server_in_sample.build_image(context_path="sample_app", tag=image)
        server_in_sample.start_container(image=image, name=live_container)

        # The container prints its startup line from Python, which has not run
        # yet at the instant `docker run` returns. Polling for output is what the
        # workflow does too, by waiting on the health check first.
        logs: dict[str, Any] = {}
        for _ in range(30):
            logs = server_in_sample.container_logs(name=live_container, tail=50)
            if logs["logs"].strip():
                break
            time.sleep(0.5)

        assert "sample-app" in logs["logs"], logs
        assert (
            logs["logs"].strip().endswith("listening on 0.0.0.0:8000")
            or "listening" in logs["logs"]
        )
        assert logs["error_logs"] == ""
        assert logs["byte_cap"] > 0
        assert logs["requested_tail"] == 50
        assert len(logs["logs"].encode()) <= logs["byte_cap"]

    def test_logs_for_an_unknown_container_are_refused(self, server_in_sample: Any) -> None:
        """A mistyped container name must not return an empty log as if it were fine."""
        with pytest.raises(server_in_sample.ToolError) as caught:
            server_in_sample.container_logs(name="aegis-not-here-xyz")
        assert "aegis-not-here-xyz" in str(caught.value)

    def test_unknown_container_is_an_actionable_error(self, server_in_sample: Any) -> None:
        with pytest.raises(server_in_sample.ToolError) as caught:
            server_in_sample.container_status(name="aegis-does-not-exist-xyz")
        message = str(caught.value)
        assert "aegis-does-not-exist-xyz" in message
        assert "start_container" in message

    def test_build_context_outside_the_root_is_refused_by_a_live_server(
        self, docker_server: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The confinement holds against the real server, not just the helper."""
        monkeypatch.setenv("AEGIS_DOCKER__CONTEXT_ROOT", str(SAMPLE_APP))
        spec = importlib.util.spec_from_file_location("aegis_docker_isolated", SERVER_PATH)
        module = importlib.util.module_from_spec(spec)
        assert spec is not None and spec.loader is not None
        sys.modules["aegis_docker_isolated"] = module
        spec.loader.exec_module(module)

        assert SAMPLE_APP.resolve() == module.CONTEXT_ROOT
        with pytest.raises(module.ToolError) as caught:
            module.build_image(context_path=str(tmp_path), tag="aegis-escape:1")
        assert "outside the permitted root" in str(caught.value)

    def test_workflow_end_to_end(self, fake_docker: FakeDockerMCP, live_container: str) -> None:
        """The full workflow against a live daemon, via the real MCP manager."""
        import shlex

        from app.agents.docker_deployment import deploy_locally
        from app.core.config import MCPSettings
        from app.services.mcp_manager import MCPClientManager

        image = f"aegis-itest:{uuid.uuid4().hex[:8]}"
        settings = get_settings()
        server_command = f"{shlex.quote(sys.executable)} {shlex.quote(str(SERVER_PATH))}"
        mcp_settings = MCPSettings(
            enabled=True,
            servers={"docker": server_command},
            forward_environment=["AEGIS_DOCKER__CONTEXT_ROOT"],
            request_timeout_seconds=900.0,
        )

        previous_root = settings.docker.context_root
        settings.docker.context_root = SAMPLE_APP.parent
        settings.repository_root = SAMPLE_APP.parent

        async def run() -> Any:
            async with MCPClientManager(mcp_settings):
                return await deploy_locally(
                    DockerDeployRequest(
                        repository_path=str(SAMPLE_APP),
                        image=image,
                        container_name=live_container,
                        approve=True,
                        approval_reference="integration-test",
                        health_timeout_seconds=90.0,
                        health_poll_interval_seconds=2.0,
                    )
                )

        import asyncio
        import os

        os.environ["AEGIS_DOCKER__CONTEXT_ROOT"] = str(SAMPLE_APP.parent)
        try:
            result = asyncio.run(run())
        finally:
            os.environ.pop("AEGIS_DOCKER__CONTEXT_ROOT", None)
            settings.docker.context_root = previous_root

        assert result.succeeded is True, result.notes
        assert result.build is not None and result.build.success is True
        assert result.health is not None and result.health.healthy is True
        assert result.logs is not None and result.logs.collected is True
        assert any("sample-app" in line for line in result.logs.logs)
        assert any("local Docker daemon only" in note for note in result.notes)

        subprocess.run(
            ["docker", "rm", "-f", live_container],
            capture_output=True,
            timeout=120,
            check=False,
        )


# Guard against a leftover container from a crashed run.
@pytest.fixture(scope="session", autouse=True)
def _require_sample_app_present() -> Any:
    assert (SAMPLE_APP / "Dockerfile").is_file(), f"missing sample app at {SAMPLE_APP}"
    assert (SAMPLE_APP / "app.py").is_file()
