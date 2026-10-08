"""Tests for the execution engine, the verification agent and the workflow graph.

The MCP layer is faked, but the *real* :class:`ToolExecutionPolicy` runs on every
call. A hand-written imitation of the approval gate is free to drift from
production, and then a test asserting "policy refused this" would be asserting
nothing. Using the real policy means a policy change breaks these tests, which is
the point.

Failure injection is per-tool and per-attempt, so a test can say "fail the first
attempt, succeed on the second" and prove the retry loop actually retried rather
than just reporting that it did.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

import pytest

from app.agents.debug_agent import assess_failure
from app.agents.deployment_verification import verify_deployment
from app.agents.deployment_workflow import build_plan, expected_port, run_deployment_workflow
from app.agents.execution_engine import execute_run
from app.models.docker import DockerDeployRequest
from app.models.execution import (
    ActionStatus,
    ErrorClass,
    ExecutionRequest,
    Task,
    classify_error,
    is_retryable,
)
from app.models.mcp import ToolCallRequest, ToolCallResult
from app.models.verification import VerificationRequest, VerificationResult
from app.services.mcp_policy import build_policy

pytestmark = pytest.mark.anyio

#: Mirrors the annotations the real Docker server publishes.
DOCKER_TOOLS = {
    "docker_available": "low",
    "list_images": "low",
    "container_status": "low",
    "container_health": "low",
    "container_logs": "low",
    "http_probe": "low",
    "build_image": "medium",
    "start_container": "medium",
    "stop_container": "high",
}


class FakeDocker:
    """A Docker MCP server that can be told exactly how to misbehave."""

    def __init__(self, server: str = "docker") -> None:
        #: The server name tools are registered and called under. The default is
        #: the local daemon; the EC2 flow uses its scoped server's name, and a
        #: policy registered under the wrong name would refuse every call — which
        #: is exactly what a test for that wiring should notice.
        self.server = server
        #: Every tool call that was *requested*, refused ones included.
        self.calls: list[tuple[str, dict[str, Any]]] = []
        #: Only the tools that actually ran. A refused call never reaches the
        #: tool, so this is what a "must not have run" assertion must check.
        self.executed: list[str] = []
        self.policy = self._policy()
        #: tool -> list of per-attempt behaviours, consumed in order.
        self.script: dict[str, list[dict[str, Any]]] = {}
        #: tool -> a sticky response, or a callable taking the arguments.
        self.responses: dict[str, Any] = {}
        self.default_behaviour: dict[str, Any] = {"success": True, "content": {}}
        #: Set to make http_probe hang, to exercise the timeout path.
        self.hang_tools: set[str] = set()
        self.hang_seconds = 30.0

    def _policy(self) -> Any:
        policy = build_policy()
        from app.models.mcp import ToolDefinition

        policy.register(
            [
                ToolDefinition(
                    qualified_name=f"{self.server}.{name}",
                    server=self.server,
                    name=name,
                    description=name,
                    risk_level=level,
                    read_only=level == "low",
                    destructive=name == "stop_container",
                )
                for name, level in DOCKER_TOOLS.items()
            ]
        )
        return policy

    def fail(self, tool: str, code: str, times: int = 99) -> None:
        """Make ``tool`` fail with ``code`` for the next ``times`` attempts."""
        self.script[tool] = [
            {"success": False, "error_code": code, "error_message": f"{tool} failed: {code}"}
            for _ in range(times)
        ]

    def succeed_after(self, tool: str, failures: int, code: str = "internal_error") -> None:
        """Fail ``failures`` times, then succeed."""
        self.script[tool] = [
            {"success": False, "error_code": code, "error_message": "transient"}
            for _ in range(failures)
        ] + [{"success": True, "content": {}}]

    def content(self, tool: str, payload: Any) -> None:
        """Answer ``tool`` with ``payload`` on every call.

        Sticky, not one-shot: a real tool returns the same thing for the same
        question, and the verifier probes ``/`` and ``/health`` separately.
        ``payload`` may be a callable taking the arguments, for tools whose answer
        depends on them.
        """
        self.script.pop(tool, None)
        self.responses[tool] = payload

    def _behaviour(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        queue = self.script.get(name)
        if queue:
            return queue.pop(0)
        if name in self.responses:
            payload = self.responses[name]
            if callable(payload):
                payload = payload(arguments)
            return {"success": True, "content": payload}
        return dict(self.default_behaviour)

    async def discover_tools(self) -> list[Any]:
        return []

    async def call_tool(self, request: ToolCallRequest) -> ToolCallResult:
        name = request.tool_name.split(".")[-1]
        self.calls.append((name, dict(request.arguments)))

        def failure(code: str, message: str) -> ToolCallResult:
            return ToolCallResult(
                tool_name=name,
                qualified_name=request.tool_name,
                server=self.server,
                success=False,
                error_code=code,
                error_message=message,
                requires_approval=code == "approval_required",
                requested_by=request.requested_by,
            )

        decision = self.policy.evaluate(request)
        if not decision.allowed:
            return failure("policy_denied", decision.reason or "Refused by policy.")
        if not decision.approved:
            return failure("approval_required", decision.reason or "Approval required.")

        if name in self.hang_tools:
            self.executed.append(name)
            await asyncio.sleep(self.hang_seconds)

        behaviour = self._behaviour(name, dict(request.arguments))
        if not behaviour.get("success", True):
            return failure(
                behaviour.get("error_code", "tool_error"),
                behaviour.get("error_message", "boom"),
            )
        self.executed.append(name)
        return ToolCallResult(
            tool_name=name,
            qualified_name=request.tool_name,
            server=self.server,
            success=True,
            content=behaviour.get("content", {}),
            requires_approval=decision.requires_approval,
            requested_by=request.requested_by,
        )

    def tools_called(self) -> list[str]:
        """Every tool requested, refused or not."""
        return [name for name, _ in self.calls]

    def tools_executed(self) -> list[str]:
        """Only the tools that actually ran."""
        return list(self.executed)


@pytest.fixture
def docker(monkeypatch: pytest.MonkeyPatch) -> FakeDocker:
    fake = FakeDocker()
    monkeypatch.setattr("app.services.mcp_manager.get_manager", lambda: fake)
    monkeypatch.setattr("app.agents.execution_engine.get_manager", lambda: fake)
    monkeypatch.setattr("app.agents.deployment_verification.get_manager", lambda: fake)
    return fake


def _task(task_id: str, tool: str, **kwargs: Any) -> Task:
    defaults: dict[str, Any] = {
        "id": task_id,
        "title": f"task {task_id}",
        "tool": f"docker.{tool}",
        "arguments": {"name": "c1"},
    }
    defaults.update(kwargs)
    return Task(**defaults)


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "expected", "retryable"),
    [
        ("timeout", ErrorClass.TRANSIENT, True),
        ("internal_error", ErrorClass.TRANSIENT, True),
        ("rate_limited", ErrorClass.TRANSIENT, True),
        ("policy_denied", ErrorClass.POLICY, False),
        ("approval_required", ErrorClass.POLICY, False),
        ("invalid_tool", ErrorClass.PERMANENT, False),
        ("not_found", ErrorClass.PERMANENT, False),
        ("tool_error", ErrorClass.PERMANENT, False),
        ("something_new", ErrorClass.UNKNOWN, False),
        (None, ErrorClass.UNKNOWN, False),
    ],
)
def test_error_classification(code: str | None, expected: ErrorClass, retryable: bool) -> None:
    assert classify_error(code) is expected
    assert is_retryable(code) is retryable


def test_unknown_error_is_not_retried() -> None:
    """The expensive mistake is assuming a failure is transient when it is not."""
    assert is_retryable("a_brand_new_error_code") is False


# ---------------------------------------------------------------------------
# Success
# ---------------------------------------------------------------------------


async def test_sequential_success(docker: FakeDocker) -> None:
    docker.content("docker_available", {"available": True})
    docker.content("build_image", {"image": "img:1"})
    docker.content("start_container", {"container": "c1"})

    response = await execute_run(
        ExecutionRequest(
            tasks=[
                _task("1", "docker_available"),
                _task("2", "build_image", requires_approval=True),
                _task("3", "start_container", requires_approval=True),
            ],
            approve=True,
            approval_reference="test",
        )
    )

    assert response.status == "completed"
    assert response.succeeded == ["1", "2", "3"]
    assert response.failed == []
    assert len(response.actions) == 3


async def test_tasks_run_in_order(docker: FakeDocker) -> None:
    await execute_run(
        ExecutionRequest(
            tasks=[
                _task("1", "docker_available"),
                _task("2", "build_image", requires_approval=True),
            ],
            approve=True,
        )
    )

    assert docker.tools_called() == ["docker_available", "build_image"]


async def test_dry_run_calls_nothing(docker: FakeDocker) -> None:
    response = await execute_run(
        ExecutionRequest(tasks=[_task("1", "build_image", requires_approval=True)], dry_run=True)
    )

    assert docker.tools_called() == [], "a dry run must not call a tool"
    assert response.status == "dry_run"
    assert response.actions, "a dry run still records what it would have done"
    assert response.actions[0].status is ActionStatus.SKIPPED


# ---------------------------------------------------------------------------
# Failure
# ---------------------------------------------------------------------------


async def test_failure_is_recorded(docker: FakeDocker) -> None:
    docker.fail("build_image", "tool_error")

    response = await execute_run(
        ExecutionRequest(
            tasks=[_task("1", "build_image", requires_approval=True, critical=True)],
            approve=True,
        )
    )

    assert response.status == "stopped"
    assert response.failed == ["1"]
    action = response.actions[0]
    assert action.status is ActionStatus.FAILURE
    assert action.error_code == "tool_error"
    assert action.error_class is ErrorClass.PERMANENT
    assert action.error


async def test_critical_failure_stops_the_run(docker: FakeDocker) -> None:
    docker.fail("build_image", "tool_error")

    response = await execute_run(
        ExecutionRequest(
            tasks=[
                _task("1", "docker_available"),
                _task("2", "build_image", requires_approval=True, critical=True),
                _task("3", "start_container", requires_approval=True),
            ],
            approve=True,
        )
    )

    assert response.stopped is True
    assert response.stop_task_id == "2"
    assert "critical" in (response.stop_reason or "")
    assert "start_container" not in docker.tools_called(), (
        "a task depending on a failed critical task must not run"
    )


async def test_non_critical_failure_reports_failed_not_stopped(docker: FakeDocker) -> None:
    """`stopped` and `failed` mean different things and must not be conflated.

    `stopped` is "the plan was halted part-way". A lone non-critical task that
    fails was never stopped -- there was nothing left to skip -- so the honest
    answer is `failed`.
    """
    docker.fail("build_image", "tool_error")

    response = await execute_run(
        ExecutionRequest(tasks=[_task("1", "build_image", requires_approval=True)], approve=True)
    )

    assert response.status == "failed"
    assert response.stopped is False
    assert response.failed == ["1"]


async def test_non_critical_failure_continues(docker: FakeDocker) -> None:
    docker.fail("docker_available", "tool_error")

    response = await execute_run(
        ExecutionRequest(
            tasks=[
                _task("1", "docker_available"),
                _task("2", "container_status"),
            ],
            approve=True,
        )
    )

    assert response.stopped is False
    assert "container_status" in docker.tools_called()


# ---------------------------------------------------------------------------
# Retry
# ---------------------------------------------------------------------------


async def test_transient_failure_is_retried_then_succeeds(docker: FakeDocker) -> None:
    docker.succeed_after("build_image", failures=2, code="internal_error")

    response = await execute_run(
        ExecutionRequest(
            tasks=[
                _task(
                    "1",
                    "build_image",
                    requires_approval=True,
                    max_attempts=3,
                    retry_backoff_seconds=0.0,
                )
            ],
            approve=True,
        )
    )

    assert response.succeeded == ["1"]
    assert len(response.actions) == 3
    assert [action.attempt for action in response.actions] == [1, 2, 3]
    assert [action.retry_count for action in response.actions] == [0, 1, 2]
    assert response.actions[0].status is ActionStatus.FAILURE
    assert response.actions[-1].status is ActionStatus.SUCCESS


async def test_permanent_failure_is_not_retried(docker: FakeDocker) -> None:
    docker.fail("build_image", "tool_error")

    response = await execute_run(
        ExecutionRequest(
            tasks=[
                _task(
                    "1",
                    "build_image",
                    requires_approval=True,
                    max_attempts=5,
                    retry_backoff_seconds=0.0,
                )
            ],
            approve=True,
        )
    )

    assert len(response.actions) == 1, "a permanent error must not be retried"


async def test_retries_stop_when_attempts_are_exhausted(docker: FakeDocker) -> None:
    docker.fail("build_image", "internal_error")

    response = await execute_run(
        ExecutionRequest(
            tasks=[
                _task(
                    "1",
                    "build_image",
                    requires_approval=True,
                    max_attempts=3,
                    retry_backoff_seconds=0.0,
                )
            ],
            approve=True,
        )
    )

    assert len(response.actions) == 3
    assert response.failed == ["1"]
    assert response.actions[-1].attempt == 3


# ---------------------------------------------------------------------------
# Timeout
# ---------------------------------------------------------------------------


async def test_timeout_is_enforced(docker: FakeDocker) -> None:
    docker.hang_tools.add("container_status")
    docker.hang_seconds = 5.0

    response = await execute_run(
        ExecutionRequest(tasks=[_task("1", "container_status", timeout_seconds=0.2, critical=True)])
    )

    assert response.status == "stopped"
    action = response.actions[0]
    assert action.status is ActionStatus.TIMEOUT
    assert action.error_code == "timeout"
    assert action.error_class is ErrorClass.TIMEOUT
    assert "0.2" in (action.error or "")


async def test_timeout_is_retried(docker: FakeDocker) -> None:
    docker.hang_tools.add("container_status")
    docker.hang_seconds = 5.0

    response = await execute_run(
        ExecutionRequest(
            tasks=[
                _task(
                    "1",
                    "container_status",
                    timeout_seconds=0.2,
                    max_attempts=2,
                    retry_backoff_seconds=0.0,
                )
            ]
        )
    )

    assert len(response.actions) == 2
    assert all(action.status is ActionStatus.TIMEOUT for action in response.actions)


# ---------------------------------------------------------------------------
# Policy rejection
# ---------------------------------------------------------------------------


async def test_policy_rejection_without_approval(docker: FakeDocker) -> None:
    response = await execute_run(
        ExecutionRequest(
            tasks=[_task("1", "build_image", requires_approval=True, critical=True)],
            approve=False,
        )
    )

    assert "build_image" not in docker.tools_executed(), "the tool must never be reached"
    assert response.status == "stopped"
    action = response.actions[0]
    assert action.status is ActionStatus.APPROVAL_REQUIRED
    assert action.error_code == "approval_required"
    assert action.error_class is ErrorClass.POLICY


async def test_policy_rejection_is_not_retried(docker: FakeDocker) -> None:
    response = await execute_run(
        ExecutionRequest(
            tasks=[
                _task(
                    "1",
                    "build_image",
                    requires_approval=True,
                    max_attempts=5,
                    retry_backoff_seconds=0.0,
                )
            ],
            approve=False,
        )
    )

    assert len(response.actions) == 1, "retrying a refusal would only produce identical refusals"


async def test_read_only_task_needs_no_approval(docker: FakeDocker) -> None:
    response = await execute_run(
        ExecutionRequest(tasks=[_task("1", "container_status")], approve=False)
    )

    assert response.succeeded == ["1"]


async def test_destructive_task_requires_approval(docker: FakeDocker) -> None:
    response = await execute_run(
        ExecutionRequest(
            tasks=[_task("1", "stop_container", requires_approval=True)], approve=False
        )
    )

    assert "stop_container" not in docker.tools_executed()
    assert response.actions[0].status is ActionStatus.APPROVAL_REQUIRED


# ---------------------------------------------------------------------------
# Audit trail
# ---------------------------------------------------------------------------


async def test_every_action_records_the_required_fields(docker: FakeDocker) -> None:
    docker.fail("build_image", "tool_error")

    response = await execute_run(
        ExecutionRequest(
            tasks=[
                _task(
                    "1",
                    "build_image",
                    requires_approval=True,
                    arguments={"name": "c1", "database_url": "postgres://u:p@h/db"},
                )
            ],
            approve=True,
        )
    )

    action = response.actions[0]
    assert action.timestamp is not None
    assert action.task_id == "1"
    assert action.tool == "docker.build_image"
    assert action.arguments, "arguments must be recorded"
    assert action.status is ActionStatus.FAILURE
    assert action.duration_seconds >= 0
    assert action.error
    assert action.retry_count == 0


async def test_secrets_are_redacted_in_the_audit_trail(docker: FakeDocker) -> None:
    task = _task(
        "1",
        "build_image",
        requires_approval=True,
        arguments={
            "password": "hunter2",
            "api_key": "sk-live-abc",
            "DATABASE_URL": "postgres://user:pass@db:5432/app",
            "image": "img:1",
        },
    )

    response = await execute_run(ExecutionRequest(tasks=[task], approve=True))
    recorded = response.actions[0].arguments

    assert recorded["password"] == "***redacted***"
    assert recorded["api_key"] == "***redacted***"
    assert "pass" not in recorded["DATABASE_URL"]
    assert "user" not in recorded["DATABASE_URL"]
    assert "db:5432/app" in recorded["DATABASE_URL"]
    assert recorded["image"] == "img:1", "non-secret arguments must survive intact"
    assert "hunter2" not in response.summary_markdown()


def test_secret_named_keys_are_fully_redacted() -> None:
    """A key called `dsn` is a secret by name, so the whole value goes."""
    task = Task(id="1", title="t", arguments={"dsn": "postgres://user:pw@host:5432/db"})

    assert task.redacted_arguments()["dsn"] == "***redacted***"


def test_neutral_key_with_a_credential_in_it_keeps_the_host() -> None:
    """A URL can carry a password in userinfo even under an innocuous name."""
    task = Task(id="1", title="t", arguments={"DATABASE_URL": "postgres://user:pw@host:5432/db"})

    assert task.redacted_arguments()["DATABASE_URL"] == "postgres://***redacted***@host:5432/db"


async def test_audit_log_line_is_structured(docker: FakeDocker) -> None:
    response = await execute_run(ExecutionRequest(tasks=[_task("1", "container_status")]))
    line = response.actions[0].as_log_line()

    for fragment in ("seq=", "task=", "tool=", "status=", "duration=", "retries="):
        assert fragment in line


# ---------------------------------------------------------------------------
# Verification agent
# ---------------------------------------------------------------------------


HEALTHY_CONTAINER = {
    "container": "c1",
    "status": "running",
    "running": True,
    "exit_code": 0,
    "started_at": "2024-05-01T10:00:00Z",
    "health": "healthy",
    "restart_count": 0,
    "image": "img:1",
    "created": "2024-05-01T09:00:00Z",
    "oom_killed": False,
    "ports": [{"container_port": 8000, "host_port": 8080, "host_ip": "0.0.0.0"}],
}


def _healthy(docker: FakeDocker) -> None:
    docker.content("container_status", HEALTHY_CONTAINER)
    docker.content("container_health", {"state": "healthy", "detail": "ok"})
    docker.content("container_logs", {"logs": ["listening on 8000"], "truncated": False})
    docker.content(
        "http_probe",
        {
            "reachable": True,
            "status": 200,
            "latency_ms": 3.2,
            "body": '{"status":"ok"}',
            "url": "http://127.0.0.1:8080/health",
            "content_type": "application/json",
        },
    )


async def test_verification_success(docker: FakeDocker) -> None:
    _healthy(docker)

    result = await verify_deployment(VerificationRequest(container_name="c1", expected_port=8080))

    assert result.status == "SUCCESS"
    assert result.failures == []
    assert result.warnings == []
    assert result.passed is True
    for name in (
        "container_exists",
        "container_running",
        "port_available",
        "health_endpoint",
        "health_status_code",
        "logs_clean",
    ):
        assert result.outcome_of(name) == "pass", f"{name} should pass"


async def test_verification_records_evidence(docker: FakeDocker) -> None:
    _healthy(docker)

    result = await verify_deployment(VerificationRequest(container_name="c1", expected_port=8080))

    assert result.evidence, "a verification without evidence is an assertion"
    sources = {item.source for item in result.evidence}
    assert "docker.container_status" in sources
    assert "docker.http_probe" in sources


async def test_probe_host_and_scoped_server_flow_into_the_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A remote target is probed where it is reachable, through its own server.

    The default probe is loopback and the default server is the local daemon;
    both would silently verify the wrong machine for an EC2 deployment. The
    policy is registered under ``docker-ec2`` only, so a call that still said
    ``docker.*`` would be refused and the assertions below would never see a
    probe at all.
    """
    fake = FakeDocker(server="docker-ec2")
    _healthy(fake)
    monkeypatch.setattr("app.services.mcp_manager.get_manager", lambda: fake)
    monkeypatch.setattr("app.agents.deployment_verification.get_manager", lambda: fake)

    result = await verify_deployment(
        VerificationRequest(
            container_name="c1",
            expected_port=8080,
            health_path="/health",
            docker_server="docker-ec2",
            probe_host="203.0.113.10",
        )
    )

    probes = [args for name, args in fake.calls if name == "http_probe"]
    assert probes, "the health endpoint must be probed"
    assert all(args.get("host") == "203.0.113.10" for args in probes)
    assert result.status == "SUCCESS"
    sources = {item.source for item in result.evidence}
    assert "docker-ec2.http_probe" in sources
    assert "docker-ec2.container_status" in sources


