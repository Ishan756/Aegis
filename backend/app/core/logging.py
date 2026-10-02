"""Structured logging for the backend.

Two output formats are supported:

* ``console`` — human readable, for local development.
* ``json``    — one JSON object per line, for log aggregation.

Every record carries the active correlation ID (see :mod:`app.core.middleware`)
so a request, the work it triggered and any error it raised can be stitched
together in a log aggregator.

Extra fields are attached with the stdlib convention::

    log = get_logger(__name__)
    log.info("deployment started", extra={"deployment_id": "dep_123"})
"""

from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar, Token
from datetime import UTC, datetime
from typing import Any

from app.core.config import Settings

_CONSOLE_FORMAT = "%(asctime)s %(levelname)-8s %(name)-28s %(message)s"

# Attributes present on every LogRecord; anything else was passed via `extra`
# and is therefore a user field worth serializing.
_RESERVED_RECORD_FIELDS = frozenset(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {
    "message",
    "asctime",
    "taskName",
}


def _extra_fields(record: logging.LogRecord) -> dict[str, Any]:
    return {
        key: value
        for key, value in record.__dict__.items()
        if key not in _RESERVED_RECORD_FIELDS and key != "correlation_id"
    }


class AegisFormatter(logging.Formatter):
    """Render a record as JSON for aggregators, or a readable line for humans."""

    def __init__(self, json_output: bool) -> None:
        super().__init__(None if json_output else _CONSOLE_FORMAT)
        self.json_output = json_output

    def format(self, record: logging.LogRecord) -> str:
        correlation_id = getattr(record, "correlation_id", None)
        extras = _extra_fields(record)

        if not self.json_output:
            line = super().format(record)
            suffix = "".join(f" {k}={v}" for k, v in extras.items())
            return f"{line} [{correlation_id}]{suffix}" if correlation_id else line + suffix

        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "correlation_id": correlation_id,
            **extras,
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


# --------------------------------------------------------------------------
# Correlation ID context
# --------------------------------------------------------------------------

_correlation_id: ContextVar[str | None] = ContextVar("aegis_correlation_id", default=None)


def get_correlation_id() -> str | None:
    """Return the correlation ID for the current context, if any."""
    return _correlation_id.get()


def set_correlation_id(value: str | None) -> Token[str | None]:
    """Bind a correlation ID and return the token needed to reset it."""
    return _correlation_id.set(value)


def reset_correlation_id(token: Token[str | None]) -> None:
    """Restore the correlation ID that was active before :func:`set_correlation_id`."""
    _correlation_id.reset(token)


# The correlation ID is stamped onto every LogRecord as it is created, rather
# than by a handler filter. Logger-level filters do not run for records that
# propagate up from child loggers, and handler-level filters only cover handlers
# we installed ourselves, so a record captured by any other handler (a test
# capture, or an exporter added later) would otherwise have no ID.
_ORIGINAL_RECORD_FACTORY = logging.getLogRecordFactory()


def _aegis_record_factory(*args: object, **kwargs: object) -> logging.LogRecord:
    record = _ORIGINAL_RECORD_FACTORY(*args, **kwargs)  # type: ignore[arg-type]
    record.correlation_id = getattr(record, "correlation_id", None) or get_correlation_id()
    return record


logging.setLogRecordFactory(_aegis_record_factory)


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


def configure_logging(settings: Settings) -> None:
    """Install Aegis' log format on the root logger.

    ``force=True`` replaces handlers installed by imported libraries so output
    is not duplicated.
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(AegisFormatter(json_output=settings.log_format == "json"))

    logging.basicConfig(
        level=settings.log_level_number,
        handlers=[handler],
        force=True,
    )

    # These libraries are chatty at INFO level and add little value here.
    for noisy in ("uvicorn.access", "httpx", "httpcore", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """Return a module logger.

    A thin wrapper so every module imports loggers the same way and the logging
    setup stays in one place.
    """
    return logging.getLogger(name)
