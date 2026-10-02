"""HTTP middleware.

:class:`RequestLoggingMiddleware` establishes the correlation ID for the life of
a request and emits one structured access-log line per request.
"""

from __future__ import annotations

import logging
import re
import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from app.core.logging import (
    get_logger,
    reset_correlation_id,
    set_correlation_id,
)

logger = get_logger(__name__)

CORRELATION_ID_HEADER = "X-Request-ID"

# Health checks would otherwise dominate the log at DEBUG.
_QUIET_PATHS = frozenset({"/health", "/api/v1/health/live"})

# Correlation IDs arrive from clients, so they are treated as untrusted input.
# Restricting the character set prevents log injection via newlines and keeps
# the value safe to embed in log lines and response headers.
_VALID_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def normalise_correlation_id(raw: str | None) -> str:
    """Return a safe correlation ID.

    Uses the client-supplied value when it is well formed, otherwise generates
    a fresh UUID4. Never trusts the incoming value verbatim.
    """
    if raw:
        candidate = raw.strip()
        if _VALID_ID.match(candidate):
            return candidate
    return str(uuid.uuid4())


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Log every request and attach a correlation ID.

    The ID is bound to a :class:`~contextvars.ContextVar` so application code can
    include it in any log line without threading a parameter through every call,
    and it is echoed back on the response for support and debugging.
    """

    def __init__(
        self,
        app,
        *,
        header_name: str = CORRELATION_ID_HEADER,
        quiet_paths: frozenset[str] = _QUIET_PATHS,
        trust_forwarded_headers: bool = False,
    ) -> None:
        super().__init__(app)
        self.header_name = header_name
        self.quiet_paths = quiet_paths
        self.trust_forwarded_headers = trust_forwarded_headers

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        correlation_id = normalise_correlation_id(request.headers.get(self.header_name))
        # Stash it on the state too, so error handlers can reach it even if the
        # context variable has already been reset.
        request.state.correlation_id = correlation_id

        token = set_correlation_id(correlation_id)
        started_at = time.perf_counter()

        try:
            response = await call_next(request)
        except Exception:
            # Let the exception bubble to the registered handlers, which turn it
            # into the standard error envelope.
            duration_ms = round((time.perf_counter() - started_at) * 1000, 2)
            logger.exception(
                "request failed",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "duration_ms": duration_ms,
                },
            )
            raise
        else:
            # Runs before `finally`, so the correlation ID is still bound here
            # and lands in the access-log record.
            duration_ms = round((time.perf_counter() - started_at) * 1000, 2)
            response.headers[self.header_name] = correlation_id
            self._log_access(request, response, duration_ms, correlation_id)
            return response
        finally:
            reset_correlation_id(token)

    def _log_access(
        self,
        request: Request,
        response: Response,
        duration_ms: float,
        correlation_id: str,
    ) -> None:
        """Emit one structured line per request."""
        if request.url.path in self.quiet_paths:
            return

        # The correlation ID is stamped from the bound context by the record
        # factory, so it does not need to be repeated here. Passing it via
        # `extra` would fail: logging rejects extras that already exist on a
        # record, and the factory always creates the field.
        fields = {
            "method": request.method,
            "path": request.url.path,
            "status_code": response.status_code,
            "duration_ms": duration_ms,
        }

        client_ip = self._resolve_client_ip(request)
        if client_ip is not None:
            fields["client_ip"] = client_ip

        if request.url.query:
            fields["query"] = request.url.query

        # 5xx means the request failed on our side and deserves attention.
        level = logging.WARNING if response.status_code >= 500 else logging.INFO
        logger.log(level, "request completed", extra=fields)

    def _resolve_client_ip(self, request: Request) -> str | None:
        """Determine the client address for the access log.

        ``X-Forwarded-For`` is client-controlled, so it is only honoured when the
        deployment is known to sit behind a trusted proxy. Otherwise anyone could
        forge their address in the logs.
        """
        if self.trust_forwarded_headers:
            forwarded = request.headers.get("X-Forwarded-For")
            if forwarded:
                # Left-most entry is the original client.
                return forwarded.split(",")[0].strip()

        return request.client.host if request.client is not None else None