async def test_missing_container_fails_and_skips_the_rest(docker: FakeDocker) -> None:
    docker.fail("container_status", "not_found")

    result = await verify_deployment(
        VerificationRequest(container_name="ghost", expected_port=8080)
    )

    assert result.status == "FAILED"
    assert result.outcome_of("container_exists") == "fail"
    assert result.outcome_of("health_endpoint") == "skipped"
    assert "container_logs" not in docker.tools_called(), "there is no container to read logs from"


async def test_stopped_container_fails(docker: FakeDocker) -> None:
    docker.content(
        "container_status",
        {**HEALTHY_CONTAINER, "running": False, "status": "exited", "exit_code": 1},
    )

    result = await verify_deployment(VerificationRequest(container_name="c1", expected_port=8080))

    assert result.status == "FAILED"
    assert result.outcome_of("container_running") == "fail"
    assert "exit_code=1" in " ".join(
        item.detail for item in result.check("container_running").evidence
    )


async def test_oom_killed_container_fails(docker: FakeDocker) -> None:
    docker.content("container_status", {**HEALTHY_CONTAINER, "oom_killed": True, "running": False})

    result = await verify_deployment(VerificationRequest(container_name="c1", expected_port=8080))

    assert result.outcome_of("container_running") == "fail"
    assert "OOM" in result.check("container_running").detail


