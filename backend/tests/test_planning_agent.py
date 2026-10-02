"""Tests for the planning graph and the agent planning endpoint.

Covers three things: the graph's shape, that each node does what it claims, and
that the endpoint returns the documented contract. No real LLM or tool is
involved, so the whole file runs offline and deterministically.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError as PydanticValidationError

from app.agents.planner import build_planning_graph, plan_validator, planner, planning_graph
from app.models.planning import (
    DeploymentPlan,
    PlanningState,
    PlanResponse,
    PlanStep,
    RequestAnalysis,
)
from app.services import llm as llm_service

EXAMPLE_REQUEST = "Deploy my Node.js application from the main branch."


def run(request: str) -> PlanningState:
    """Invoke the graph the same way the API does."""
    return planning_graph.invoke({"request": request})


# --------------------------------------------------------------------------
# Graph structure
# --------------------------------------------------------------------------


def test_graph_contains_the_required_nodes() -> None:
    nodes = set(planning_graph.get_graph().nodes)

    assert {"request_analyzer", "planner", "plan_validator"} <= nodes
    assert "__start__" in nodes
    assert "__end__" in nodes


def test_graph_nodes_are_wired_in_order() -> None:
    edges = {(edge.source, edge.target) for edge in planning_graph.get_graph().edges}

    assert (
        "__start__",
        "request_analyzer",
    ) in edges
    assert ("request_analyzer", "planner") in edges
    assert ("planner", "plan_validator") in edges
    assert ("plan_validator", "__end__") in edges


def test_graph_runs_end_to_end_and_populates_every_state_key() -> None:
    state = run(EXAMPLE_REQUEST)

    assert set(state) == {"request", "analysis", "plan", "validation"}
    assert state["request"] == EXAMPLE_REQUEST


def test_each_build_returns_an_independent_graph() -> None:
    assert build_planning_graph() is not planning_graph


# --------------------------------------------------------------------------
# request_analyzer
# --------------------------------------------------------------------------


def test_analyzer_extracts_task_type_runtime_and_branch() -> None:
    analysis = run(EXAMPLE_REQUEST)["analysis"]

    assert analysis.task_type == "deploy"
    assert analysis.runtime == "nodejs"
    assert analysis.branch == "main"


def test_analyzer_does_not_mistake_an_environment_for_a_branch() -> None:
    analysis = run("Roll back the Python service on production")["analysis"]

    assert analysis.branch is None
    assert analysis.environment == "production"


def test_analyzer_flags_an_unrecognisable_request() -> None:
    analysis = run("Do something vaguely administrative")["analysis"]

    assert analysis.task_type == "unknown"
    assert analysis.confidence < 0.5
    assert analysis.notes


def test_analyzer_notes_missing_details() -> None:
    analysis = run("Deploy it")["analysis"]

    assert analysis.environment is None
    assert analysis.branch is None
    assert analysis.notes


# --------------------------------------------------------------------------
# planner
# --------------------------------------------------------------------------


def test_planner_returns_every_required_field() -> None:
    plan = run(EXAMPLE_REQUEST)["plan"]

    assert plan.objective
    assert plan.task_type == "deploy"
    assert plan.assumptions
    assert plan.required_tools
    assert plan.steps
    assert plan.risk_level in {"low", "medium", "high", "critical"}
    assert isinstance(plan.requires_human_approval, bool)
    assert plan.expected_verification


def test_planner_orders_steps_from_one() -> None:
    plan = run(EXAMPLE_REQUEST)["plan"]

    assert [step.order for step in plan.steps] == list(range(1, len(plan.steps) + 1))


def test_planner_only_names_tools_and_never_calls_them() -> None:
    plan = run(EXAMPLE_REQUEST)["plan"]

    # Tools are declarative names for a later execution stage, not callables.
    for step in plan.steps:
        assert step.tool is None or isinstance(step.tool, str)


def test_planner_flags_a_deploy_as_needing_approval() -> None:
    plan = run(EXAMPLE_REQUEST)["plan"]

    assert plan.requires_human_approval is True
    assert any(step.requires_approval for step in plan.steps)


def test_planner_does_not_require_approval_for_a_read_only_request() -> None:
    plan = run("Monitor the logs for the frontend")["plan"]

    assert plan.requires_human_approval is False
    assert not any(step.requires_approval for step in plan.steps)


def test_planner_raises_risk_for_a_rollback() -> None:
    plan = run("Roll back the API to the previous release")["plan"]

    assert plan.risk_level == "high"
    assert plan.requires_human_approval is True


def test_planner_output_is_deterministic() -> None:
    first = run(EXAMPLE_REQUEST)["plan"]
    second = run(EXAMPLE_REQUEST)["plan"]

    assert first == second


def test_planner_templates_vary_by_task_type() -> None:
    deploy = run(EXAMPLE_REQUEST)["plan"]
    monitor = run("Monitor the logs for the frontend")["plan"]

    assert deploy.required_tools != monitor.required_tools


# --------------------------------------------------------------------------
# plan_validator
# --------------------------------------------------------------------------


def _valid_state() -> PlanningState:
    plan = DeploymentPlan(
        objective="Deploy the service",
        task_type="deploy",
        assumptions=["Assumed staging."],
        required_tools=["docker.build_image"],
        steps=[
            PlanStep(
                order=1,
                title="Build image",
                description="Build it.",
                tool="docker.build_image",
                requires_approval=True,
            )
        ],
        risk_level="medium",
        requires_human_approval=True,
        expected_verification=["Service responds."],
    )
    return {
        "request": "x",
        "analysis": RequestAnalysis(task_type="deploy", confidence=0.9),
        "plan": plan,
    }


def test_validator_accepts_a_sound_plan() -> None:
    state = _valid_state()

    result = plan_validator(state)

    assert result["validation"].is_valid is True
    assert result["validation"].issues == []


def test_validator_rejects_an_undeclared_tool() -> None:
    state = _valid_state()
    state["plan"] = state["plan"].model_copy(update={"required_tools": []})

    result = plan_validator(state)

    assert result["validation"].is_valid is False
    assert any("undeclared tool" in issue for issue in result["validation"].issues)


def test_validator_rejects_steps_that_are_not_ordered() -> None:
    state = _valid_state()
    state["plan"] = state["plan"].model_copy(
        update={"steps": [PlanStep(order=3, title="A", description="a")]}
    )

    result = plan_validator(state)

    assert result["validation"].is_valid is False
    assert any("not sequential" in issue for issue in result["validation"].issues)


def test_validator_rejects_a_high_risk_plan_without_approval() -> None:
    state = _valid_state()
    state["plan"] = state["plan"].model_copy(
        update={"risk_level": "high", "requires_human_approval": False}
    )

    result = plan_validator(state)

    assert result["validation"].is_valid is False
    assert any("human approval" in issue for issue in result["validation"].issues)


def test_validator_rejects_a_plan_with_no_verification() -> None:
    state = _valid_state()
    state["plan"] = state["plan"].model_copy(update={"expected_verification": []})

    result = plan_validator(state)

    assert result["validation"].is_valid is False
    assert any("verification" in issue for issue in result["validation"].issues)


def test_validator_does_not_mutate_the_input_state() -> None:
    state = _valid_state()

    plan_validator(state)

    assert "validation" not in state


# --------------------------------------------------------------------------
# Provider isolation
# --------------------------------------------------------------------------


class _StubLLM:
    """Stand-in for a real provider, proving the interface is the only seam."""

    def draft_plan(self, request: str, analysis: RequestAnalysis) -> DeploymentPlan:
        return DeploymentPlan(
            objective="STUB",
            task_type=analysis.task_type,
            required_tools=["stub.tool"],
            steps=[PlanStep(order=1, title="Stub step", description="From the stub.")],
            risk_level="low",
            requires_human_approval=False,
            expected_verification=["Stub check."],
        )


def test_planning_llm_interface_is_satisfied_by_a_new_implementation() -> None:
    stub: llm_service.PlanningLLM = _StubLLM()

    plan = stub.draft_plan("anything", RequestAnalysis(task_type="deploy", confidence=0.9))

    assert plan.objective == "STUB"


def test_planner_uses_the_llm_service_so_the_provider_can_be_swapped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(llm_service, "get_planning_llm", lambda: _StubLLM())
    monkeypatch.setattr("app.agents.planner.get_planning_llm", lambda: _StubLLM())

    plan = planner(
        {
            "request": EXAMPLE_REQUEST,
            "analysis": RequestAnalysis(task_type="deploy", confidence=0.9),
        }
    )["plan"]

    assert plan.objective == "STUB"


def test_default_llm_makes_no_network_call() -> None:
    # The shipped implementation is deterministic and local; a provider swap is
    # the only thing that should ever introduce outbound traffic here.
    assert isinstance(llm_service.get_planning_llm(), llm_service.HeuristicPlanningLLM)


# --------------------------------------------------------------------------
# POST /api/agent/plan
# --------------------------------------------------------------------------


def test_plan_endpoint_returns_a_structured_plan(client: TestClient) -> None:
    response = client.post("/api/agent/plan", json={"request": EXAMPLE_REQUEST})

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"plan", "analysis", "validation"}
    assert body["plan"]["objective"]
    assert body["plan"]["task_type"] == "deploy"
    assert body["plan"]["steps"]
    assert body["analysis"]["runtime"] == "nodejs"
    assert body["validation"]["is_valid"] is True


def test_plan_endpoint_includes_every_documented_plan_field(client: TestClient) -> None:
    response = client.post("/api/agent/plan", json={"request": EXAMPLE_REQUEST})

    plan = response.json()["plan"]
    for field in (
        "objective",
        "task_type",
        "assumptions",
        "required_tools",
        "steps",
        "risk_level",
        "requires_human_approval",
        "expected_verification",
    ):
        assert field in plan, f"missing {field}"


def test_plan_endpoint_step_shape(client: TestClient) -> None:
    response = client.post("/api/agent/plan", json={"request": EXAMPLE_REQUEST})

    step = response.json()["plan"]["steps"][0]
    assert set(step) >= {"order", "title", "description", "tool", "requires_approval"}


def test_plan_endpoint_rejects_an_empty_request(client: TestClient) -> None:
    response = client.post("/api/agent/plan", json={"request": ""})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_plan_endpoint_rejects_a_missing_request_field(client: TestClient) -> None:
    response = client.post("/api/agent/plan", json={})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_plan_endpoint_rejects_an_overlong_request(client: TestClient) -> None:
    response = client.post("/api/agent/plan", json={"request": "x" * 5000})

    assert response.status_code == 422


def test_plan_endpoint_handles_an_unknown_task_type(client: TestClient) -> None:
    response = client.post("/api/agent/plan", json={"request": "Do something vaguely odd"})

    assert response.status_code == 200
    assert response.json()["plan"]["task_type"] == "unknown"


def test_plan_endpoint_is_documented(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()

    assert "/api/agent/plan" in schema["paths"]
    assert "post" in schema["paths"]["/api/agent/plan"]


def test_plan_endpoint_emits_a_correlation_id(client: TestClient) -> None:
    response = client.post("/api/agent/plan", json={"request": EXAMPLE_REQUEST})

    assert response.headers.get("X-Request-ID")


def test_plan_endpoint_is_not_exposed_under_the_versioned_prefix(client: TestClient) -> None:
    # Agent routes are intentionally unversioned; this pins that decision.
    assert client.post("/api/v1/agent/plan", json={"request": EXAMPLE_REQUEST}).status_code == 404


def test_plan_response_model_matches_the_documented_schema() -> None:
    assert set(PlanResponse.model_fields) == {"plan", "analysis", "validation"}


def test_analysis_rejects_an_out_of_range_confidence() -> None:
    with pytest.raises(PydanticValidationError):
        RequestAnalysis(task_type="deploy", confidence=5.0)


def test_deployment_plan_requires_at_least_one_step() -> None:
    with pytest.raises(Exception):  # noqa: B017 - pydantic raises ValidationError
        DeploymentPlan(
            objective="x",
            task_type="deploy",
            steps=[],
            risk_level="low",
            requires_human_approval=False,
        )
