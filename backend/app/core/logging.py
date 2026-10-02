"""Logging configuration for the backend.

Two formats are supported: a human-readable console format for local
development and structured JSON for log aggregation later on.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

from app.core.config import Settings

_LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"


class JsonFormatter(logging.Formatter):
    """Render log records as single-line JSON objects."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def configure_logging(settings: Settings) -> None:
    """Install Aegis' log format on the root logger.

    ``force=True`` replaces any handlers installed by imported libraries so
    output is not duplicated.
    """
    handler = logging.StreamHandler(sys.stdout)
    if settings.log_format == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter(_LOG_FORMAT))

    logging.basicConfig(
        level=settings.log_level,
        handlers=[handler],
        force=True,
    )

    # These libraries are chatty at INFO level and add little value for us.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