async def test_unpublished_port_fails(docker: FakeDocker) -> None:
    docker.content("container_status", {**HEALTHY_CONTAINER, "ports": []})

    result = await verify_deployment(VerificationRequest(container_name="c1", expected_port=8080))

    assert result.status == "FAILED"
    assert result.outcome_of("port_available") == "fail"
    assert "not published" in result.check("port_available").detail


async def test_published_but_dead_port_fails(docker: FakeDocker) -> None:
    """A mapped port that nothing listens on passes every status check and fails every request."""
    docker.content("container_status", HEALTHY_CONTAINER)
    docker.content(
        "http_probe",
        {"reachable": False, "status": None, "error": "ConnectionRefusedError", "body": ""},
    )

    result = await verify_deployment(VerificationRequest(container_name="c1", expected_port=8080))

    assert result.outcome_of("port_available") == "fail"
    assert "ConnectionRefused" in result.check("port_available").detail


async def test_health_endpoint_unreachable_fails(docker: FakeDocker) -> None:
    docker.content("container_status", HEALTHY_CONTAINER)
    docker.content(
        "http_probe", {"reachable": False, "status": None, "error": "timeout", "body": ""}
    )

    result = await verify_deployment(VerificationRequest(container_name="c1", expected_port=8080))

    assert result.outcome_of("health_endpoint") == "fail"
    assert result.outcome_of("health_status_code") == "skipped"


