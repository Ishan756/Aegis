"""Execution engine contracts.

A plan says what should happen; an execution is what actually happened. The gap
between the two is where deployments go wrong, so this module exists to make the
gap legible: every attempt, including the failed ones, becomes a durable
:class:`ExecutionAction` before the engine moves on.

Retryability is a property of *why* something failed, not of what was being done.
A refused call and a corrupt argument both say "do not try again"; a daemon that
was briefly down does not. Modelling that as a field rather than a caller's
judgement is what keeps a retry loop from turning one bad argument into five.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, TypedDict

from pydantic import BaseModel, Field


class TaskStatus(StrEnum):
    """Lifecycle of a single task."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    TIMED_OUT = "timed_out"
    REJECTED = "rejected"


class ActionStatus(StrEnum):
    """Outcome of one attempt. An attempt, not a task: retries are separate rows."""

    SUCCESS = "success"
    FAILURE = "failure"
    TIMEOUT = "timeout"
    POLICY_DENIED = "policy_denied"
    APPROVAL_REQUIRED = "approval_required"
    SKIPPED = "skipped"


class ErrorClass(StrEnum):
    """Why an attempt failed, and therefore whether retrying can help."""

    #: Transient infrastructure: a busy daemon, a dropped socket.
    TRANSIENT = "transient"
    #: The arguments were wrong. Retrying identical arguments cannot help.
    PERMANENT = "permanent"
    #: Policy refused the call. Retrying without new approval cannot help.
    POLICY = "policy"
    #: The call was attempted and ran out of time.
    TIMEOUT = "timeout"
    #: The tool itself failed in a way the caller cannot classify.
    UNKNOWN = "unknown"


#: MCP error codes that mean "not now, try later". A refused call is deliberately
#: absent: `policy_denied` and `approval_required` are answered by a human, not by
#: waiting.
RETRYABLE_ERROR_CODES: frozenset[str] = frozenset(
    {
        "timeout",
        "connection_error",
        "unavailable",
        "rate_limited",
        "internal_error",
        "resource_busy",
        "daemon_starting",
        "upstream_error",
    }
)

#: Codes that mean "stop asking". Anything not listed as retryable is permanent,
#: which is the safe default: retrying a refusal wastes time and, worse, makes an
#: operator believe a gate was passed.
NON_RETRYABLE_ERROR_CODES: frozenset[str] = frozenset(
    {
        "policy_denied",
        "approval_required",
        "invalid_tool",
        "unknown_tool",
        "not_found",
        "validation_error",
        "invalid_arguments",
        "invalid_tool_arguments",
        "permission_denied",
        "tool_error",
        "forbidden",
        "unauthorized",
    }
)


def classify_error(error_code: str | None) -> ErrorClass:
    """Map an MCP error code onto a retry decision.

    The default is :attr:`ErrorClass.PERMANENT`. Assuming a failure is transient
    when it is not is the expensive mistake: it multiplies the damage and delays
    the report of a real problem.
    """
    if error_code is None:
        return ErrorClass.UNKNOWN
    if error_code in NON_RETRYABLE_ERROR_CODES:
        if error_code in {"policy_denied", "approval_required"}:
            return ErrorClass.POLICY
        return ErrorClass.PERMANENT
    if error_code in RETRYABLE_ERROR_CODES:
        return ErrorClass.TRANSIENT
    return ErrorClass.UNKNOWN


def is_retryable(error_code: str | None) -> bool:
    """True only for failures a retry could plausibly fix."""
    return classify_error(error_code) is ErrorClass.TRANSIENT


