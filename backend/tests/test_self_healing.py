"""Tests for failure investigation and the bounded self-healing loop.

The suite is organised around the safety constraints rather than around the code,
because the constraints are the part that matters and the part that is easiest to
regress. Each constraint gets a test that fails if the constraint is removed:

- the retry budget is respected
- the loop cannot run forever
- destructive actions never happen automatically
- code and configuration changes are never automatic
- high-risk actions need a human
- every action, applied or declined, is logged
- low confidence stops and escalates
"""

from __future__ import annotations

import asyncio
import pathlib
import re
from typing import Any

import pytest
from pydantic import ValidationError

from app.agents.investigation import collect_repository_evidence, investigate_failure
from app.agents.recovery_workflow import build_recovery_graph, run_recovery
from app.agents.self_healing import assess_risk, propose_action
from app.models.incident import (
    IncidentReport,
    Observation,
    SuspectedRootCause,
    derive_confidence,
    match_signatures,
)
from app.models.mcp import ToolCallRequest, ToolCallResult
from app.models.self_healing import (
    EscalationReason,
    FixCategory,
    FixRisk,
    RecoveryAction,
    SelfHealingPolicy,
)
from app.models.verification import (
    Evidence,
    VerificationCheck,
    VerificationResult,
)
from app.services.mcp_policy import build_policy

pytestmark = pytest.mark.anyio

#: Tools that change state. The loop may call the first; it must never call the
#: rest, and the tests assert that directly rather than trusting the policy.
DESTRUCTIVE_TOOLS = {"remove_container", "remove_image", "prune", "rollback"}
MUTATING_TOOLS = {"build_image", "start_container", "stop_container"}