async def test_wrong_status_code_fails(docker: FakeDocker) -> None:
    docker.content("container_status", HEALTHY_CONTAINER)
    docker.content(
        "http_probe", {"reachable": True, "status": 503, "latency_ms": 1.0, "body": "down"}
    )

    result = await verify_deployment(VerificationRequest(container_name="c1", expected_port=8080))

    assert result.status == "FAILED"
    check = result.check("health_status_code")
    assert check.outcome in {"fail"}
    assert "503" in check.detail


async def test_image_health_unhealthy_fails(docker: FakeDocker) -> None:
    docker.content("container_status", HEALTHY_CONTAINER)
    docker.content(
        "http_probe", {"reachable": True, "status": 200, "latency_ms": 1.0, "body": "ok"}
    )
    docker.content("container_health", {"state": "unhealthy", "detail": "db unreachable"})

    result = await verify_deployment(VerificationRequest(container_name="c1", expected_port=8080))

    assert result.status == "FAILED"
    assert any("unhealthy" in item.detail for item in result.checks)


async def test_no_healthcheck_warns(docker: FakeDocker) -> None:
    docker.content("container_status", HEALTHY_CONTAINER)
    docker.content(
        "http_probe", {"reachable": True, "status": 200, "latency_ms": 1.0, "body": "ok"}
    )
    docker.content("container_health", {"state": "no_healthcheck"})
    docker.content("container_logs", {"logs": ["listening"], "truncated": False})

    result = await verify_deployment(VerificationRequest(container_name="c1", expected_port=8080))

    assert result.status == "WARNING", "absence of a health check is not evidence of health"
    assert result.passed is False


