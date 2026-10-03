"""Sequential execution engine with retries, timeouts and a full audit trail.

One task at a time, in plan order. Sequential rather than concurrent on purpose:
the tasks in a deployment are causally ordered — you cannot start a container
before its image exists — and a parallel engine would need to encode that ordering
as dependencies anyway while making the audit trail much harder to read.

The engine does not decide what a deployment *should* do. It executes a task list it
is given, and every attempt becomes a durable record whether it worked or not.

Three properties are enforced rather than documented:

- **Nothing runs unapproved.** Every call passes through the real
  :class:`~app.services.mcp_policy.ToolExecutionPolicy` before the tool is
  touched. A refusal is recorded as an action and treated as non-retryable, so a
  denied call cannot be turned into five by the retry loop.
- **Retries stop on the right errors.** Only codes classified
  :attr:`~app.models.execution.ErrorClass.TRANSIENT` are retried. The default for
  an unrecognised code is *permanent*.
- **The audit trail is redacted and written before the next attempt.** Arguments
  pass through key-based redaction, so a credential cannot reach a log that will
  be read, copied and archived.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.models.execution import (
    ActionStatus,
    ErrorClass,
    ExecutionAction,
    ExecutionGraphState,
    ExecutionRequest,
    ExecutionResponse,
    ExecutionState,
    Task,
    TaskResult,
    TaskStatus,
    classify_error,
    is_retryable,
)
from app.models.mcp import ToolCallRequest, ToolCallResult
from app.services.mcp_manager import get_manager

logger = logging.getLogger(__name__)

#: Who the engine attributes its calls to, for the audit trail.
REQUESTED_BY = "deployment-execution-engine"


def _summary(content: Any, limit: int = 200) -> str | None:
    """A short, non-sensitive description of a tool result.

    Only well-known non-secret keys are quoted. Reflecting an arbitrary payload
    into the audit trail would put whatever the tool returned -- possibly a token
    it echoed back -- into a durable log.
    """
    if content is None:
        return None
    if isinstance(content, dict):
        parts: list[str] = []
        for key in ("image", "container", "status", "available", "healthy", "truncated"):
            if key in content and isinstance(content[key], (str, bool, int, float)):
                parts.append(f"{key}={content[key]}")
        return " ".join(parts)[:limit] or None
    if isinstance(content, str):
        return content[:limit]
    return None


async def _call_tool(task: Task, approve: bool, approval_reference: str | None) -> ToolCallResult:
    """Invoke one task's tool through the MCP layer, where policy is applied."""
    request = ToolCallRequest(
        tool_name=task.tool or "",
        arguments=dict(task.arguments),
        requested_by=REQUESTED_BY,
        approval_granted=approve,
        approval_reference=approval_reference if task.requires_approval else None,
        reason=task.description or f"executing planned task {task.id}",
    )
    return await get_manager().call_tool(request)


def _status_for(result: ToolCallResult, timed_out: bool = False) -> ActionStatus:
    """Map a tool result onto an attempt status."""
    if timed_out:
        return ActionStatus.TIMEOUT
    if result.success:
        return ActionStatus.SUCCESS
    if result.error_code in {"policy_denied"}:
        return ActionStatus.POLICY_DENIED
    if result.error_code in {"approval_required"}:
        return ActionStatus.APPROVAL_REQUIRED
    return ActionStatus.FAILURE


def _task_status_for(action: ActionStatus) -> TaskStatus:
    if action is ActionStatus.SUCCESS:
        return TaskStatus.SUCCEEDED
    if action is ActionStatus.TIMEOUT:
        return TaskStatus.TIMED_OUT
    if action in {ActionStatus.POLICY_DENIED, ActionStatus.APPROVAL_REQUIRED}:
        return TaskStatus.REJECTED
    return TaskStatus.FAILED


async def run_task(state: ExecutionState, task: Task) -> TaskResult:
    """Run one task to a terminal outcome, retrying only what retrying can fix.

    Every attempt appends an :class:`ExecutionAction` before the next begins, so a
    run that is killed mid-way still leaves a complete record of what it had done.
    """
    actions: list[ExecutionAction] = []
    attempts = 0
    last: ExecutionAction | None = None

    for attempt in range(1, task.max_attempts + 1):
        attempts = attempt
        attempt_started = time.monotonic()

        try:
            result = await asyncio.wait_for(
                _call_tool(task, state.approval_granted, state.approval_reference),
                timeout=task.timeout_seconds,
            )
        except TimeoutError:
            # A timeout is the engine's own observation, so the error code is set
            # here rather than being inherited from a tool that never answered.
            action = ExecutionAction(
                sequence=state.sequence + attempt - 1,
                task_id=task.id,
                task_title=task.title,
                tool=task.tool,
                arguments=task.redacted_arguments(),
                status=ActionStatus.TIMEOUT,
                duration_seconds=round(time.monotonic() - attempt_started, 4),
                error=f"Tool did not respond within {task.timeout_seconds:g}s.",
                error_code="timeout",
                error_class=ErrorClass.TIMEOUT,
                retry_count=attempt - 1,
                attempt=attempt,
            )
            actions.append(action)
            last = action
            logger.warning("task timed out", extra=action.as_log_fields())

            if attempt < task.max_attempts:
                await asyncio.sleep(task.retry_backoff_seconds * attempt)
                continue
            break

        status = _status_for(result)
        error_class = None if result.success else classify_error(result.error_code)
        action = ExecutionAction(
            sequence=state.sequence + attempt - 1,
            task_id=task.id,
            task_title=task.title,
            tool=task.tool,
            arguments=task.redacted_arguments(),
            status=status,
            duration_seconds=round(time.monotonic() - attempt_started, 4),
            error=result.error_message,
            error_code=result.error_code,
            error_class=error_class,
            retry_count=attempt - 1,
            attempt=attempt,
            result_summary=_summary(result.content),
        )
        actions.append(action)
        last = action
        logger.info("task attempt", extra=action.as_log_fields())

        if result.success:
            return TaskResult(
                task=task,
                status=TaskStatus.SUCCEEDED,
                attempts=attempts,
                actions=actions,
                output=result.content if isinstance(result.content, dict) else {},
            )

        # A refusal is answered by a human. Retrying identical arguments would only
        # produce identical refusals, so the loop stops immediately.
        if error_class in {ErrorClass.POLICY, ErrorClass.PERMANENT}:
            break
        if not is_retryable(result.error_code):
            break
        if attempt < task.max_attempts:
            await asyncio.sleep(task.retry_backoff_seconds * attempt)

    assert last is not None, "a task always records at least one attempt"
    return TaskResult(
        task=task,
        status=_task_status_for(last.status),
        attempts=attempts,
        actions=actions,
        error=last.error,
        error_code=last.error_code,
        error_class=last.error_class,
    )