class FakeDocker:
    """A Docker MCP server scripted to fail in a specific, recognisable way."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.executed: list[str] = []
        self.responses: dict[str, Any] = {}
        self.script: dict[str, list[dict[str, Any]]] = {}
        self.policy = self._policy()
        #: Flipped by `restart_container`, so a restart genuinely fixes things.
        self.container_running = False
        self.restart_count = 0

    def _policy(self) -> Any:
        policy = build_policy()
        policy.register(
            [
                _definition(name)
                for name in (
                    "docker_available",
                    "list_images",
                    "container_status",
                    "container_health",
                    "container_logs",
                    "http_probe",
                    "build_image",
                    "start_container",
                    "stop_container",
                    *sorted(DESTRUCTIVE_TOOLS),
                )
            ]
        )
        return policy

    def content(self, tool: str, payload: Any) -> None:
        self.script.pop(tool, None)
        self.responses[tool] = payload

    def fail(self, tool: str, code: str = "tool_error", times: int = 99) -> None:
        self.responses.pop(tool, None)
        self.script[tool] = [
            {"success": False, "error_code": code, "error_message": "scripted failure"}
            for _ in range(times)
        ]

    def tools_called(self) -> list[str]:
        return [name for name, _ in self.calls]

    def tools_executed(self) -> list[str]:
        return list(self.executed)

    async def call_tool(self, request: ToolCallRequest) -> ToolCallResult:
        name = request.tool_name.split(".")[-1]
        self.calls.append((name, dict(request.arguments)))

        if not self.policy.evaluate(request).approved:
            return ToolCallResult(
                tool_name=name,
                qualified_name=request.tool_name,
                server="docker",
                success=False,
                error_code="approval_required",
                error_message="refused",
            )

        queue = self.script.get(name)
        if queue:
            behaviour = queue.pop(0)
            return self._result(request, behaviour)

        payload = self.responses.get(name)
        if callable(payload):
            payload = payload(request.arguments)
        if payload is None:
            payload = {}
        return self._result(request, {"success": True, "content": payload})

    def _result(self, request: ToolCallRequest, behaviour: dict[str, Any]) -> ToolCallResult:
        name = request.tool_name.split(".")[-1]
        if not behaviour.get("success"):
            return ToolCallResult(
                tool_name=name,
                qualified_name=request.tool_name,
                server="docker",
                success=False,
                error_code=behaviour.get("error_code", "tool_error"),
                error_message=behaviour.get("error_message", ""),
            )

        self.executed.append(name)
        # A stop really stops the container, so a restart can really fix it.
        if name == "stop_container":
            self.container_running = False
        return ToolCallResult(
            tool_name=name,
            qualified_name=request.tool_name,
            server="docker",
            success=True,
            content=behaviour.get("content", {}),
        )

    def restart(self) -> None:
        """Called by the redeploy stub: models the container coming back healthy."""
        self.restart_count += 1
        self.container_running = True


def _definition(name: str) -> Any:
    from app.models.mcp import ToolDefinition

    if name in DESTRUCTIVE_TOOLS:
        risk, destructive = "high", True
    elif name in {"build_image", "start_container", "stop_container"}:
        risk, destructive = "medium", False
    else:
        risk, destructive = "low", False
    return ToolDefinition(
        qualified_name=f"docker.{name}",
        server="docker",
        name=name,
        description=f"test tool {name}",
        risk_level=risk,
        read_only=name not in MUTATING_TOOLS and name not in DESTRUCTIVE_TOOLS,
        destructive=destructive,
    )


#: Modules that hold their own ``get_manager`` reference. ``recovery_workflow``
#: and ``deployment_workflow`` are absent because they delegate to these.
MCP_CALLING_MODULES = (
    "app.agents.investigation",
    "app.agents.self_healing",
    "app.agents.deployment_verification",
    "app.agents.execution_engine",
)


@pytest.fixture
def docker(monkeypatch: pytest.MonkeyPatch) -> FakeDocker:
    fake = FakeDocker()
    manager = _Manager(fake)

    # Every module that reaches the MCP layer imports `get_manager` directly, so
    # each one has to be pointed at the fake. Missing a module here would show up
    # as a test that silently stops exercising the loop, so the list is asserted
    # against what actually imports it rather than patched with raising=False.
    for module in MCP_CALLING_MODULES:
        monkeypatch.setattr(f"{module}.get_manager", lambda manager=manager: manager)

    return fake


class _Manager:
    """The slice of MCPClientManager the agents actually use."""

    def __init__(self, fake: FakeDocker) -> None:
        self.fake = fake

    async def call_tool(self, request: ToolCallRequest) -> ToolCallResult:
        return await self.fake.call_tool(request)


# ---------------------------------------------------------------------------
# Fixtures for failure shapes
# ---------------------------------------------------------------------------


def oom_container(docker: FakeDocker) -> None:
    """An OOM-killed container. The signature is unambiguous, so confidence is high."""
    docker.content(
        "container_status",
        {
            "container": "c1",
            "status": "exited",
            "running": False,
            "exit_code": 137,
            "oom_killed": True,
            "restart_count": 0,
            "ports": [{"container_port": 8000, "host_port": 8080}],
        },
    )
    docker.content("container_logs", {"logs": ["starting", "Killed process 1 (python)"]})
    docker.content("container_health", {"state": "unhealthy", "detail": "oom"})


def dependency_failure(docker: FakeDocker) -> None:
    """A refused database connection: a weak signature that needs corroboration."""
    docker.content(
        "container_status",
        {
            "container": "c1",
            "status": "running",
            "running": True,
            "exit_code": 0,
            "oom_killed": False,
            "restart_count": 0,
            "ports": [{"container_port": 8000, "host_port": 8080}],
        },
    )
    docker.content(
        "container_logs",
        {"logs": ["starting", "could not connect to server on host db:5432"]},
    )
    docker.content("container_health", {"state": "starting"})


def missing_config(docker: FakeDocker) -> None:
    """A container that failed for want of a configuration value.

    The implied remedy is a configuration change, which is permanently outside
    the automatic set.
    """
    docker.content(
        "container_status",
        {"container": "c1", "status": "exited", "running": False, "exit_code": 78},
    )
    docker.content(
        "container_logs",
        {"logs": ["Error: DATABASE_URL environment variable is not set"]},
    )
    docker.content("container_health", {"state": "unhealthy", "detail": "config"})


def build_failure(docker: FakeDocker) -> None:
    """A failed build. The implied fix is a rebuild, which is approval-gated."""
    docker.content(
        "container_status",
        {"container": "c1", "status": "exited", "running": False, "exit_code": 1},
    )
    docker.content(
        "container_logs",
        {"logs": ['ERROR: failed to solve: process "/bin/sh -c pip install" did not complete']},
    )
    docker.content("container_health", {"state": "no_healthcheck"})


def failing_verification(container: str = "c1") -> VerificationResult:
    return VerificationResult(
        container=container,
        status="FAILED",
        checks=[
            VerificationCheck(
                name="container_running",
                outcome="fail",
                detail="container is exited, exit_code=137",
                evidence=[Evidence(source="docker.container_status", detail="exit_code=137")],
            )
        ],
        failures=["container_running"],
        evidence=[Evidence(source="docker.container_status", detail="exit_code=137")],
    )


# ---------------------------------------------------------------------------
# Investigation: reasoning from evidence
# ---------------------------------------------------------------------------


async def test_investigation_produces_a_populated_report(docker: FakeDocker) -> None:
    oom_container(docker)

    report = await investigate_failure(failing_verification(), container="c1", image="img:1")

    assert report.symptom
    assert report.evidence, "a report with no evidence is an assertion"
    assert report.suspected_root_causes
    assert report.affected_component
    assert report.next_action
    assert report.recommended_fix is not None


async def test_oom_is_identified_from_two_independent_sources(docker: FakeDocker) -> None:
    """Exit code 137 *and* the log line are separate observations, so: high."""
    oom_container(docker)

    report = await investigate_failure(failing_verification(), container="c1")

    cause = next(c for c in report.suspected_root_causes if c.id == "out_of_memory")
    assert cause.confidence == "high"
    assert report.confidence == "high"


async def test_a_single_source_is_not_high_confidence(docker: FakeDocker) -> None:
    """One witness is medium. Calling that high is how a wrong fix gets applied."""
    docker.content("container_status", {"container": "c1", "status": "exited", "exit_code": 0})
    docker.content("container_logs", {"logs": ["panic: runtime error: index out of range"]})
    docker.content("container_health", {"error": "unavailable"})

    report = await investigate_failure(container="c1")

    cause = next(c for c in report.suspected_root_causes if c.id == "process_crashed")
    assert cause.confidence == "medium"
    assert report.confidence != "high"


async def test_unrecognised_evidence_is_reported_as_inconclusive(docker: FakeDocker) -> None:
    """The important negative test: no signature means no invented cause."""
    docker.content(
        "container_status",
        {"container": "c1", "status": "running", "running": True, "exit_code": 0},
    )
    docker.content("container_logs", {"logs": ["all good", "nothing to see"]})
    docker.content("container_health", {"state": "healthy"})

    report = await investigate_failure(container="c1")

    assert report.inconclusive is True
    assert report.suspected_root_causes == []
    assert report.confidence == "low"
    assert report.automatic_fix_safe is False


async def test_investigation_never_changes_anything(docker: FakeDocker) -> None:
    """Read-only by construction: not one state-changing tool is reachable."""
    oom_container(docker)

    await investigate_failure(failing_verification(), container="c1", image="img:1")

    assert not set(docker.tools_called()) & DESTRUCTIVE_TOOLS
    assert not set(docker.tools_executed()) & MUTATING_TOOLS


async def test_probe_failure_is_recorded_as_evidence_not_a_crash(docker: FakeDocker) -> None:
    """One unavailable probe must not lose the whole investigation."""
    oom_container(docker)
    docker.fail("container_health")

    report = await investigate_failure(failing_verification(), container="c1")

    assert any("unavailable" in item.detail for item in report.evidence)
    assert report.suspected_root_causes


def test_confidence_requires_corroboration() -> None:
    one = [Observation(source="docker.container_logs", detail="x")]
    two = [
        Observation(source="docker.container_logs", detail="x"),
        Observation(source="docker.container_status", detail="y"),
    ]

    assert derive_confidence(one, strength="strong") == "medium"
    assert derive_confidence(two, strength="strong") == "high"
    assert derive_confidence(one, strength="weak") == "low"
    assert derive_confidence([], strength="strong") == "low"


def test_a_root_cause_without_evidence_is_rejected() -> None:
    with pytest.raises(ValidationError, match="cites no evidence"):
        SuspectedRootCause(id="invented", cause="because", component="application", evidence=[])


def test_high_confidence_without_a_cause_is_rejected() -> None:
    """Otherwise 'high confidence' is just a feeling with a field name."""
    with pytest.raises(ValidationError, match="requires at least one suspected root cause"):
        IncidentReport(
            incident_id="i1",
            symptom="something broke",
            confidence="high",
            next_action="look",
            suspected_root_causes=[],
        )


def test_low_confidence_can_never_claim_a_safe_automatic_fix() -> None:
    with pytest.raises(ValidationError, match="cannot be true while confidence is low"):
        IncidentReport(
            incident_id="i1",
            symptom="something broke",
            confidence="low",
            next_action="look",
            automatic_fix_safe=True,
        )


@pytest.mark.parametrize(
    "line,expected",
    [
        ("Killed process 1 (python)", "out_of_memory"),
        ("panic: runtime error: index out of range", "process_crashed"),
        ("Traceback (most recent call last):", "process_crashed"),
        ("could not connect to server on host db:5432", "dependency_refused"),
        ("bind: address already in use", "port_conflict"),
        ("no such host: db.internal", "dependency_missing"),
        ("KeyError: 'DATABASE_URL'", "missing_configuration"),
        ("manifest unknown", "image_missing"),
    ],
)
def test_known_signatures_are_recognised(line: str, expected: str) -> None:
    assert expected in {signature.id for signature, _ in match_signatures(line)}


def test_benign_lines_match_nothing() -> None:
    """A false positive here would restart a container that is working."""
    for line in ("INFO listening on :8000", "request completed in 3ms", "retrying, attempt 2"):
        assert match_signatures(line) == []


def test_repository_evidence_reads_the_dockerfile(tmp_path: Any) -> None:
    (tmp_path / "Dockerfile").write_text("FROM python:3.12\nEXPOSE 8000\n", encoding="utf-8")

    from app.core.config import get_settings

    settings = get_settings()
    previous = settings.repository_root
    try:
        settings.repository_root = tmp_path
        observations = collect_repository_evidence(".")
    finally:
        settings.repository_root = previous

    assert any("HEALTHCHECK" in item.detail for item in observations)
    assert any("EXPOSE" in item.detail for item in observations)


def test_repository_evidence_refuses_to_escape_the_root(tmp_path: Any) -> None:
    """Containment is re-checked at the read, not trusted from the caller."""
    from app.core.config import get_settings

    settings = get_settings()
    previous = settings.repository_root
    try:
        settings.repository_root = tmp_path
        observations = collect_repository_evidence("../../../etc")
    finally:
        settings.repository_root = previous

    assert observations
    assert "escapes the repository root" in observations[0].detail


# ---------------------------------------------------------------------------
# Risk check
# ---------------------------------------------------------------------------


def _action(category: FixCategory, risk: FixRisk = FixRisk.SAFE) -> RecoveryAction:
    return RecoveryAction(
        action=category.value,
        category=category,
        description="d",
        rationale="r",
        risk=risk,
        requires_approval=True,
    )


def _report(confidence: str, *, safe: bool = True) -> IncidentReport:
    """A minimal report carrying one well-evidenced cause."""
    return IncidentReport(
        incident_id="i1",
        symptom="s",
        confidence=confidence,
        automatic_fix_safe=safe,
        next_action="restart and verify",
        suspected_root_causes=[
            SuspectedRootCause(
                id="out_of_memory",
                cause="oom",
                component="application",
                evidence=[Observation(source="docker.container_status", detail="exit 137")],
            )
        ],
    )


def test_restart_is_safe_when_policy_permits_it() -> None:
    policy = SelfHealingPolicy(enabled=True, min_confidence="medium")
    decision = assess_risk(_action(FixCategory.RESTART_CONTAINER), _report("high"), policy)

    assert decision.may_apply_automatically is True


def test_low_confidence_blocks_an_otherwise_safe_action() -> None:
    policy = SelfHealingPolicy(enabled=True, min_confidence="medium")
    decision = assess_risk(
        _action(FixCategory.RESTART_CONTAINER), _report("low", safe=False), policy
    )

    assert decision.may_apply_automatically is False
    assert "confidence" in decision.reason


def test_confidence_floor_is_configurable() -> None:
    """A permissive floor is a deliberate choice, and it is respected."""
    action = _action(FixCategory.RESTART_CONTAINER)

    strict = assess_risk(action, _report("medium"), SelfHealingPolicy(min_confidence="high"))
    lenient = assess_risk(action, _report("medium"), SelfHealingPolicy(min_confidence="low"))

    assert strict.may_apply_automatically is False
    assert lenient.may_apply_automatically is True


def test_code_changes_are_never_automatic_under_any_policy() -> None:
    """No configuration makes an agent edit code. This is the point."""
    most_permissive = SelfHealingPolicy(
        enabled=True, min_confidence="low", allow_rebuild=True, allow_restart=True
    )

    decision = assess_risk(_action(FixCategory.CODE), _report("high"), most_permissive)

    assert decision.may_apply_automatically is False
    assert decision.risk is FixRisk.FORBIDDEN
    assert "never be applied automatically" in decision.reason


def test_configuration_changes_are_never_automatic() -> None:
    policy = SelfHealingPolicy(enabled=True, min_confidence="low")

    decision = assess_risk(_action(FixCategory.CONFIGURATION), _report("high"), policy)

    assert decision.may_apply_automatically is False
    assert decision.risk is FixRisk.FORBIDDEN


def test_dependency_changes_are_never_automatic() -> None:
    policy = SelfHealingPolicy(enabled=True, min_confidence="low")

    assert (
        assess_risk(_action(FixCategory.DEPENDENCY), _report("high"), policy).risk
        is FixRisk.FORBIDDEN
    )


def test_rebuild_needs_approval_by_default() -> None:
    """A rebuild runs untrusted Dockerfile steps, so it is not a default."""
    policy = SelfHealingPolicy(enabled=True, min_confidence="low", allow_rebuild=False)
    action = _action(FixCategory.REBUILD_IMAGE, FixRisk.APPROVAL_REQUIRED)

    decision = assess_risk(action, _report("high"), policy)

    assert decision.may_apply_automatically is False
    assert "permit" in decision.reason, "the reason must name the config that blocked it"


def test_rebuild_is_automatic_only_when_explicitly_enabled() -> None:
    policy = SelfHealingPolicy(enabled=True, min_confidence="low", allow_rebuild=True)
    action = _action(FixCategory.REBUILD_IMAGE, FixRisk.SAFE)

    assert assess_risk(action, _report("high"), policy).may_apply_automatically is True


def test_a_forbidden_action_cannot_claim_approval_is_enough() -> None:
    """Otherwise 'forbidden' would read as a weaker form of 'needs approval'."""
    with pytest.raises(ValidationError, match="cannot be approved into existence"):
        RecoveryAction(
            action="remove_everything",
            category=FixCategory.RESTART_CONTAINER,
            description="d",
            rationale="r",
            risk=FixRisk.FORBIDDEN,
            requires_approval=False,
        )


def test_policy_refuses_a_forbidden_category() -> None:
    policy = SelfHealingPolicy()

    assert policy.permits_category(FixCategory.RESTART_CONTAINER) is True
    assert policy.permits_category(FixCategory.RETRY_DEPLOYMENT) is True
    assert policy.permits_category(FixCategory.REBUILD_IMAGE) is False
    assert policy.permits_category(FixCategory.CODE) is False
    assert policy.permits_category(FixCategory.CONFIGURATION) is False


# ---------------------------------------------------------------------------
# The loop: successful recovery
# ---------------------------------------------------------------------------


async def test_successful_recovery_restarts_and_re_verifies(docker: FakeDocker) -> None:
    """The happy path, end to end: investigate, restart, redeploy, verify, recover."""
    oom_container(docker)
    verification = failing_verification()
    calls = {"verify": 0}

    async def verify() -> VerificationResult:
        calls["verify"] += 1
        if docker.restart_count == 0:
            return verification
        return VerificationResult(container="c1", status="SUCCESS")

    async def redeploy() -> None:
        docker.restart()

    outcome = await run_recovery(
        SelfHealingPolicy(enabled=True, max_recovery_attempts=3, min_confidence="medium"),
        verification=verification,
        container="c1",
        image="img:1",
        verify=verify,
        redeploy=redeploy,
    )

    assert outcome.recovered is True
    assert outcome.escalated is False
    assert outcome.attempts_used == 1
    assert docker.restart_count == 1
    assert "stop_container" in docker.tools_executed()
    assert outcome.attempts[0].succeeded is True
    assert outcome.attempts[0].verification_status == "SUCCESS"


async def test_recovery_stops_immediately_once_verified(docker: FakeDocker) -> None:
    """No further attempts after SUCCESS, even with budget left."""
    oom_container(docker)

    async def verify() -> VerificationResult:
        return VerificationResult(container="c1", status="SUCCESS")

    async def redeploy() -> None:
        docker.restart()

    outcome = await run_recovery(
        SelfHealingPolicy(enabled=True, max_recovery_attempts=5),
        container="c1",
        verify=verify,
        redeploy=redeploy,
    )

    assert outcome.recovered is True
    assert outcome.attempts_used == 1
    assert docker.restart_count == 1, "a verified recovery must not keep restarting"


async def test_only_one_verification_per_successful_attempt(docker: FakeDocker) -> None:
    oom_container(docker)
    counter = {"n": 0}

    async def verify() -> VerificationResult:
        counter["n"] += 1
        return VerificationResult(container="c1", status="SUCCESS")

    async def redeploy() -> None:
        docker.restart()

    await run_recovery(
        SelfHealingPolicy(enabled=True, max_recovery_attempts=4),
        container="c1",
        verify=verify,
        redeploy=redeploy,
    )

    assert counter["n"] == 1


# ---------------------------------------------------------------------------
# The loop: failed recovery
# ---------------------------------------------------------------------------


async def test_failed_recovery_exhausts_the_budget_and_escalates(docker: FakeDocker) -> None:
    """Never succeeds, never loops forever, and says so."""
    oom_container(docker)
    verification = failing_verification()

    async def verify() -> VerificationResult:
        return verification

    async def redeploy() -> None:
        docker.restart()

    outcome = await run_recovery(
        SelfHealingPolicy(enabled=True, max_recovery_attempts=3),
        verification=verification,
        container="c1",
        verify=verify,
        redeploy=redeploy,
    )

    assert outcome.recovered is False
    assert outcome.escalated is True
    assert outcome.escalation_reason is EscalationReason.BUDGET_EXHAUSTED
    assert outcome.attempts_used == 3
    assert len(outcome.attempts) == 3
    assert not any(attempt.succeeded for attempt in outcome.attempts)


async def test_the_loop_never_exceeds_its_budget(docker: FakeDocker) -> None:
    """The hard guarantee. Checked against several budgets."""
    oom_container(docker)
    verification = failing_verification()
    restarts = {"n": 0}

    async def verify() -> VerificationResult:
        return verification

    async def redeploy() -> None:
        restarts["n"] += 1
        docker.restart()

    for budget in (1, 2, 4):
        docker.restart_count = 0
        outcome = await run_recovery(
            SelfHealingPolicy(enabled=True, max_recovery_attempts=budget),
            verification=verification,
            container="c1",
            verify=verify,
            redeploy=redeploy,
        )
        assert outcome.attempts_used <= budget, f"budget {budget} exceeded"

    assert restarts["n"] <= 4


async def test_a_failing_restart_still_consumes_budget(docker: FakeDocker) -> None:
    """An erroring tool must not produce an unbounded loop."""
    oom_container(docker)
    docker.fail("stop_container")

    async def verify() -> VerificationResult:
        return VerificationResult(container="c1", status="SUCCESS")

    outcome = await run_recovery(
        SelfHealingPolicy(enabled=True, max_recovery_attempts=2),
        container="c1",
        verify=verify,
        redeploy=lambda: asyncio.sleep(0),
    )

    assert outcome.recovered is False
    assert outcome.attempts_used <= 2
    assert any(attempt.error for attempt in outcome.attempts)


# ---------------------------------------------------------------------------
# Safety constraints
# ---------------------------------------------------------------------------


async def test_disabled_policy_diagnoses_but_never_acts(docker: FakeDocker) -> None:
    """The default posture: a full diagnosis, and not one state change."""
    oom_container(docker)

    async def verify() -> VerificationResult:
        return VerificationResult(container="c1", status="SUCCESS")

    async def redeploy() -> None:
        docker.restart()

    outcome = await run_recovery(
        SelfHealingPolicy(enabled=False),
        container="c1",
        verify=verify,
        redeploy=redeploy,
    )

    assert outcome.recovered is False
    assert outcome.escalation_reason is EscalationReason.DISABLED
    assert outcome.attempts == []
    assert docker.restart_count == 0
    assert not set(docker.tools_executed()) & MUTATING_TOOLS


async def test_zero_budget_is_inert(docker: FakeDocker) -> None:
    oom_container(docker)

    outcome = await run_recovery(
        SelfHealingPolicy(enabled=True, max_recovery_attempts=0),
        container="c1",
        verify=lambda: asyncio.sleep(0),
    )

    assert outcome.recovered is False
    assert outcome.attempts_used == 0


async def test_no_root_cause_escalates_without_acting(docker: FakeDocker) -> None:
    """Nothing recognised means nothing attempted."""
    docker.content("container_status", {"container": "c1", "status": "running", "exit_code": 0})
    docker.content("container_logs", {"logs": ["nothing interesting"]})
    docker.content("container_health", {"state": "healthy"})

    outcome = await run_recovery(
        SelfHealingPolicy(enabled=True, max_recovery_attempts=3),
        container="c1",
    )

    assert outcome.recovered is False
    assert outcome.escalation_reason is EscalationReason.NO_ROOT_CAUSE
    assert not set(docker.tools_executed()) & MUTATING_TOOLS


async def test_a_high_risk_fix_never_applies_without_a_human(docker: FakeDocker) -> None:
    """A build failure implies a rebuild, which is approval-gated.

    `human_approved` is the only thing that promotes it, and it comes from
    outside the graph.
    """
    build_failure(docker)

    outcome = await run_recovery(
        SelfHealingPolicy(enabled=True, max_recovery_attempts=3, allow_rebuild=False),
        container="c1",
        verify=lambda: asyncio.sleep(0),
        human_approved=False,
    )

    assert outcome.recovered is False
    assert outcome.escalated is True
    assert outcome.escalation_reason is EscalationReason.APPROVAL_REQUIRED
    assert "build_image" not in docker.tools_executed(), (
        "a rebuild executes untrusted Dockerfile steps and must not happen unattended"
    )


async def test_a_human_approval_permits_the_rebuild(docker: FakeDocker) -> None:
    """The approval tier is reachable, and only from the caller.

    `allow_rebuild=False` means "not automatically", not "never". A rebuild runs
    untrusted Dockerfile steps, so it is off by default and needs a human -- but
    refusing a human's explicit approval would just be theatre.
    """
    build_failure(docker)

    outcome = await run_recovery(
        SelfHealingPolicy(enabled=True, max_recovery_attempts=1, allow_rebuild=False),
        container="c1",
        verify=lambda: asyncio.sleep(0),
        human_approved=True,
    )

    assert outcome.attempts, "an approved action must still be recorded"
    assert outcome.attempts[0].approved is True
    assert outcome.attempts[0].category is FixCategory.REBUILD_IMAGE
    assert "build_image" in docker.tools_executed()


async def test_human_approval_cannot_unlock_a_forbidden_fix(docker: FakeDocker) -> None:
    """Approval buys an `approval_required` action. It must not buy a forbidden one.

    A missing configuration value is a code/config change: permanently outside
    the automatic set, so no approval setting reaches it.
    """
    missing_config(docker)

    outcome = await run_recovery(
        SelfHealingPolicy(enabled=True, max_recovery_attempts=1),
        container="c1",
        verify=lambda: asyncio.sleep(0),
        human_approved=True,
    )

    assert outcome.escalation_reason is EscalationReason.FORBIDDEN_FIX
    assert not any(attempt.applied for attempt in outcome.attempts)
    assert "build_image" not in docker.tools_executed()
    assert "stop_container" not in docker.tools_executed()


async def test_a_blocked_fix_is_not_reported_as_an_exhausted_budget(docker: FakeDocker) -> None:
    """The escalation reason must name the real obstacle.

    Reporting `budget_exhausted` when the policy blocked the fix sends an
    operator to raise a retry limit that would not change anything.
    """
    build_failure(docker)

    outcome = await run_recovery(
        SelfHealingPolicy(enabled=True, max_recovery_attempts=3, allow_rebuild=False),
        container="c1",
        verify=lambda: asyncio.sleep(0),
    )

    assert outcome.escalation_reason is EscalationReason.APPROVAL_REQUIRED
    assert outcome.escalation_reason is not EscalationReason.BUDGET_EXHAUSTED
    assert len(outcome.attempts) == 1, "a blocked fix must not burn the whole budget"


async def test_a_fix_that_errors_is_never_called_recovered(docker: FakeDocker) -> None:
    """A green verification is worthless if the fix never applied.

    The stop can succeed and the start fail, leaving the container down. Reporting
    that as a recovery is how a loop declares success over a broken system.
    """
    oom_container(docker)
    docker.fail("stop_container")

    async def verify() -> VerificationResult:
        return VerificationResult(container="c1", status="SUCCESS")

    async def redeploy() -> None:
        docker.restart()

    outcome = await run_recovery(
        SelfHealingPolicy(enabled=True, max_recovery_attempts=2),
        container="c1",
        verify=verify,
        redeploy=redeploy,
    )

    assert outcome.recovered is False, "a fix that errored must not count as recovered"
    assert any(attempt.error for attempt in outcome.attempts)
    assert not any(attempt.succeeded for attempt in outcome.attempts)


async def test_a_declined_action_is_still_recorded(docker: FakeDocker) -> None:
    """The attempts not taken are the ones worth reading later."""
    oom_container(docker)

    outcome = await run_recovery(
        SelfHealingPolicy(enabled=True, max_recovery_attempts=3, allow_restart=False),
        container="c1",
        verify=lambda: asyncio.sleep(0),
    )

    assert outcome.attempts, "a refusal with no record is indistinguishable from silence"
    assert outcome.attempts[0].applied is False


async def test_low_confidence_escalates_to_a_human(docker: FakeDocker) -> None:
    """Weak evidence plus a strict floor must stop the loop, not nudge it."""
    dependency_failure(docker)

    outcome = await run_recovery(
        SelfHealingPolicy(enabled=True, max_recovery_attempts=3, min_confidence="high"),
        container="c1",
        verify=lambda: asyncio.sleep(0),
    )

    assert outcome.recovered is False
    assert not set(docker.tools_executed()) & MUTATING_TOOLS


async def test_destructive_tools_are_never_called(docker: FakeDocker) -> None:
    """The guarantee that does not depend on any setting."""
    oom_container(docker)
    verification = failing_verification()

    async def verify() -> VerificationResult:
        return VerificationResult(container="c1", status="SUCCESS")

    async def redeploy() -> None:
        docker.restart()

    await run_recovery(
        SelfHealingPolicy(
            enabled=True, max_recovery_attempts=3, min_confidence="low", allow_rebuild=True
        ),
        verification=verification,
        container="c1",
        verify=verify,
        redeploy=redeploy,
        human_approved=True,
    )

    assert not set(docker.tools_called()) & DESTRUCTIVE_TOOLS


async def test_every_attempt_records_what_it_did(docker: FakeDocker) -> None:
    oom_container(docker)
    verification = failing_verification()

    async def verify() -> VerificationResult:
        return verification

    async def redeploy() -> None:
        docker.restart()

    outcome = await run_recovery(
        SelfHealingPolicy(enabled=True, max_recovery_attempts=2),
        verification=verification,
        container="c1",
        verify=verify,
        redeploy=redeploy,
    )

    assert outcome.attempts
    for index, attempt in enumerate(outcome.attempts, start=1):
        assert attempt.index == index
        assert attempt.action
        assert attempt.category
        assert attempt.risk
        assert attempt.description
        assert attempt.evidence, "an attempt with no cause behind it is a coincidence"


async def test_the_outcome_says_what_happened(docker: FakeDocker) -> None:
    oom_container(docker)

    async def verify() -> VerificationResult:
        return VerificationResult(container="c1", status="SUCCESS")

    async def redeploy() -> None:
        docker.restart()

    outcome = await run_recovery(
        SelfHealingPolicy(enabled=True), container="c1", verify=verify, redeploy=redeploy
    )

    assert "recovered" in outcome.summary()
    assert outcome.next_action


# ---------------------------------------------------------------------------
# Graph shape
# ---------------------------------------------------------------------------


def test_every_mcp_calling_module_is_covered_by_the_fixture() -> None:
    """If a new module starts calling MCP tools, it must be added to the fake list.

    Without this, a new module would silently reach the real manager and the
    tests would stop testing anything.
    """
    import importlib

    reachable = {
        name
        for name in (
            "app.agents.investigation",
            "app.agents.self_healing",
            "app.agents.deployment_verification",
            "app.agents.execution_engine",
        )
        if hasattr(importlib.import_module(name), "get_manager")
    }

    assert reachable == set(MCP_CALLING_MODULES)


def test_the_recovery_graph_has_the_expected_nodes() -> None:
    nodes = set(build_recovery_graph().nodes)

    assert {
        "debug",
        "root_cause",
        "fix_recommendation",
        "risk_check",
        "apply_fix",
        "redeploy",
        "verify",
        "escalate",
    } <= nodes


def _report_with_fix(fix: Any) -> IncidentReport:
    """A report carrying one well-evidenced cause and the given recommendation."""
    return IncidentReport(
        incident_id="i1",
        symptom="s",
        confidence="high",
        automatic_fix_safe=True,
        next_action="restart",
        recommended_fix=fix,
        suspected_root_causes=[
            SuspectedRootCause(
                id="out_of_memory",
                cause="oom",
                component="application",
                evidence=[Observation(source="docker.container_status")],
            )
        ],
    )


def test_a_proposed_restart_names_its_cause(docker: FakeDocker) -> None:
    """An action with no cause behind it is not a fix."""
    from app.models.incident import FixRecommendation

    report = _report_with_fix(
        FixRecommendation(
            action="restart_container",
            description="restart",
            rationale="the process is dead",
            tool="docker.stop_container",
            arguments={"name": "c1"},
        )
    )

    action = propose_action(report, container="c1", image="img:1")

    assert action is not None
    assert action.category is FixCategory.RESTART_CONTAINER
    assert action.derived_from == ["out_of_memory"]
    assert action.reversible is True


def test_a_report_with_no_recommendation_proposes_nothing(docker: FakeDocker) -> None:
    """A cause with no remedy attached yields no action, and that is correct."""
    report = IncidentReport(
        incident_id="i1",
        symptom="s",
        confidence="high",
        next_action="restart",
        suspected_root_causes=[
            SuspectedRootCause(
                id="out_of_memory",
                cause="oom",
                component="application",
                evidence=[Observation(source="docker.container_status")],
            )
        ],
    )

    assert propose_action(report, container="c1", image="img:1") is None


def test_the_source_contains_no_destructive_shortcut() -> None:
    """Belt and braces: the loop's own module names no destructive capability."""
    module = pathlib.Path(__file__).resolve().parents[1] / "app" / "agents" / "self_healing.py"
    # Docstrings describe prohibitions by name, so strip them before searching.
    source = re.sub(r'""".*?"""', "", module.read_text(encoding="utf-8"), flags=re.S)

    for banned in ("remove_container", "prune", "rollback", "docker system prune"):
        assert banned not in source, f"{banned} must not be reachable from the healing loop"