@pytest.mark.parametrize(
    "line",
    [
        "panic: runtime error: index out of range",
        "Traceback (most recent call last):",
        "Unhandled exception in worker",
        "FATAL: could not bind to port",
        "connect ECONNREFUSED 127.0.0.1:5432",
        "panic: out of memory",
        "unable to connect to database",
    ],
)
async def test_fatal_log_lines_fail(docker: FakeDocker, line: str) -> None:
    docker.content("container_status", HEALTHY_CONTAINER)
    docker.content(
        "http_probe", {"reachable": True, "status": 200, "latency_ms": 1.0, "body": "ok"}
    )
    docker.content("container_health", {"state": "healthy"})
    docker.content("container_logs", {"logs": ["started", line], "truncated": False})

    result = await verify_deployment(VerificationRequest(container_name="c1", expected_port=8080))

    assert result.outcome_of("logs_clean") == "fail", f"should have flagged: {line}"
    assert result.status == "FAILED"


@pytest.mark.parametrize(
    "line",
    [
        "ERROR_CODE=0",
        "no errors during startup",
        "Errors logged: 0",
        "INFO listening on port 8000",
        "WARN slow query took 2s",
    ],
)
async def test_benign_log_lines_do_not_fail(docker: FakeDocker, line: str) -> None:
    """A verifier that cries wolf gets switched off, which is worse than none."""
    docker.content("container_status", HEALTHY_CONTAINER)
    docker.content(
        "http_probe", {"reachable": True, "status": 200, "latency_ms": 1.0, "body": "ok"}
    )
    docker.content("container_health", {"state": "healthy"})
    docker.content("container_logs", {"logs": [line], "truncated": False})

    result = await verify_deployment(VerificationRequest(container_name="c1", expected_port=8080))

    assert result.outcome_of("logs_clean") == "pass", f"false positive on: {line}"