class Task(BaseModel):
    """One unit of work, derived from a plan step."""

    id: str = Field(description="Stable identifier, unique within the run.")
    title: str
    description: str = ""
    tool: str | None = Field(
        default=None, description="Qualified MCP tool, e.g. 'docker.build_image'."
    )
    arguments: dict[str, Any] = Field(default_factory=dict)
    requires_approval: bool = False
    approval_reference: str | None = None
    critical: bool = Field(
        default=False,
        description="A failure here stops the run; later tasks will not run.",
    )
    timeout_seconds: float = Field(default=120.0, gt=0.0, le=3600.0)
    max_attempts: int = Field(default=1, ge=1, le=10)
    retry_backoff_seconds: float = Field(default=1.0, ge=0.0, le=60.0)

    def redacted_arguments(self) -> dict[str, Any]:
        """Arguments with anything secret-looking replaced.

        Key-name based, not value-based: a secret is identified by what it is
        called far more reliably than by what it looks like. Recording the raw
        arguments would put credentials in a log that gets read, copied and
        archived.
        """
        return {key: _redact_value(key, value) for key, value in self.arguments.items()}


#: Argument names whose values must never reach the audit trail.
_SECRET_HINTS = (
    "password",
    "passwd",
    "secret",
    "token",
    "credential",
    "apikey",
    "api_key",
    "auth",
    "private_key",
    "dsn",
    "connection_string",
)

REDACTED = "***redacted***"


def _redact_value(key: str, value: Any) -> Any:
    lowered = key.lower()
    if any(hint in lowered for hint in _SECRET_HINTS):
        return REDACTED
    # A DSN or URL can carry a password in its userinfo section.
    if isinstance(value, str) and "://" in value and "@" in value:
        scheme, _, rest = value.partition("://")
        _, _, host = rest.rpartition("@")
        return f"{scheme}://***redacted***@{host}"
    return value


class ExecutionAction(BaseModel):
    """One attempt at one task. Append-only, and written before the next begins."""

    sequence: int = Field(ge=1, description="Monotonic across the whole run.")
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    task_id: str
    task_title: str = ""
    tool: str | None = None
    arguments: dict[str, Any] = Field(
        default_factory=dict, description="Redacted arguments. Never raw secrets."
    )
    status: ActionStatus
    duration_seconds: float = Field(default=0.0, ge=0.0)
    error: str | None = None
    error_code: str | None = None
    error_class: ErrorClass | None = None
    retry_count: int = Field(default=0, ge=0, description="Retries already spent.")
    attempt: int = Field(default=1, ge=1, description="1-based attempt number.")
    result_summary: str | None = Field(
        default=None, description="Short, non-sensitive description of the result."
    )

    def as_log_fields(self) -> dict[str, Any]:
        """Structured fields for ``logging``'s ``extra``.

        ``extra`` must be a mapping. A pre-rendered line belongs in a message, not
        in ``extra``, and passing one raises ``TypeError`` at log time -- which
        would take down the very call it was meant to record.
        """
        return {
            "sequence": self.sequence,
            "task_id": self.task_id,
            "tool": self.tool or "-",
            "status": str(self.status),
            "attempt": self.attempt,
            "retry_count": self.retry_count,
            "duration_seconds": self.duration_seconds,
            "error_code": self.error_code or "-",
            "error_class": str(self.error_class) if self.error_class else "-",
        }

    def as_log_line(self) -> str:
        """A single line suitable for structured logging."""
        parts = [
            f"seq={self.sequence}",
            f"task={self.task_id}",
            f"tool={self.tool or '-'}",
            f"status={self.status}",
            f"attempt={self.attempt}",
            f"retries={self.retry_count}",
            f"duration={self.duration_seconds:.3f}s",
        ]
        if self.error_code:
            parts.append(f"error_code={self.error_code}")
        if self.error_class:
            parts.append(f"error_class={self.error_class}")
        if self.error:
            parts.append(f"error={self.error[:200]}")
        return " ".join(parts)


class TaskResult(BaseModel):
    """The accumulated outcome of one task across its attempts."""

    task: Task
    status: TaskStatus
    attempts: int = Field(default=0, ge=0)
    actions: list[ExecutionAction] = Field(default_factory=list)
    output: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    error_code: str | None = None
    error_class: ErrorClass | None = None

    @property
    def succeeded(self) -> bool:
        return self.status is TaskStatus.SUCCEEDED