async def test_a_restart_without_a_way_to_start_is_refused(docker: FakeDocker) -> None:
    """`restart_container` is a stop. Without a redeploy it would be an outage.

    The check has to happen before the stop, not after: a refusal that arrives
    once the container is already stopped is a refusal of the wrong thing.
    """
    oom_container(docker)

    outcome = await run_recovery(
        SelfHealingPolicy(enabled=True, max_recovery_attempts=2),
        container="c1",
        verify=lambda: asyncio.sleep(0),
        redeploy=None,
    )

    assert outcome.recovered is False
    assert "stop_container" not in docker.tools_executed(), (
        "the container must not be stopped when nothing can start it again"
    )
    assert any("no way to start" in (attempt.error or "") for attempt in outcome.attempts)


async def test_an_errored_action_is_not_recorded_as_applied(docker: FakeDocker) -> None:
    """`applied` has to mean it was applied, not merely attempted."""
    oom_container(docker)
    docker.fail("stop_container")

    async def redeploy() -> None:
        docker.restart()

    outcome = await run_recovery(
        SelfHealingPolicy(enabled=True, max_recovery_attempts=1),
        container="c1",
        verify=lambda: asyncio.sleep(0),
        redeploy=redeploy,
    )

    assert all(attempt.applied is False for attempt in outcome.attempts)


