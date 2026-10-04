"""Deployment history endpoints and workflow recording.

These run against the app as it is actually served, so a route that is registered
but unreachable — or mounted at a prefix the frontend does not call — fails here
rather than in the browser.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.memory import DeploymentMemoryService, set_memory_service
from app.models.deployment_record import DeploymentRecord, DeploymentStatus
from app.models.verification import VerificationResult

pytestmark = pytest.mark.anyio


@pytest.fixture
def history() -> Iterator[Any]:
    """An in-memory history wired into the app the way the lifespan wires one."""
    from app.memory.in_memory import InMemoryMemoryStore

    store = InMemoryMemoryStore()
    set_memory_service(DeploymentMemoryService(store))
    yield store
    set_memory_service(None)


def _deployment(deployment_id: str, **overrides: object) -> DeploymentRecord:
    fields: dict[str, object] = {
        "deployment_id": deployment_id,
        "repository": "acme/api",
        "commit_sha": "b" * 40,
        "image": "acme/api:dev",
        "container": "acme-api",
        "status": DeploymentStatus.SUCCEEDED,
        "verification": VerificationResult(status="SUCCESS"),
    }
    fields.update(overrides)
    return DeploymentRecord.model_validate(fields)


async def test_history_starts_empty(client: TestClient) -> None:
    """An empty ledger is a valid answer, not an error."""
    response = client.get("/api/deployments")

    assert response.status_code == 200
    body = response.json()
    assert body == {"items": [], "total": 0, "limit": 50, "offset": 0}


async def test_history_lists_deployments(client: TestClient, history: Any) -> None:
    await history.save_deployment(_deployment("d1"))
    await history.save_deployment(_deployment("d2"))

    response = client.get("/api/deployments")

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert {item["deployment_id"] for item in body["items"]} == {"d1", "d2"}


async def test_a_history_row_is_a_summary(client: TestClient, history: Any) -> None:
    """The list must stay cheap: a plan in every row makes the list expensive."""
    from app.models.deployment_plan import (
        DetectedStack,
        RepositoryDeploymentPlan,
        RepositoryReference,
        Strategy,
    )

    plan = RepositoryDeploymentPlan(
        repository=RepositoryReference(owner="acme", name="api", full_name="acme/api", ref="main"),
        detected_stack=DetectedStack(primary_language="python", languages=["python"]),
        build_strategy=Strategy(approach="docker", summary="Build in Docker."),
        test_strategy=Strategy(approach="pytest", summary="Run pytest."),
        run_strategy=Strategy(approach="docker", summary="Run in Docker."),
        deployment_strategy=Strategy(approach="docker", summary="Deploy with Docker."),
        health_check_strategy=Strategy(approach="http", summary="Poll the health endpoint."),
        rollback_strategy=Strategy(approach="redeploy", summary="Redeploy the previous image."),
        blocked=False,
        rationale="fixture",
    )
    await history.save_deployment(_deployment("d1", plan=plan))

    row = client.get("/api/deployments").json()["items"][0]

    assert row["deployment_id"] == "d1"
    assert "plan" not in row
    assert "actions" not in row


async def test_the_limit_is_capped_by_the_api(client: TestClient) -> None:
    """The cap is part of the contract, not only of the model."""
    assert client.get("/api/deployments?limit=201").status_code == 422


async def test_a_bad_limit_is_rejected(client: TestClient) -> None:
    assert client.get("/api/deployments?limit=0").status_code == 422


async def test_history_is_filtered_by_status(client: TestClient, history: Any) -> None:
    await history.save_deployment(_deployment("d1"))
    await history.save_deployment(_deployment("d2", status=DeploymentStatus.FAILED))

    body = client.get("/api/deployments?status=failed").json()

    assert [item["deployment_id"] for item in body["items"]] == ["d2"]


async def test_history_is_filtered_by_repository(client: TestClient, history: Any) -> None:
    await history.save_deployment(_deployment("d1", repository="acme/api"))
    await history.save_deployment(_deployment("d2", repository="acme/other"))

    body = client.get("/api/deployments?repository=acme/api").json()

    assert [item["deployment_id"] for item in body["items"]] == ["d1"]


async def test_an_unknown_status_filter_is_rejected(client: TestClient) -> None:
    assert client.get("/api/deployments?status=fabulous").status_code == 422


async def test_a_trace_carries_the_whole_run(client: TestClient) -> None:
    """The detail view is the record of a real run, flattened as the run produced it."""
    from app.agents.deployment_workflow import run_deployment_workflow
    from app.memory.in_memory import InMemoryMemoryStore
    from app.memory.service import DeploymentMemoryService as Service
    from app.models.docker import DockerDeployRequest

    store = InMemoryMemoryStore()
    service = Service(store)
    set_memory_service(service)

    result = await run_deployment_workflow(
        DockerDeployRequest(
            repository_path="../examples/sample_app",
            image="acme/api:dev",
            container_name="acme-api",
            approve=True,
            approval_reference="tester",
            dry_run=True,
        )
    )

    body = client.get(f"/api/deployments/{result['deployment_id']}").json()

    assert body["deployment_id"] == result["deployment_id"]
    assert body["status"] == "dry_run"
    assert body["image"] == "acme/api:dev"
    assert body["tasks"], "the executed task list belongs in the trace"
    assert body["verification"] is None, "a dry run deploys nothing, so nothing was verified"
    assert "terminal" not in body, "terminal is computed, not stored"


async def test_a_trace_is_markdown_on_request(client: TestClient, history: Any) -> None:
    await history.save_deployment(_deployment("d1"))

    response = client.get("/api/deployments/d1?format=markdown")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "d1" in response.text
    assert "## Actions" in response.text


async def test_an_unknown_trace_is_a_404(client: TestClient) -> None:
    response = client.get("/api/deployments/nope")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


async def test_history_is_read_only(client: TestClient, history: Any) -> None:
    """History is evidence. An endpoint that can edit it cannot be trusted as it."""
    await history.save_deployment(_deployment("d1"))

    for method in ("POST", "PATCH", "PUT", "DELETE"):
        response = client.request(method, "/api/deployments/d1", json={"status": "succeeded"})
        assert response.status_code == 405, f"{method} must not be routable"


async def test_the_routes_are_advertised(client: TestClient) -> None:
    """A route nobody documents is a route nobody calls."""
    paths = client.get("/openapi.json").json()["paths"]

    assert "/api/deployments" in paths
    assert "/api/deployments/{deployment_id}" in paths


async def test_a_workflow_run_is_recorded_and_returned() -> None:
    """The end-to-end contract: running a workflow leaves a trace behind."""
    from app.agents.deployment_workflow import run_deployment_workflow
    from app.memory.in_memory import InMemoryMemoryStore
    from app.memory.service import DeploymentMemoryService as Service
    from app.models.deployment_record import DeploymentQuery
    from app.models.docker import DockerDeployRequest

    store = InMemoryMemoryStore()
    service = Service(store)
    set_memory_service(service)

    request = DockerDeployRequest(
        repository_path="../examples/sample_app",
        image="acme/api:dev",
        container_name="acme-api",
        approve=True,
        approval_reference="tester",
        dry_run=True,
    )
    result = await run_deployment_workflow(request)

    deployment_id = result["deployment_id"]
    assert deployment_id

    detail = await service.get_deployment(deployment_id)
    assert detail is not None, "the workflow must leave a retrievable trace"
    assert detail.status is DeploymentStatus.DRY_RUN
    assert detail.dry_run is True
    assert detail.tasks, "the executed task list belongs in the trace"

    listed = await service.list_deployments(DeploymentQuery())
    assert listed.total == 1
    assert listed.items[0].deployment_id == deployment_id


async def test_a_workflow_that_raises_still_leaves_a_trace() -> None:
    """A crash is history too — an interrupted run must be visible, not missing."""
    import app.agents.deployment_workflow as workflow_module
    from app.memory.in_memory import InMemoryMemoryStore
    from app.memory.service import DeploymentMemoryService as Service
    from app.models.deployment_record import DeploymentQuery
    from app.models.docker import DockerDeployRequest

    store = InMemoryMemoryStore()
    service = Service(store)
    set_memory_service(service)

    request = DockerDeployRequest(
        repository_path="../examples/sample_app",
        image="acme/api:dev",
        container_name="acme-api",
        approve=True,
        approval_reference="tester",
    )

    class ExplodingGraph:
        async def ainvoke(self, state: object) -> dict[str, object]:
            raise RuntimeError("planner crashed")

    original = workflow_module.deployment_workflow_graph
    workflow_module.deployment_workflow_graph = ExplodingGraph()  # type: ignore[assignment]
    try:
        with pytest.raises(RuntimeError, match="planner crashed"):
            await workflow_module.run_deployment_workflow(request)
    finally:
        workflow_module.deployment_workflow_graph = original  # type: ignore[assignment]

    listed = await service.list_deployments(DeploymentQuery())
    assert listed.total == 1
    assert listed.items[0].status is DeploymentStatus.IN_PROGRESS

    detail = await service.get_deployment(listed.items[0].deployment_id)
    assert detail is not None
    assert detail.terminal is False, "an interrupted run is not a finished one"


async def test_the_memory_singleton_is_replaceable_and_clearable() -> None:
    """Tests must be able to swap the store without leaking it into each other."""
    from app.memory import get_memory_service
    from app.memory.in_memory import InMemoryMemoryStore
    from app.memory.service import DeploymentMemoryService as Service

    set_memory_service(None)
    first = Service(InMemoryMemoryStore())
    set_memory_service(first)
    assert get_memory_service() is first

    set_memory_service(None)
    assert get_memory_service() is not first