async def test_empty_logs_warn(docker: FakeDocker) -> None:
    docker.content("container_status", HEALTHY_CONTAINER)
    docker.content(
        "http_probe", {"reachable": True, "status": 200, "latency_ms": 1.0, "body": "ok"}
    )
    docker.content("container_health", {"state": "healthy"})
    docker.content("container_logs", {"logs": [], "truncated": False})

    result = await verify_deployment(VerificationRequest(container_name="c1", expected_port=8080))

    assert result.outcome_of("logs_clean") == "warn"
    assert result.status == "WARNING"


async def test_unverifiable_dependency_warns(docker: FakeDocker) -> None:
    docker.content("container_status", HEALTHY_CONTAINER)
    docker.content(
        "http_probe", {"reachable": True, "status": 200, "latency_ms": 1.0, "body": "ok"}
    )
    docker.content("container_health", {"state": "healthy"})
    docker.content("container_logs", {"logs": ["ok"]})
    docker.fail("container_logs", "tool_error", times=0)

    result = await verify_deployment(
        VerificationRequest(container_name="c1", expected_port=8080, dependencies=["redis"])
    )

    assert result.outcome_of("dependencies_reachable") == "warn"
    assert "could not verify" in result.check("dependencies_reachable").detail


async def test_verification_is_read_only(docker: FakeDocker) -> None:
    _healthy(docker)

    await verify_deployment(VerificationRequest(container_name="c1", expected_port=8080))

    forbidden = {"build_image", "start_container", "stop_container"}
    assert forbidden.isdisjoint(set(docker.tools_called())), (
        "verification must never change what it verifies"
    )