async def execute_run(request: ExecutionRequest) -> ExecutionResponse:
    """Execute a task list sequentially and return the run with its audit trail."""
    run_id = f"run_{uuid.uuid4().hex[:12]}"
    state = ExecutionState(
        run_id=run_id,
        tasks=list(request.tasks),
        approval_granted=request.approve,
        approval_reference=request.approval_reference,
    )
    started = time.monotonic()

    if request.dry_run:
        # Record the intent without calling anything. Useful for showing an
        # operator exactly what would run, and safe because nothing happens.
        for task in request.tasks:
            action = ExecutionAction(
                sequence=state.sequence,
                task_id=task.id,
                task_title=task.title,
                tool=task.tool,
                arguments=task.redacted_arguments(),
                status=ActionStatus.SKIPPED,
                error="Dry run: no tool was called.",
                retry_count=0,
                attempt=1,
            )
            state.actions.append(action)
            state.results.append(
                TaskResult(
                    task=task,
                    status=TaskStatus.SKIPPED,
                    attempts=0,
                    actions=[action],
                    error="Dry run.",
                )
            )
        return _to_response(state, started, stopped=False, status="dry_run", dry_run=True)

    for index, task in enumerate(request.tasks):
        state.current_index = index
        result = await run_task(state, task)

        state.actions.extend(result.actions)
        state.results.append(result)

        if result.succeeded:
            continue

        if result.status is TaskStatus.REJECTED:
            state.stopped = True
            state.stop_task_id = task.id
            state.stop_reason = (
                f"{task.title!r} was refused: {result.error or 'policy or approval required'}. "
                "Later tasks depend on it and were not run."
            )
            break

        if task.critical and request.stop_on_critical_failure:
            state.stopped = True
            state.stop_task_id = task.id
            state.stop_reason = (
                f"{task.title!r} failed after {result.attempts} attempt(s): "
                f"{result.error or 'unknown error'}. It is a critical step, so the "
                "run stopped rather than building on a broken foundation."
            )
            break

    status = "dry_run" if request.dry_run else ("stopped" if state.stopped else "completed")
    failed = [item.task.id for item in state.results if not item.succeeded]
    if not state.stopped and failed:
        status = "failed"
    return _to_response(state, started, stopped=state.stopped, status=status)


def _to_response(
    state: ExecutionState,
    started: float,
    *,
    stopped: bool,
    status: str,
    dry_run: bool = False,
) -> ExecutionResponse:
    """Fold the run state into the response."""
    state.finished_at = datetime.now(UTC)
    return ExecutionResponse(
        run_id=state.run_id,
        stopped=stopped,
        stop_reason=state.stop_reason,
        stop_task_id=state.stop_task_id,
        status=status,
        results=state.results,
        actions=state.actions,
        succeeded=[item.task.id for item in state.results if item.succeeded],
        failed=[item.task.id for item in state.results if not item.succeeded],
        skipped=[item.task.id for item in state.results if item.status is TaskStatus.SKIPPED],
        duration_seconds=round(time.monotonic() - started, 4),
        dry_run=dry_run,
    )


# ---------------------------------------------------------------------------
# LangGraph EXECUTE stage
# ---------------------------------------------------------------------------


async def execute_stage(state: ExecutionGraphState) -> dict[str, Any]:
    """The EXECUTE node: run the planned tasks and keep the audit trail."""
    request = ExecutionRequest(
        tasks=list(state.get("tasks", [])),
        approve=bool(state.get("approve")),
        approval_reference=state.get("approval_reference"),
        stop_on_critical_failure=bool(state.get("stop_on_critical_failure", True)),
        dry_run=bool(state.get("dry_run", False)),
    )
    response = await execute_run(request)
    return {
        "execution": None,
        "response": response,
        "stopped": response.stopped,
        "stop_reason": response.stop_reason,
        "stop_task_id": response.stop_task_id,
    }


async def route_after_execute(state: ExecutionGraphState) -> str:
    """A stopped run has nothing to verify: go to debug, not to VERIFY."""
    return "debug" if state.get("stopped") else "verify"


def build_execution_graph() -> CompiledStateGraph:
    """A one-node graph, so the engine can be dropped into a larger workflow."""
    builder = StateGraph(ExecutionGraphState)
    builder.add_node("execute", execute_stage)
    builder.add_edge(START, "execute")
    builder.add_edge("execute", END)
    return builder.compile()


execution_graph = build_execution_graph()


__all__ = [
    "REQUESTED_BY",
    "build_execution_graph",
    "classify_error",
    "execute_run",
    "execute_stage",
    "execution_graph",
    "is_retryable",
    "route_after_execute",
    "run_task",
]
