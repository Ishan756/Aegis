"""The service the rest of the application talks to.

Workflows do not know whether history lives in Postgres or in a dictionary, and
they certainly should not build :class:`DeploymentRecord` themselves. This service
owns that translation: it takes the loosely-typed result of a deployment run and
produces a record with a final status, a normalised failure list and any lessons
worth keeping.

The failure list is the part worth explaining. Four stages each report failure in
their own shape — a task result, a verification check, an incident cause — and a
history that shows only the last one read is not a trace. So they are flattened
into :class:`FailureRecord` without pretending the stages agree on what a failure
is: ``stage`` and ``kind`` stay distinct, and nothing is dropped for lacking a
category elsewhere.

Persistence is best-effort by design. A database outage must not fail a
deployment that otherwise succeeded, so writes are logged and swallowed. Losing a
history row is recoverable; refusing to deploy because a ledger was down is not.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any

from app.core.config import Settings, get_settings
from app.memory.base import LessonMatcher, MemoryStore
from app.memory.in_memory import InMemoryMemoryStore
from app.memory.lessons import derive_lessons, now
from app.memory.matching import KeywordLessonMatcher
from app.memory.postgres import PostgresMemoryStore
from app.models.deployment_record import (
    DeploymentDetail,
    DeploymentListResponse,
    DeploymentQuery,
    DeploymentRecord,
    DeploymentStatus,
    FailureRecord,
    FailureStage,
    Lesson,
    MemoryHealth,
)

logger = logging.getLogger(__name__)

# A ref that looks like a commit, as opposed to a branch name. Short shas are
# accepted because that is what a human would have written down.
_SHA = frozenset("0123456789abcdef")


class DeploymentMemoryService:
    """Records deployments, answers questions about them, and remembers lessons."""

    def __init__(self, store: MemoryStore, *, matcher: LessonMatcher | None = None) -> None:
        self._store = store
        self._matcher = matcher or KeywordLessonMatcher()

    @property
    def backend(self) -> str:
        return self._store.backend

    async def start(self) -> None:
        await self._store.start()

    async def close(self) -> None:
        await self._store.close()

    async def health(self) -> MemoryHealth:
        """Report store reachability. Never raises.

        The contract belongs here rather than in each store: a health endpoint
        that can raise is a health endpoint that reports "down" for the wrong
        reason, and every caller has to guard it.
        """
        try:
            return await self._store.health()
        except Exception as exc:  # noqa: BLE001 - reporting is the whole job
            logger.error("memory store health check failed", extra={"error": str(exc)})
            return MemoryHealth(backend=self._store.backend, status="error", detail=str(exc))

    # -- recording ---------------------------------------------------------

    async def begin_deployment(
        self,
        *,
        request: Any = None,
        deployment_id: str | None = None,
    ) -> DeploymentRecord:
        """Record a deployment as in-progress and return it.

        Called before the first stage so an interrupted run still appears in
        history. Without it, a deployment that hangs or is killed leaves no trace,
        and "it vanished" is indistinguishable from "it never ran".
        """
        record = DeploymentRecord(
            deployment_id=deployment_id or uuid.uuid4().hex,
            repository_path=getattr(request, "repository_path", None),
            image=getattr(request, "image", None),
            container=getattr(request, "container_name", None),
            status=DeploymentStatus.IN_PROGRESS,
            dry_run=bool(getattr(request, "dry_run", False)),
            request=_dump(request),
            started_at=now(),
        )
        return await self._save(record)

    async def record_deployment(
        self,
        *,
        deployment_id: str,
        request: Any = None,
        plan: Any = None,
        tasks: Any = None,
        execution: Any = None,
        verification: Any = None,
        debug: Any = None,
        incident: Any = None,
        recovery: Any = None,
        recovered: bool = False,
        stopped: bool = False,
        stop_reason: str | None = None,
        started_at: datetime | None = None,
    ) -> DeploymentRecord:
        """Fold a finished run into its record.

        A write failure here is logged, not raised: see the module docstring.
        """
        record = self._build(
            deployment_id=deployment_id,
            request=request,
            plan=plan,
            tasks=tasks,
            execution=execution,
            verification=verification,
            incident=incident,
            recovery=recovery,
            recovered=recovered,
            stopped=stopped,
            stop_reason=stop_reason,
            started_at=started_at,
        )
        record = await self._save(record)
        await self._remember(record)
        return record

    def _build(
        self,
        *,
        deployment_id: str,
        request: Any,
        plan: Any,
        tasks: Any,
        execution: Any,
        verification: Any,
        incident: Any,
        recovery: Any,
        recovered: bool,
        stopped: bool,
        stop_reason: str | None,
        started_at: datetime | None,
    ) -> DeploymentRecord:
        finished_at = now()
        started = started_at or finished_at
        actions = _dump(execution).get("actions", []) if execution is not None else []
        failures = _collect_failures(
            execution=execution,
            verification=verification,
            incident=incident,
            recovery=recovery,
        )

        status, error = _final_status(
            dry_run=bool(getattr(request, "dry_run", False)),
            stopped=stopped,
            stop_reason=stop_reason,
            verification=verification,
            recovered=recovered,
        )

        repository, commit_ref, commit_sha = _repository_identity(plan)

        return DeploymentRecord(
            deployment_id=deployment_id,
            run_id=getattr(execution, "run_id", None),
            repository=repository,
            repository_path=getattr(request, "repository_path", None),
            commit_sha=commit_sha,
            commit_ref=commit_ref,
            image=getattr(request, "image", None),
            container=getattr(request, "container_name", None),
            status=status,
            recovered=recovered,
            escalated=bool(getattr(recovery, "escalated", False)),
            escalation_reason=_text(getattr(recovery, "escalation_reason", None)),
            dry_run=bool(getattr(request, "dry_run", False)),
            error=error,
            plan=plan,
            tasks=_dump_sequence(tasks),
            actions=actions,
            verification=verification,
            failures=failures,
            recovery_attempts=_dump(recovery).get("attempts", []) if recovery else [],
            incident=incident,
            request=_dump(request),
            started_at=started,
            finished_at=finished_at,
            duration_seconds=round((finished_at - started).total_seconds(), 3),
        )

    async def _save(self, record: DeploymentRecord) -> DeploymentRecord:
        try:
            return await self._store.save_deployment(record)
        except Exception as exc:  # noqa: BLE001 - history must not fail a deployment
            logger.error(
                "failed to persist deployment history",
                extra={
                    "deployment_id": record.deployment_id,
                    "status": str(record.status),
                    "error": str(exc),
                },
                exc_info=True,
            )
            return record

    async def _remember(self, record: DeploymentRecord) -> list[Lesson]:
        lessons = derive_lessons(record)
        stored: list[Lesson] = []
        for lesson in lessons:
            try:
                stored.append(await self._store.save_lesson(lesson))
            except Exception as exc:  # noqa: BLE001 - see _save
                logger.error(
                    "failed to persist lesson",
                    extra={"fingerprint": lesson.fingerprint, "error": str(exc)},
                    exc_info=True,
                )
        return stored

    # -- reading -----------------------------------------------------------

    async def list_deployments(self, query: DeploymentQuery) -> DeploymentListResponse:
        return await self._store.list_deployments(query)

    async def get_deployment(self, deployment_id: str) -> DeploymentDetail | None:
        record = await self._store.get_deployment(deployment_id)
        if record is None:
            return None
        lessons = await self._store.list_lessons(repository=record.repository, limit=100)
        mine = [lesson for lesson in lessons if record.deployment_id in lesson.deployment_ids]
        return DeploymentDetail(**record.model_dump(), lessons=mine)

    async def list_lessons(self, *, repository: str | None = None, limit: int = 50) -> list[Lesson]:
        return await self._store.list_lessons(repository=repository, limit=limit)

    async def recall_lessons(
        self,
        *,
        repository: str | None = None,
        component: str | None = None,
        query: str | None = None,
        limit: int = 10,
    ) -> list[Lesson]:
        """Lessons worth reading before the next deployment.

        Scored rather than filtered, because a lesson from the same repository at
        zero keyword overlap is still relevant and a strict filter would drop it.
        """
        candidates = await self._store.list_lessons(repository=repository, limit=200)
        scored = [
            (
                self._matcher.score(
                    lesson, repository=repository, component=component, query=query
                ),
                lesson,
            )
            for lesson in candidates
        ]
        ranked = sorted(scored, key=lambda pair: (pair[0], pair[1].occurrences), reverse=True)
        return [lesson for score, lesson in ranked if score > 0][:limit]


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


def _final_status(
    *,
    dry_run: bool,
    stopped: bool,
    stop_reason: str | None,
    verification: Any,
    recovered: bool,
) -> tuple[DeploymentStatus, str | None]:
    """Decide what a run amounted to.

    Ordering matters. A dry run deployed nothing, so it is not a success however
    clean the plan looked. An execution that stopped never got to verification, so
    its verification result is absent and saying anything else would be a guess.
    """
    if dry_run:
        return DeploymentStatus.DRY_RUN, None

    if stopped:
        return DeploymentStatus.FAILED, stop_reason or "execution stopped before verification"

    status = _text(getattr(verification, "status", None))

    if status == "SUCCESS":
        # Recovered deployments are succeeded, but the fact is recorded separately
        # rather than folded into the status: "worked first time" and "worked after
        # a restart" are different facts about the same outcome.
        return DeploymentStatus.SUCCEEDED, None

    if status == "WARNING":
        return DeploymentStatus.DEGRADED, "verification could not confirm everything"

    if recovered:
        return DeploymentStatus.SUCCEEDED, None

    failures = getattr(verification, "failures", None) or []
    detail = failures[0] if failures else "no verification result"
    return DeploymentStatus.FAILED, str(detail)


def _repository_identity(plan: Any) -> tuple[str | None, str | None, str | None]:
    """Pull repository and commit out of a plan.

    ``ref`` is whatever the analysis resolved, which may be a branch. It is only
    reported as a sha when it actually looks like one — a branch called ``main`` is
    not a commit and recording it as one would be a lie with a hex shape.
    """
    repository = getattr(plan, "repository", None)
    if repository is None:
        return None, None, None

    full_name = getattr(repository, "full_name", None)
    ref = getattr(repository, "ref", None)
    ref = str(ref) if ref else None
    is_sha = bool(ref) and all(character in _SHA for character in ref.lower())
    return full_name, ref, (ref if is_sha else None)


# ---------------------------------------------------------------------------
# Failure collection
# ---------------------------------------------------------------------------


def _collect_failures(
    *,
    execution: Any,
    verification: Any,
    incident: Any,
    recovery: Any,
) -> list[FailureRecord]:
    """Flatten every stage's failures into one sortable list."""
    failures: list[FailureRecord] = []

    results = getattr(execution, "results", None) or []
    for result in results:
        task = getattr(result, "task", None)
        error = getattr(result, "error", None)
        if not error:
            continue
        failures.append(
            FailureRecord(
                stage=FailureStage.EXECUTE,
                kind=_text(getattr(result, "error_code", None)) or "task_failed",
                message=str(error),
                detail=getattr(task, "tool", None),
                task_id=getattr(task, "id", None),
                tool=getattr(task, "tool", None),
            )
        )

    for failure in getattr(verification, "failures", None) or []:
        failures.append(
            FailureRecord(
                stage=FailureStage.VERIFY,
                kind="verification_failed",
                message=str(failure),
            )
        )

    for cause in getattr(incident, "suspected_root_causes", None) or []:
        failures.append(
            FailureRecord(
                stage=FailureStage.INVESTIGATE,
                kind=str(getattr(cause, "id", "unknown")),
                message=str(getattr(cause, "cause", "")),
                detail=getattr(cause, "confirm", None),
            )
        )

    for attempt in getattr(recovery, "attempts", None) or []:
        error = getattr(attempt, "error", None)
        if not error:
            continue
        failures.append(
            FailureRecord(
                stage=FailureStage.RECOVER,
                kind=_text(getattr(attempt, "action", None)) or "recovery_failed",
                message=str(error),
                detail=getattr(attempt, "description", None),
            )
        )

    return failures


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _dump(value: Any) -> dict[str, Any]:
    """Model -> JSON-safe dict, tolerating None and plain dicts alike."""
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    dump = getattr(value, "model_dump", None)
    if dump is None:
        return {}
    return dump(mode="json")


def _dump_sequence(value: Any) -> list[dict[str, Any]]:
    """A list of models or dicts -> a list of JSON-safe dicts."""
    if not value:
        return []
    return [_dump(item) for item in value]


def _text(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def build_memory_service(settings: Settings | None = None) -> DeploymentMemoryService:
    """Choose a store from configuration.

    No database URL means in-memory history rather than no history: a feature that
    refuses to record anything without Postgres is a feature nobody switches on.
    """
    settings = settings or get_settings()
    database = settings.database
    url = database.url.get_secret_value() if database.url is not None else None

    if url:
        store: MemoryStore = PostgresMemoryStore(
            url,
            min_size=1,
            max_size=max(1, database.pool_size),
            connect_timeout_seconds=database.connect_timeout_seconds,
        )
    else:
        store = InMemoryMemoryStore()

    return DeploymentMemoryService(store)


__all__ = ["DeploymentMemoryService", "build_memory_service"]