async def test_verification_markdown(docker: FakeDocker) -> None:
    _healthy(docker)

    result = await verify_deployment(VerificationRequest(container_name="c1", expected_port=8080))
    markdown = result.summary_markdown()

    assert markdown.startswith("# Verification: SUCCESS")
    assert "| `container_exists` |" in markdown
    assert "Recommendation" in markdown


# ---------------------------------------------------------------------------
# Debug placeholder
# ---------------------------------------------------------------------------


async def test_debug_agent_proposes_but_never_acts(docker: FakeDocker) -> None:
    docker.fail("container_status", "not_found")

    result = await verify_deployment(VerificationRequest(container_name="ghost"))
    assessment = await assess_failure(result=result)

    assert assessment.applied is False
    assert assessment.applied_action is None
    assert assessment.placeholder is True
    assert assessment.proposals, "a debug agent should suggest something"
    for proposal in assessment.proposals:
        assert proposal.requires_approval is True
    assert "stop_container" not in docker.tools_executed(), (
        "the debug agent must not act on its own proposals"
    )


async def test_debug_agent_handles_an_execution_stop() -> None:
    assessment = await assess_failure(stop_reason="build failed", stop_task_id="2")

    assert assessment.kind == "execution_stopped"
    assert assessment.applied is False
    assert assessment.hypotheses


async def test_debug_agent_hypotheses_cite_evidence(docker: FakeDocker) -> None:
    docker.fail("container_status", "not_found")

    result = await verify_deployment(VerificationRequest(container_name="ghost"))
    assessment = await assess_failure(result=result)

    assert assessment.hypotheses
    for hypothesis in assessment.hypotheses:
        assert hypothesis.evidence, "a hypothesis without evidence is a guess"
        assert hypothesis.test


# ---------------------------------------------------------------------------
# Workflow: PLAN -> EXECUTE -> VERIFY -> END
# ---------------------------------------------------------------------------


def test_plan_encodes_causal_order() -> None:
    request = DockerDeployRequest(
        repository_path="../examples/sample_app",
        image="img:1",
        container_name="c1",
        ports=["8080:8000"],
    )
    tasks = build_plan(request)
    tools = [task.tool for task in tasks]

    assert tools == ["docker.docker_available", "docker.build_image", "docker.start_container"]
    build = tasks[1]
    run = tasks[2]
    assert build.requires_approval and run.requires_approval
    assert build.timeout_seconds > run.timeout_seconds, "a build legitimately takes longer"


def test_expected_port_prefers_the_host_side() -> None:
    request = DockerDeployRequest(
        repository_path=".", image="i:1", container_name="c", ports=["8080:8000"]
    )
    assert expected_port(request) == 8080

    single = DockerDeployRequest(
        repository_path=".", image="i:1", container_name="c", ports=["8000"]
    )
    assert expected_port(single) == 8000

    none = DockerDeployRequest(repository_path=".", image="i:1", container_name="c")
    assert expected_port(none) is None


def test_workflow_graph_has_the_expected_shape() -> None:
    from app.agents.deployment_workflow import build_workflow_graph

    nodes = set(build_workflow_graph().nodes)
    assert {"plan", "execute", "verify", "debug"} <= nodes