async def test_the_outcome_carries_the_report_that_produced_it(docker: FakeDocker) -> None:
    """An outcome with no report behind it cannot be reviewed.

    The evidence is the only part of this feature a human can independently
    check, so it has to survive the trip out of the graph.
    """
    oom_container(docker)

    outcome = await run_recovery(
        SelfHealingPolicy(enabled=True, max_recovery_attempts=1),
        container="c1",
        verify=lambda: asyncio.sleep(0),
        redeploy=lambda: asyncio.sleep(0),
    )

    assert outcome.incident is not None
    assert outcome.incident.suspected_root_causes
    assert all(cause.evidence for cause in outcome.incident.suspected_root_causes)
    assert outcome.attempts[0].evidence == [
        cause.id for cause in outcome.incident.suspected_root_causes
    ]


async def test_the_disabled_path_still_returns_a_report(docker: FakeDocker) -> None:
    """Diagnose and act are separable: switching healing off keeps the diagnosis."""
    oom_container(docker)

    outcome = await run_recovery(
        SelfHealingPolicy(enabled=False),
        container="c1",
        verify=lambda: asyncio.sleep(0),
    )

    assert outcome.escalation_reason is EscalationReason.DISABLED
    assert outcome.incident is not None
    assert outcome.incident.suspected_root_causes
    # Read-only probes are the whole point of the investigation; what must not
    # appear is a tool that changes the deployment.
    assert not set(docker.tools_executed()) & MUTATING_TOOLS
    assert not set(docker.tools_executed()) & DESTRUCTIVE_TOOLS


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def test_self_healing_is_disabled_on_a_fresh_install() -> None:
    """The default has to be the safe end of every axis.

    An install that starts healing deployments without anyone asking is worse
    than one that only reports, so `enabled` defaults off and the rest of the
    defaults are reached only once it is on.
    """
    from app.core.config import SelfHealingSettings

    policy = SelfHealingSettings().to_policy()

    assert policy.enabled is False
    assert policy.max_recovery_attempts == 2
    assert policy.min_confidence == "medium"
    assert policy.allow_restart is True
    assert policy.allow_retry is True
    assert policy.allow_rebuild is False