class ExecutionState(BaseModel):
    """Mutable state carried across the whole run."""

    run_id: str
    tasks: list[Task] = Field(default_factory=list)
    results: list[TaskResult] = Field(default_factory=list)
    actions: list[ExecutionAction] = Field(default_factory=list)
    current_index: int = Field(default=0, ge=0)
    stopped: bool = Field(default=False)
    stop_reason: str | None = None
    stop_task_id: str | None = None
    approval_granted: bool = Field(
        default=False, description="Set once the caller approves the run."
    )
    approval_reference: str | None = None
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None

    @property
    def sequence(self) -> int:
        """The next action sequence number."""
        return len(self.actions) + 1

    def result_for(self, task_id: str) -> TaskResult | None:
        return next((item for item in self.results if item.task.id == task_id), None)

    def audit_lines(self) -> list[str]:
        """Every recorded action, oldest first."""
        return [action.as_log_line() for action in self.actions]


class ExecutionRequest(BaseModel):
    """Body of ``POST /api/deployment/execute``."""

    tasks: list[Task] = Field(min_length=1, max_length=50)
    approve: bool = Field(
        default=False,
        description="Approve the mutating tools this run will call. Without it they are refused.",
    )
    approval_reference: str | None = Field(default=None, max_length=200)
    stop_on_critical_failure: bool = Field(
        default=True,
        description="Stop the run when a critical task fails instead of pressing on.",
    )
    dry_run: bool = Field(
        default=False, description="Evaluate policy and record intent without calling any tool."
    )


class ExecutionResponse(BaseModel):
    """Response body. The audit trail is part of the result, not a debug extra."""

    run_id: str
    stopped: bool = False
    stop_reason: str | None = None
    stop_task_id: str | None = None
    status: str = Field(description="'succeeded', 'failed', 'stopped' or 'dry_run'.")
    results: list[TaskResult] = Field(default_factory=list)
    actions: list[ExecutionAction] = Field(default_factory=list)
    succeeded: list[str] = Field(default_factory=list)
    failed: list[str] = Field(default_factory=list)
    skipped: list[str] = Field(default_factory=list)
    duration_seconds: float = Field(default=0.0, ge=0.0)
    dry_run: bool = False

    def summary_markdown(self) -> str:
        """Render the run for a human reviewer."""
        lines = [
            f"# Execution {self.run_id} — {self.status}",
            "",
            f"**Tasks:** {len(self.succeeded)} succeeded, "
            f"{len(self.failed)} failed, {len(self.skipped)} skipped  ",
            f"**Duration:** {self.duration_seconds:.2f}s  ",
            f"**Actions recorded:** {len(self.actions)}",
            "",
        ]
        if self.stopped and self.stop_reason:
            lines += [f"**Stopped at `{self.stop_task_id}`:** {self.stop_reason}", ""]
        lines += [
            "| # | Task | Tool | Status | Attempts | Error |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for result in self.results:
            lines.append(
                f"| {result.task.id} | {result.task.title} | "
                f"{result.task.tool or '—'} | {result.status} | {result.attempts} "
                f"| {(result.error or '—')[:60]} |"
            )
        lines.append("")
        lines += ["## Audit trail", "", "```"]
        lines += [action.as_log_line() for action in self.actions]
        lines += ["```"]
        return "\n".join(lines)


class ExecutionGraphState(TypedDict, total=False):
    """LangGraph state for the EXECUTE stage."""

    tasks: list[Task]
    approve: bool
    approval_reference: str | None
    stop_on_critical_failure: bool
    dry_run: bool
    execution: ExecutionState
    response: ExecutionResponse
    stopped: bool
    stop_reason: str | None
    stop_task_id: str | None
