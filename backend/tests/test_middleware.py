"""Tests for request logging middleware and correlation IDs."""

from __future__ import annotations

import logging

from fastapi import APIRouter, FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from app.core.logging import get_correlation_id, set_correlation_id
from app.core.middleware import (
    RequestLoggingMiddleware,
    normalise_correlation_id,
)

router = APIRouter()


@router.get("/ok")
def read_ok() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/boom")
def read_boom() -> None:
    raise RuntimeError("deliberate failure")


@router.get("/echo-correlation")
def read_echo() -> dict[str, str | None]:
    return {"correlation_id": get_correlation_id()}


def build_client() -> TestClient:
    app = FastAPI()
    app.add_exception_handler(
        RuntimeError,
        lambda request, exc: JSONResponse(status_code=500, content={"detail": "handled"}),
    )
    app.include_router(router)
    app.add_middleware(RequestLoggingMiddleware)
    return TestClient(app, raise_server_exceptions=False)


def test_generates_correlation_id_when_absent() -> None:
    with build_client() as client:
        response = client.get("/ok")

    assert response.headers["X-Request-ID"]


def test_echoes_client_supplied_correlation_id() -> None:
    with build_client() as client:
        response = client.get("/ok", headers={"X-Request-ID": "req-abc-123"})

    assert response.headers["X-Request-ID"] == "req-abc-123"


def test_correlation_id_is_available_to_handlers() -> None:
    with build_client() as client:
        body = client.get("/echo-correlation", headers={"X-Request-ID": "req-xyz"}).json()

    assert body["correlation_id"] == "req-xyz"


def test_malformed_correlation_id_is_replaced_not_trusted() -> None:
    """Untrusted IDs must not reach logs or headers verbatim."""
    with build_client() as client:
        response = client.get("/ok", headers={"X-Request-ID": "bad id\ninjected"})

    echoed = response.headers["X-Request-ID"]
    assert echoed != "bad id\ninjected"
    assert "\n" not in echoed


def test_normalise_keeps_valid_ids() -> None:
    assert normalise_correlation_id("abc-123") == "abc-123"


def test_normalise_generates_for_missing_value() -> None:
    assert len(normalise_correlation_id(None)) == 36  # uuid4


def test_normalise_rejects_overlong_value() -> None:
    assert len(normalise_correlation_id("a" * 200)) == 36


def test_access_log_emits_structured_fields(caplog) -> None:
    with caplog.at_level(logging.INFO, logger="app.core.middleware"), build_client() as client:
        client.get("/ok?debug=1")

    record = next(r for r in caplog.records if r.getMessage() == "request completed")

    assert record.method == "GET"
    assert record.path == "/ok"
    assert record.status_code == 200
    assert record.duration_ms >= 0
    assert record.correlation_id
    assert record.query == "debug=1"


def test_health_probes_are_not_logged(caplog) -> None:
    app = FastAPI()
    app.add_middleware(RequestLoggingMiddleware)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    with caplog.at_level(logging.INFO, logger="app.core.middleware"), TestClient(app) as client:
        client.get("/health")

    assert not [r for r in caplog.records if r.getMessage() == "request completed"]


def test_correlation_id_does_not_leak_between_requests() -> None:
    """A stale ID from one request must not appear on the next."""
    with build_client() as client:
        first = client.get("/echo-correlation", headers={"X-Request-ID": "req-first"}).json()
        second = client.get("/echo-correlation").json()

    assert first["correlation_id"] == "req-first"
    assert second["correlation_id"] != "req-first"


def test_correlation_id_resets_after_request() -> None:
    with build_client() as client:
        client.get("/ok", headers={"X-Request-ID": "req-leak"})

    assert get_correlation_id() is None


def test_set_correlation_id_returns_restorable_token() -> None:
    token = set_correlation_id("manual")
    try:
        assert get_correlation_id() == "manual"
    finally:
        from app.core.logging import reset_correlation_id

        reset_correlation_id(token)

    assert get_correlation_id() is None


def test_client_ip_uses_socket_peer_by_default(caplog) -> None:
    """X-Forwarded-For is client-controlled and must be ignored by default."""
    with caplog.at_level(logging.INFO, logger="app.core.middleware"), build_client() as client:
        client.get("/ok", headers={"X-Forwarded-For": "1.2.3.4"})

    record = next(r for r in caplog.records if r.getMessage() == "request completed")

    assert record.client_ip != "1.2.3.4"


def test_client_ip_honours_forwarded_header_when_enabled(caplog) -> None:
    app = FastAPI()
    app.include_router(router)
    app.add_middleware(RequestLoggingMiddleware, trust_forwarded_headers=True)

    with caplog.at_level(logging.INFO, logger="app.core.middleware"), TestClient(app) as client:
        client.get("/ok", headers={"X-Forwarded-For": "1.2.3.4, 10.0.0.1"})

    record = next(r for r in caplog.records if r.getMessage() == "request completed")

    assert record.client_ip == "1.2.3.4"