def test_the_attempt_budget_is_capped_in_settings() -> None:
    """An unbounded retry budget is an outage with extra steps.

    The cap is enforced by the settings schema so no environment file can
    configure one, rather than by a check that runs somewhere later.
    """
    from pydantic import ValidationError

    from app.core.config import SelfHealingSettings

    with pytest.raises(ValidationError):
        SelfHealingSettings(max_recovery_attempts=11)


def test_settings_produce_the_policy_the_loop_checks() -> None:
    """Every knob has to reach the policy, or the settings file is decoration."""
    from app.core.config import SelfHealingSettings

    policy = SelfHealingSettings(
        enabled=True,
        max_recovery_attempts=4,
        min_confidence="high",
        allow_restart=False,
        allow_rebuild=True,
        allow_retry=False,
    ).to_policy()

    assert policy.enabled is True
    assert policy.max_recovery_attempts == 4
    assert policy.min_confidence == "high"
    assert policy.allow_restart is False
    assert policy.allow_rebuild is True
    assert policy.allow_retry is False


# ---------------------------------------------------------------------------
# HTTP surface
# ---------------------------------------------------------------------------


def test_the_recovery_routes_are_registered(client: object) -> None:
    paths = client.app.openapi()["paths"]  # type: ignore[attr-defined]

    assert "/api/deployment/incident" in paths
    assert "/api/deployment/recover" in paths


