"""Exception handlers.

Registers handlers so that *every* error path produces the same envelope defined
in :mod:`app.models.errors`, including errors raised inside FastAPI itself and
unhandled exceptions. Registering a handler for :class:`Exception` is what stops
a traceback from ever reaching the client.
"""

from __future__ import annotations

from http import HTTPStatus

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.exceptions import AegisError, ErrorCode
from app.core.logging import get_correlation_id, get_logger
from app.models.errors import ErrorResponse

logger = get_logger(__name__)

# Map bare HTTP statuses onto stable error codes so even framework-generated
# errors (a stray 404 on an unknown path) share the API's vocabulary.
_STATUS_TO_CODE: dict[int, ErrorCode] = {
    HTTPStatus.BAD_REQUEST: ErrorCode.VALIDATION_ERROR,
    HTTPStatus.UNAUTHORIZED: ErrorCode.PERMISSION_DENIED,
    HTTPStatus.FORBIDDEN: ErrorCode.PERMISSION_DENIED,
    HTTPStatus.NOT_FOUND: ErrorCode.NOT_FOUND,
    HTTPStatus.CONFLICT: ErrorCode.CONFLICT,
    HTTPStatus.UNPROCESSABLE_ENTITY: ErrorCode.VALIDATION_ERROR,
    HTTPStatus.TOO_MANY_REQUESTS: ErrorCode.RATE_LIMITED,
    HTTPStatus.BAD_GATEWAY: ErrorCode.UPSTREAM_ERROR,
    HTTPStatus.GATEWAY_TIMEOUT: ErrorCode.TIMEOUT,
}

_DEFAULT_MESSAGES: dict[int, str] = {
    HTTPStatus.NOT_FOUND: "The requested resource was not found.",
    HTTPStatus.METHOD_NOT_ALLOWED: "The method is not allowed for this resource.",
}


def _envelope(
    request: Request,
    *,
    code: ErrorCode,
    message: str,
    status_code: int,
    details: object | None = None,
) -> JSONResponse:
    """Build the uniform error response.

    ``request`` is accepted so a correlation ID can be attached when one has not
    already been bound to the context.
    """
    correlation_id = get_correlation_id() or getattr(request.state, "correlation_id", None)
    payload = ErrorResponse(
        error={
            "code": str(code),
            "message": message,
            "details": details,
            "correlation_id": correlation_id,
        }
    )
    return JSONResponse(
        status_code=status_code,
        content=jsonable_encoder(payload),
        headers={"X-Request-ID": correlation_id} if correlation_id else None,
    )


async def handle_aegis_error(request: Request, exc: Exception) -> JSONResponse:
    """Render an expected :class:`AegisError`."""
    error = exc if isinstance(exc, AegisError) else AegisError()
    logger.warning(
        "handled error",
        extra={"error_code": str(error.code), "path": request.url.path},
    )
    return _envelope(
        request,
        code=error.code,
        message=error.message,
        status_code=error.status_code,
        details=error.details or None,
    )


_SOURCE_LOCATIONS = frozenset({"body", "query", "path", "header", "cookie"})


def _flatten_location(loc: object) -> str:
    """Turn a validation ``loc`` tuple into a readable field path.

    FastAPI prefixes locations with where the value came from, so
    ``("query", "value")`` becomes ``value`` rather than ``query.value``.
    """
    parts = [str(part) for part in loc] if isinstance(loc, (list, tuple)) else []
    if parts and parts[0] in _SOURCE_LOCATIONS:
        parts = parts[1:]
    return ".".join(parts) or "body"


async def handle_validation_error(request: Request, exc: Exception) -> JSONResponse:
    """Render a FastAPI request-validation failure.

    Field-level messages are normalised into a flat list because the raw
    ``loc`` tuples are awkward for clients to read.
    """
    errors = exc.errors() if isinstance(exc, RequestValidationError) else []
    details = [
        {
            "field": _flatten_location(error.get("loc", ())),
            "message": error.get("msg", "Invalid value"),
            "type": error.get("type", "value_error"),
        }
        for error in errors
    ]
    logger.warning(
        "request validation failed",
        extra={"error_count": len(details), "path": request.url.path},
    )
    return _envelope(
        request,
        code=ErrorCode.VALIDATION_ERROR,
        message="The request failed validation.",
        status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
        # An empty list would be misleading; use None so the field is omitted.
        details=details or None,
    )


async def handle_http_exception(request: Request, exc: Exception) -> JSONResponse:
    """Render an HTTPException raised by Starlette or a route."""
    status_code = getattr(exc, "status_code", HTTPStatus.INTERNAL_SERVER_ERROR)
    detail = getattr(exc, "detail", None) or _DEFAULT_MESSAGES.get(
        status_code, "The request could not be completed."
    )
    code = _STATUS_TO_CODE.get(status_code, ErrorCode.INTERNAL_ERROR)
    logger.warning("http error", extra={"status_code": status_code, "path": request.url.path})
    return _envelope(request, code=code, message=str(detail), status_code=status_code)


async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    """Render any exception that reached the top of the stack.

    The traceback goes to the log, never to the client: stack traces leak
    paths and versions. ``AEGIS_DEBUG`` adds the exception type, which is safe
    enough for local use and still no traceback.
    """
    from app.core.config import get_settings

    logger.exception(
        "unhandled exception",
        extra={"path": request.url.path, "method": request.method},
    )
    message = "An unexpected error occurred."
    if get_settings().debug:
        message = f"{message} ({type(exc).__name__})"
    return _envelope(
        request,
        code=ErrorCode.INTERNAL_ERROR,
        message=message,
        status_code=HTTPStatus.INTERNAL_SERVER_ERROR,
    )


def register_exception_handlers(app: FastAPI) -> None:
    """Attach every Aegis error handler to ``app``.

    Order matters: the specific handlers are registered first, and Starlette
    matches them by exact type before falling back to ``Exception``.
    """
    app.add_exception_handler(AegisError, handle_aegis_error)
    app.add_exception_handler(RequestValidationError, handle_validation_error)
    app.add_exception_handler(StarletteHTTPException, handle_http_exception)
    app.add_exception_handler(Exception, handle_unexpected_error)
