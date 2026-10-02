"""Tests for the health endpoint contract."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_root_returns_service_banner(client: TestClient) -> None:
    response = client.get("/")

    assert response.status_code == 200
    body = response.json()
    assert body["service"] == "Aegis Backend"
    assert body["docs"] == "/docs"


def test_health_reports_ok(client: TestClient) -> None:
    response = client.get("/api/v1/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["service"] == "Aegis Backend"
    assert body["uptime_seconds"] >= 0
    assert body["stage"].startswith("stage-")


def test_health_lists_component_breakdown(client: TestClient) -> None:
    response = client.get("/api/v1/health")

    components = {c["name"]: c for c in response.json()["components"]}
    assert components["api"]["status"] == "ok"
    # Not yet wired up in stage 1, but must be reported honestly.
    assert components["mcp"]["status"] == "not_configured"
    assert components["database"]["status"] == "not_configured"


def test_liveness_probe(client: TestClient) -> None:
    response = client.get("/api/v1/health/live")

    assert response.status_code == 200
    assert response.json() == {"alive": True}


def test_unversioned_health_alias(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
