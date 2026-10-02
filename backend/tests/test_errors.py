"""Tests for the uniform API error envelope."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.errors import register_exception_handlers
from app.core.exceptions import (
    AegisError,
    ConfigurationError,
    ConflictError,
    ErrorCode,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from app.core.middleware import RequestLoggingMiddleware


def build_app() -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)
    # Mirror production: the correlation-ID middleware always runs, so error
    # payloads can be expected to carry an ID.
    app.add_middleware(RequestLoggingMiddleware)

    @app.get("/not-found")
    def not_found() -> None:
        raise NotFoundError(
            "Deployment dep_123 was not found.",
            details={"resource": "deployment", "id": "dep_123"},
        )

    @app.get("/conflict")
    def conflict() -> None:
        raise ConflictError("A deployment is already in progress.")

    @app.get("/permission")
    def permission() -> None:
        raise PermissionDeniedError()

    @app.get("/crash")
    def crash() -> None:
        raise RuntimeError("internal detail that must not leak")

    @app.get("/typed")
    def typed(value: int) -> dict[str, int]:
        return {"value": value}

    return app


def build_client() -> TestClient:
    # raise_server_exceptions=False so the unhandled-exception handler is
    # exercised instead of the exception propagating into the test.
    return TestClient(build_app(), raise_server_exceptions=False)


def test_known_error_uses_standard_envelope() -> None:
    with build_client() as client:
        body = client.get("/not-found").json()

    error = body["error"]
    assert set(error) == {"code", "message", "details", "correlation_id"}
    assert error["code"] == "not_found"
    assert error["message"] == "Deployment dep_123 was not found."
    assert error["details"] == {"resource": "deployment", "id": "dep_123"}


def test_error_status_codes_map_correctly() -> None:
    with build_client() as client:
        assert client.get("/not-found").status_code == 404
        assert client.get("/conflict").status_code == 409
        assert client.get("/permission").status_code == 403


def test_default_message_is_used_when_none_given() -> None:
    with build_client() as client:
        body = client.get("/permission").json()

    assert body["error"]["code"] == "permission_denied"
    assert body["error"]["message"] == "This action requires explicit approval."
    assert body["error"]["details"] is None


def test_validation_error_is_flattened_and_uses_envelope() -> None:
    with build_client() as client:
        response = client.get("/typed", params={"value": "not-an-int"})

    body = response.json()
    assert response.status_code == 422
    assert body["error"]["code"] == "validation_error"
    assert body["error"]["details"][0]["field"] == "value"


def test_unknown_route_uses_standard_envelope() -> None:
    with build_client() as client:
        response = client.get("/does-not-exist")

    body = response.json()
    assert response.status_code == 404
    assert body["error"]["code"] == "not_found"


def test_unhandled_exception_never_leaks_internals() -> None:
    with build_client() as client:
        response = client.get("/crash")

    body = response.json()
    assert response.status_code == 500
    assert body["error"]["code"] == "internal_error"
    assert "internal detail that must not leak" not in response.text
    assert "Traceback" not in response.text


def test_error_response_includes_correlation_id() -> None:
    with build_client() as client:
        response = client.get("/not-found", headers={"X-Request-ID": "err-trace-1"})

    assert response.json()["error"]["correlation_id"] == "err-trace-1"


def test_error_response_echoes_id_in_header() -> None:
    with build_client() as client:
        response = client.get("/conflict", headers={"X-Request-ID": "err-trace-2"})

    assert response.headers["X-Request-ID"] == "err-trace-2"


def test_error_code_values_are_stable() -> None:
    """These strings are part of the public API contract."""
    assert str(ErrorCode.NOT_FOUND) == "not_found"
    assert str(ErrorCode.VALIDATION_ERROR) == "validation_error"
    assert str(ErrorCode.INTERNAL_ERROR) == "internal_error"


def test_aegis_error_serialises_without_handler() -> None:
    error = AegisError("boom", details={"k": "v"}, code=ErrorCode.CONFLICT)

    assert error.to_dict() == {
        "code": "conflict",
        "message": "boom",
        "details": {"k": "v"},
    }


def test_exception_hierarchy_defaults() -> None:
    assert issubclass(NotFoundError, AegisError)
    assert issubclass(NotFoundError, Exception)
    assert ConfigurationError().status_code == 500
    assert ValidationError().status_code == 422
