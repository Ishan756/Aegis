"""In-memory implementation of :class:`~app.memory.base.MemoryStore`.

The default. Aegis has to be useful with no database configured — a fresh checkout,
CI, a laptop mid-feature — and a history feature that refuses to start without
Postgres is a history feature nobody turns on.

Semantics deliberately match :mod:`app.memory.postgres` exactly: same upsert
behaviour, same lesson merging by fingerprint, same ordering and filtering. That
equivalence is what makes the in-memory store a legitimate test double rather than
a test double that behaves differently and hides bugs; the shared suite runs
against both.

Not durable. Process-local and bounded, so a long-lived process cannot grow
without limit.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from app.memory.lessons import merge_lesson, now
from app.models.deployment_record import (
    DeploymentListResponse,
    DeploymentQuery,
    DeploymentRecord,
    DeploymentSummary,
    Lesson,
    MemoryHealth,
)

#: Most deployments retained before the oldest are dropped. Bounds memory in a
#: long-lived process; the Postgres store has no such limit.
DEFAULT_CAPACITY = 500


def _now() -> datetime:
    return now()


class InMemoryMemoryStore:
    """Process-local store, safe for concurrent use within one event loop."""

    backend = "memory"

    def __init__(self, capacity: int = DEFAULT_CAPACITY) -> None:
        self._deployments: dict[str, DeploymentRecord] = {}
        self._lessons: dict[str, Lesson] = {}
        self._capacity = capacity
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        """Nothing to open."""

    async def close(self) -> None:
        """Drop everything, so a restarted process does not inherit stale history."""

        async with self._lock:
            self._deployments.clear()
            self._lessons.clear()

    async def save_deployment(self, record: DeploymentRecord) -> DeploymentRecord:
        async with self._lock:
            existing = self._deployments.get(record.deployment_id)
            # The first write owns created_at. A deployment is written again as
            # each stage completes, and resetting the stamp would turn "how long
            # ago did this run" into "how long since its last stage".
            created_at = (existing.created_at if existing else None) or record.created_at or _now()
            stored = record.model_copy(update={"created_at": created_at, "updated_at": _now()})
            self._deployments[stored.deployment_id] = stored
            self._evict_oldest_deployments()
            return stored

    async def get_deployment(self, deployment_id: str) -> DeploymentRecord | None:
        async with self._lock:
            return self._deployments.get(deployment_id)

    async def list_deployments(self, query: DeploymentQuery) -> DeploymentListResponse:
        async with self._lock:
            records = list(self._deployments.values())

        if query.repository:
            wanted = query.repository.lower()
            records = [
                record
                for record in records
                if record.repository
                and record.repository.lower() == wanted
                or (record.repository_path and record.repository_path == query.repository)
            ]
        if query.status is not None:
            records = [record for record in records if record.status is query.status]

        # Newest first, with the deployment id as a tie-break so the order is total
        # even when timestamps collide.
        records.sort(
            key=lambda record: (
                record.started_at or record.created_at or datetime.min.replace(tzinfo=UTC),
                record.deployment_id,
            ),
            reverse=True,
        )

        total = len(records)
        page = records[query.offset : query.offset + query.limit]
        return DeploymentListResponse(
            items=[DeploymentSummary.of(record) for record in page],
            total=total,
            limit=query.limit,
            offset=query.offset,
        )

    async def save_lesson(self, lesson: Lesson) -> Lesson:
        async with self._lock:
            existing = self._lessons.get(lesson.fingerprint)
            merged = merge_lesson(existing, lesson)
            self._lessons[merged.fingerprint] = merged
            return merged

    async def list_lessons(self, *, repository: str | None = None, limit: int = 50) -> list[Lesson]:
        async with self._lock:
            lessons = list(self._lessons.values())

        if repository:
            wanted = repository.lower()
            lessons = [
                lesson
                for lesson in lessons
                if lesson.repository and lesson.repository.lower() == wanted
            ]

        lessons.sort(
            key=lambda lesson: (
                lesson.last_seen_at or lesson.created_at or datetime.min.replace(tzinfo=UTC),
                lesson.occurrences,
            ),
            reverse=True,
        )
        return lessons[:limit]

    async def health(self) -> MemoryHealth:
        return MemoryHealth(
            backend=self.backend,
            status="ok",
            detail=(
                "In-memory history; not durable. Set AEGIS_DATABASE__URL to persist it."
                if len(self._deployments) < self._capacity
                else "In-memory history at capacity; oldest deployments are dropped."
            ),
            deployments=len(self._deployments),
            lessons=len(self._lessons),
        )

    def _evict_oldest_deployments(self) -> None:
        if len(self._deployments) <= self._capacity:
            return
        ordered = sorted(
            self._deployments.values(),
            key=lambda record: (
                record.started_at or record.created_at or datetime.min.replace(tzinfo=UTC)
            ),
        )
        for record in ordered[: len(self._deployments) - self._capacity]:
            self._deployments.pop(record.deployment_id, None)


__all__ = ["InMemoryMemoryStore"]
