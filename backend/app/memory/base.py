"""The memory store contract.

Deployment history is persisted behind :class:`MemoryStore` so the rest of the
service depends on an interface rather than on PostgreSQL. Everything above this
line — the workflow, the API, the lesson derivation — is written against the
protocol, which is what makes the storage replaceable.

Two things are deliberately *not* here:

- **Embeddings.** Recall is keyword-based and lives behind
  :class:`LessonMatcher`. When semantic recall is wanted, it becomes another
  implementation of that protocol plus a column on ``lessons``; no caller changes.
- **Queries.** There is no general query escape hatch. A store that accepts
  arbitrary SQL stops being a boundary and becomes a second, undocumented API.

The lifecycle is ``start``/``close`` rather than lazy connection, so a store can
report its own health and a failed connection surfaces at startup instead of on
the first deployment.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.models.deployment_record import (
    DeploymentListResponse,
    DeploymentQuery,
    DeploymentRecord,
    Lesson,
    MemoryHealth,
)


@runtime_checkable
class MemoryStore(Protocol):
    """Persistence for deployment history and lessons."""

    #: Short identifier of the implementation, e.g. ``postgres``, ``memory``.
    backend: str

    async def start(self) -> None:
        """Open connections and make sure the schema exists."""

    async def close(self) -> None:
        """Release connections. Safe to call when never started."""

    async def save_deployment(self, record: DeploymentRecord) -> DeploymentRecord:
        """Insert or replace one deployment by ``deployment_id``.

        An upsert, not an append: a deployment is written more than once as its
        stages complete, and history must not accumulate a row per stage.
        """

    async def get_deployment(self, deployment_id: str) -> DeploymentRecord | None:
        """Return one deployment, or ``None`` when there is no such id."""

    async def list_deployments(self, query: DeploymentQuery) -> DeploymentListResponse:
        """Return summaries newest-first, plus the total matching ``query``."""

    async def save_lesson(self, lesson: Lesson) -> Lesson:
        """Record a lesson, merging repeats of the same fingerprint.

        Returns the stored lesson including its accumulated ``occurrences``, which
        may be higher than the one passed in.
        """

    async def list_lessons(self, *, repository: str | None = None, limit: int = 50) -> list[Lesson]:
        """Lessons by recency, optionally scoped to one repository."""

    async def health(self) -> MemoryHealth:
        """Report reachability. Must not raise; a broken store is a status."""


@runtime_checkable
class LessonMatcher(Protocol):
    """How a lesson is judged relevant to the deployment about to run.

    This is the seam for semantic recall. The shipped implementation scores on
    tokens, tags and repetition; an embedding-backed one would score on similarity
    and could be swapped in without touching a caller.
    """

    def score(
        self,
        lesson: Lesson,
        *,
        repository: str | None,
        component: str | None,
        query: str | None,
    ) -> float:
        """Return relevance in ``[0, 1]``. Higher is more relevant."""


__all__ = ["LessonMatcher", "MemoryStore"]
