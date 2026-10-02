"""Application exception types.

Every failure Aegis raises on purpose derives from :class:`AegisError` and
carries the machine-readable ``code`` that appears in the HTTP error response.
Handlers translate these into the envelope defined in
:mod:`app.models.errors`; callers therefore never build error payloads by hand.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    """Stable, machine-readable error identifiers.

    These are part of the API contract: clients may branch on them, so values
    are never renamed once published.
    """

    CONFIGURATION_ERROR = "configuration_error"
    VALIDATION_ERROR = "validation_error"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    PERMISSION_DENIED = "permission_denied"
    UPSTREAM_ERROR = "upstream_error"
    TIMEOUT = "timeout"
    RATE_LIMITED = "rate_limited"
    INTERNAL_ERROR = "internal_error"


class AegisError(Exception):
    """Base class for expected, reportable failures."""

    code: ErrorCode = ErrorCode.INTERNAL_ERROR
    status_code: int = 500
    default_message: str = "An unexpected error occurred."

    def __init__(
        self,
        message: str | None = None,
        *,
        details: dict[str, Any] | None = None,
        code: ErrorCode | None = None,
        status_code: int | None = None,
    ) -> None:
        self.message = message or self.default_message
        self.details = details or {}
        if code is not None:
            self.code = code
        if status_code is not None:
            self.status_code = status_code
        super().__init__(self.message)

    def to_dict(self) -> dict[str, Any]:
        """Serialise to the API error envelope."""
        return {
            "code": str(self.code),
            "message": self.message,
            "details": self.details or None,
        }


class ConfigurationError(AegisError):
    """Required configuration is missing or invalid."""

    code = ErrorCode.CONFIGURATION_ERROR
    status_code = 500
    default_message = "The service is misconfigured."


class ValidationError(AegisError):
    """Input failed a domain rule."""

    code = ErrorCode.VALIDATION_ERROR
    status_code = 422
    default_message = "The request failed validation."


class NotFoundError(AegisError):
    """A referenced resource does not exist."""

    code = ErrorCode.NOT_FOUND
    status_code = 404
    default_message = "The requested resource was not found."


class ConflictError(AegisError):
    """The request conflicts with current state."""

    code = ErrorCode.CONFLICT
    status_code = 409
    default_message = "The request conflicts with the current state."


class PermissionDeniedError(AegisError):
    """The action is not permitted.

    Raised by the approval gate for tools that have not been authorised. Aegis
    does not yet perform authentication; this exists so the refusal path has a
    defined shape before the auth stage.
    """

    code = ErrorCode.PERMISSION_DENIED
    status_code = 403
    default_message = "This action requires explicit approval."


class UpstreamError(AegisError):
    """An external dependency failed.

    Reserved for MCP servers, GitHub and cloud APIs. Not raised yet: no client
    exists.
    """

    code = ErrorCode.UPSTREAM_ERROR
    status_code = 502
    default_message = "An upstream service failed to respond."


class UpstreamTimeoutError(AegisError):
    """An external dependency exceeded its deadline."""

    code = ErrorCode.TIMEOUT
    status_code = 504
    default_message = "An upstream service timed out."