async def test_workflow_success_ends_cleanly(docker: FakeDocker) -> None:
    _healthy(docker)
    docker.content("docker_available", {"available": True})
    docker.content("build_image", {"image": "img:1"})
    docker.content("start_container", {"container": "c1"})

    outcome = await run_deployment_workflow(
        DockerDeployRequest(
            repository_path="../examples/sample_app",
            image="img:1",
            container_name="c1",
            ports=["8080:8000"],
            approve=True,
            approval_reference="test",
        )
    )

    assert outcome["execution"].status == "completed"
    assert outcome["verification"].status == "SUCCESS"
    assert outcome["debug"] is None, "a clean run must not reach the debug agent"


async def test_workflow_build_failure_routes_to_debug(docker: FakeDocker) -> None:
    docker.content("docker_available", {"available": True})
    docker.fail("build_image", "tool_error")

    outcome = await run_deployment_workflow(
        DockerDeployRequest(
            repository_path="../examples/sample_app",
            image="img:1",
            container_name="c1",
            ports=["8080:8000"],
            approve=True,
        )
    )

    assert outcome["stopped"] is True
    assert outcome["verification"] is None, "there is nothing to verify"
    assert outcome["debug"] is not None
    assert outcome["debug"].kind == "execution_stopped"
    assert outcome["debug"].applied is False


async def test_workflow_verification_failure_routes_to_debug(docker: FakeDocker) -> None:
    docker.content("docker_available", {"available": True})
    docker.content("build_image", {"image": "img:1"})
    docker.content("start_container", {"container": "c1"})
    docker.content("container_status", {**HEALTHY_CONTAINER, "running": False, "exit_code": 1})

    outcome = await run_deployment_workflow(
        DockerDeployRequest(
            repository_path="../examples/sample_app",
            image="img:1",
            container_name="c1",
            ports=["8080:8000"],
            approve=True,
        )
    )

    assert outcome["verification"].status == "FAILED"
    assert outcome["debug"] is not None
    assert outcome["debug"].kind == "verification_failed"


async def test_workflow_without_approval_cannot_build(docker: FakeDocker) -> None:
    docker.content("docker_available", {"available": True})

    outcome = await run_deployment_workflow(
        DockerDeployRequest(
            repository_path="../examples/sample_app",
            image="img:1",
            container_name="c1",
            ports=["8080:8000"],
            approve=False,
        )
    )

    assert "build_image" not in docker.tools_executed()
    assert outcome["stopped"] is True
    assert outcome["debug"] is not None


async def test_workflow_dry_run_touches_nothing(docker: FakeDocker) -> None:
    outcome = await run_deployment_workflow(
        DockerDeployRequest(
            repository_path="../examples/sample_app",
            image="img:1",
            container_name="c1",
            ports=["8080:8000"],
            dry_run=True,
        )
    )

    assert docker.tools_called() == [], "a dry run must not touch the daemon at all"
    assert outcome["execution"].status == "dry_run"
    assert outcome["verification"] is None, "nothing was deployed, so there is nothing to verify"
    assert outcome["debug"] is None


# ---------------------------------------------------------------------------
# Routing unit tests
# ---------------------------------------------------------------------------


def test_route_after_execute() -> None:
    from app.agents.deployment_workflow import route_after_execute

    assert route_after_execute({"stopped": True}) == "debug"
    assert route_after_execute({"stopped": False}) == "verify"
    assert route_after_execute({"dry_run": True, "stopped": False}) == "end"


def test_route_after_verify() -> None:
    from app.agents.deployment_workflow import route_after_verify

    assert route_after_verify({"status": "SUCCESS"}) == "end"
    assert route_after_verify({"status": "FAILED"}) == "debug"
    assert route_after_verify({"status": "WARNING"}) == "debug"


# ---------------------------------------------------------------------------
# Model behaviour
# ---------------------------------------------------------------------------


def test_check_order_covers_the_seven_required_checks() -> None:
    from app.models.verification import CHECK_ORDER

    assert CHECK_ORDER == (
        "container_exists",
        "container_running",
        "port_available",
        "health_endpoint",
        "health_status_code",
        "logs_clean",
        "dependencies_reachable",
    )


def test_warning_is_not_a_pass() -> None:

    assert VerificationResult(status="SUCCESS").passed is True
    assert VerificationResult(status="WARNING").passed is False
    assert VerificationResult(status="FAILED").passed is False


def test_fatal_patterns_are_specific() -> None:
    """A generic 'error' substring would flag a line that merely mentions it."""
    from app.models.verification import FATAL_LOG_PATTERNS

    assert not any(pattern == "error" for pattern in FATAL_LOG_PATTERNS)
    for pattern in FATAL_LOG_PATTERNS:
        assert re.search(pattern, "INFO listening on :8000", re.IGNORECASE) is None


def test_execution_request_requires_at_least_one_task() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ExecutionRequest(tasks=[])


def test_request_rejects_a_hostile_port() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        VerificationRequest(container_name="c1", expected_port=99999)