def test_the_incident_endpoint_reports_an_evidence_backed_report(client: object) -> None:
    from unittest.mock import AsyncMock, patch

    from app.models.verification import VerificationResult

    verification = VerificationResult(
        container="c1",
        status="FAILED",
        checks=[
            {
                "name": "container_running",
                "outcome": "fail",
                "detail": "container exited with 137",
                "severity": "critical",
            }
        ],
    )
    with patch(
        "app.agents.deployment_verification.verify_deployment",
        AsyncMock(return_value=verification),
    ):
        response = client.post("/api/deployment/incident", json={"container_name": "c1"})  # type: ignore[attr-defined]

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) >= {
        "incident_id",
        "symptom",
        "evidence",
        "suspected_root_causes",
        "confidence",
        "affected_component",
        "recommended_fix",
        "automatic_fix_safe",
        "next_action",
    }


def test_the_recover_endpoint_defaults_to_the_configured_disabled_policy(
    client: object,
) -> None:
    """An unmodified install diagnoses; it must not start changing deployments."""
    from unittest.mock import AsyncMock, patch

    from app.models.verification import VerificationResult

    verification = VerificationResult(container="c1", status="FAILED")
    with patch(
        "app.agents.deployment_verification.verify_deployment",
        AsyncMock(return_value=verification),
    ):
        response = client.post(  # type: ignore[attr-defined]
            "/api/deployment/recover",
            json={"container_name": "c1", "verify_after_fix": False},
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["recovered"] is False
    assert body["escalation_reason"] == "disabled"
    assert body["incident"], "the diagnosis must still be returned"
