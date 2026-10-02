"""Tests for the logging layer: correlation IDs and structured output."""

from __future__ import annotations

import json
import logging
from io import StringIO

import pytest

from app.core.config import Settings
from app.core.logging import (
    AegisFormatter,
    configure_logging,
    get_correlation_id,
    get_logger,
    reset_correlation_id,
    set_correlation_id,
)


@pytest.fixture
def json_logs() -> StringIO:
    """Capture root-logger output as JSON lines and restore handlers after."""
    stream = StringIO()
    previous_handlers = logging.getLogger().handlers[:]
    previous_level = logging.getLogger().level

    configure_logging(Settings(_env_file=None, log_format="json", log_level="DEBUG"))

    handler = logging.StreamHandler(stream)
    handler.setFormatter(AegisFormatter(json_output=True))
    logging.getLogger().handlers = [handler]
    logging.getLogger().setLevel(logging.DEBUG)

    yield stream

    logging.getLogger().handlers = previous_handlers
    logging.getLogger().setLevel(previous_level)


def test_emit_json_line_with_message_and_level(json_logs: StringIO) -> None:
    get_logger("aegis.test").info("hello")

    record = json.loads(json_logs.getvalue().strip())

    assert record["message"] == "hello"
    assert record["level"] == "INFO"
    assert record["logger"] == "aegis.test"
    assert "timestamp" in record


def test_extra_fields_are_included_as_structured_data(json_logs: StringIO) -> None:
    get_logger("aegis.test").info("deploy started", extra={"deployment_id": "dep_1"})

    record = json.loads(json_logs.getvalue().strip())

    assert record["deployment_id"] == "dep_1"


def test_correlation_id_is_attached_to_records(json_logs: StringIO) -> None:
    token = set_correlation_id("corr-123")
    try:
        get_logger("aegis.test").info("with id")
    finally:
        reset_correlation_id(token)

    assert json.loads(json_logs.getvalue().strip())["correlation_id"] == "corr-123"


def test_correlation_id_resets_after_context(json_logs: StringIO) -> None:
    token = set_correlation_id("corr-123")
    reset_correlation_id(token)

    get_logger("aegis.test").info("after reset")

    assert json.loads(json_logs.getvalue().strip())["correlation_id"] is None


def test_get_correlation_id_defaults_to_none() -> None:
    assert get_correlation_id() is None


def test_correlation_id_comes_from_context(json_logs: StringIO) -> None:
    """The bound context is the single source of the correlation ID."""
    token = set_correlation_id("ambient-id")
    try:
        get_logger("aegis.test").info("message")
    finally:
        reset_correlation_id(token)

    assert json.loads(json_logs.getvalue().strip())["correlation_id"] == "ambient-id"


def test_correlation_id_cannot_be_passed_via_extra(json_logs: StringIO) -> None:
    """`extra` cannot set correlation_id, because the factory always creates it.

    logging rejects extras that would overwrite an existing LogRecord attribute.
    Callers must bind the context instead; the request middleware relies on this.
    """
    token = set_correlation_id("ambient-id")
    try:
        with pytest.raises(KeyError, match="Attempt to overwrite"):
            get_logger("aegis.test").info("message", extra={"correlation_id": "forced"})
    finally:
        reset_correlation_id(token)


def test_exceptions_are_serialised_not_raised(json_logs: StringIO) -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        get_logger("aegis.test").exception("failed")

    record = json.loads(json_logs.getvalue().strip())

    assert "ValueError: boom" in record["exception"]


def test_console_format_shows_correlation_id_and_extras() -> None:
    stream = StringIO()
    previous = logging.getLogger().handlers[:]
    configure_logging(Settings(_env_file=None, log_format="console"))
    handler = logging.StreamHandler(stream)
    handler.setFormatter(AegisFormatter(json_output=False))
    logging.getLogger().handlers = [handler]

    token = set_correlation_id("corr-abc")
    try:
        get_logger("aegis.test").warning("disk pressure", extra={"free_gb": 2})
    finally:
        reset_correlation_id(token)
        logging.getLogger().handlers = previous

    output = stream.getvalue()

    assert "disk pressure" in output
    assert "[corr-abc]" in output
    assert "free_gb=2" in output
