"""PostgreSQL implementation of :class:`~app.memory.base.MemoryStore`.

Design choices worth stating, because each one is a trade:

- **JSONB for nested structures, columns for the scalars.** A deployment is read
  as a document — plan, actions, verification, recovery — and normalised rows for
  each would buy nothing but joins nobody writes. The scalars that get filtered
  and sorted on are real columns with indexes.
- **Upsert by ``deployment_id``.** A deployment is written several times as its
  stages complete. Appending would produce a row per stage.
- **Lessons merge on a stable fingerprint**, so the same root cause in the same
  repository is one row with a count, not N near-identical rows.
- **Counters accumulate in SQL**, not from a read-merge-write, so two concurrent
  recordings of the same lesson cannot lose an occurrence to a race. The prose
  fields are merged through the same helper the in-memory store uses, so both
  backends produce identical lessons.
- **The DDL is idempotent and applied on start.** There is no migration runner;
  adding one is a bigger commitment than this stage warrants, but the schema
  version is recorded so that adding one later is possible.

The driver is imported lazily so the rest of the service works without it.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from app.memory.lessons import merge_lesson
from app.models.deployment_record import (
    DeploymentListResponse,
    DeploymentQuery,
    DeploymentRecord,
    DeploymentSummary,
    Lesson,
    MemoryHealth,
)

logger = logging.getLogger(__name__)

#: Bumped whenever SCHEMA changes. Recorded in ``memory_schema`` so an existing
#: database can be told apart from one created by this version.
SCHEMA_VERSION = 1

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS memory_schema (
    version     INTEGER     PRIMARY KEY,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS deployments (
    deployment_id      TEXT        PRIMARY KEY,
    run_id             TEXT,
    repository         TEXT,
    repository_path    TEXT,
    commit_sha         TEXT,
    commit_ref         TEXT,
    image              TEXT,
    container          TEXT,
    status             TEXT        NOT NULL,
    recovered          BOOLEAN     NOT NULL DEFAULT FALSE,
    escalated          BOOLEAN     NOT NULL DEFAULT FALSE,
    escalation_reason  TEXT,
    dry_run            BOOLEAN     NOT NULL DEFAULT FALSE,
    error              TEXT,
    action_count       INTEGER     NOT NULL DEFAULT 0,
    failure_count      INTEGER     NOT NULL DEFAULT 0,
    plan               JSONB,
    tasks              JSONB       NOT NULL DEFAULT '[]'::jsonb,
    actions            JSONB       NOT NULL DEFAULT '[]'::jsonb,
    verification       JSONB,
    failures           JSONB       NOT NULL DEFAULT '[]'::jsonb,
    recovery_attempts  JSONB       NOT NULL DEFAULT '[]'::jsonb,
    incident           JSONB,
    request            JSONB,
    started_at         TIMESTAMPTZ,
    finished_at        TIMESTAMPTZ,
    duration_seconds   DOUBLE PRECISION,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT deployments_status_check
        CHECK (status IN ('in_progress', 'succeeded', 'degraded', 'failed', 'dry_run'))
);

-- Newest-first listing is the default read, so it gets the leading index.
CREATE INDEX IF NOT EXISTS deployments_started_at_idx ON deployments (started_at DESC);
CREATE INDEX IF NOT EXISTS deployments_repository_idx ON deployments (repository);
CREATE INDEX IF NOT EXISTS deployments_status_idx ON deployments (status);

CREATE TABLE IF NOT EXISTS lessons (
    lesson_id     TEXT        PRIMARY KEY,
    fingerprint   TEXT        NOT NULL UNIQUE,
    title         TEXT        NOT NULL,
    summary       TEXT        NOT NULL,
    detail        TEXT,
    repository    TEXT,
    component     TEXT,
    cause_id      TEXT,
    severity      TEXT        NOT NULL DEFAULT 'warning',
    evidence      JSONB       NOT NULL DEFAULT '[]'::jsonb,
    recommendation TEXT,
    tags          TEXT[]      NOT NULL DEFAULT '{}',
    occurrences   INTEGER     NOT NULL DEFAULT 1,
    deployment_ids TEXT[]     NOT NULL DEFAULT '{}',
    first_seen_at TIMESTAMPTZ,
    last_seen_at  TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT lessons_severity_check
        CHECK (severity IN ('info', 'warning', 'critical'))
);

CREATE INDEX IF NOT EXISTS lessons_repository_idx ON lessons (repository);
CREATE INDEX IF NOT EXISTS lessons_cause_idx ON lessons (cause_id);
CREATE INDEX IF NOT EXISTS lessons_last_seen_idx ON lessons (last_seen_at DESC);
CREATE INDEX IF NOT EXISTS lessons_tags_idx ON lessons USING GIN (tags);

INSERT INTO memory_schema (version) VALUES (1) ON CONFLICT (version) DO NOTHING;
"""

_DEPLOYMENT_COLUMNS = """
    deployment_id, run_id, repository, repository_path, commit_sha, commit_ref,
    image, container, status, recovered, escalated, escalation_reason, dry_run,
    error, action_count, failure_count, plan, tasks, actions, verification,
    failures, recovery_attempts, incident, request, started_at, finished_at,
    duration_seconds, created_at, updated_at
"""

_LESSON_COLUMNS = """
    lesson_id, fingerprint, title, summary, detail, repository, component,
    cause_id, severity, evidence, recommendation, tags, occurrences,
    deployment_ids, first_seen_at, last_seen_at, created_at, updated_at
"""


def _redact_dsn(url: str) -> str:
    """A loggable form of a DSN. A password must never reach a log line."""
    parts = urlsplit(url)
    if not parts.scheme or "@" not in parts.netloc:
        return url
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, f"***@{host}", parts.path, parts.query, parts.fragment))


class PostgresMemoryStore:
    """Durable history in PostgreSQL."""

    backend = "postgres"

    def __init__(
        self,
        dsn: str,
        *,
        min_size: int = 1,
        max_size: int = 5,
        connect_timeout_seconds: float = 10.0,
    ) -> None:
        self._dsn = dsn
        self._min_size = min_size
        self._max_size = max_size
        self._connect_timeout = connect_timeout_seconds
        self._pool: Any = None

    async def start(self) -> None:
        try:
            import asyncpg
        except ImportError as exc:  # pragma: no cover - depends on the environment
            raise RuntimeError(
                "asyncpg is required for the Postgres memory store. Install the "
                "'storage' extra or leave AEGIS_DATABASE__URL unset to use in-memory history."
            ) from exc

        self._pool = await asyncpg.create_pool(
            dsn=self._dsn,
            min_size=self._min_size,
            max_size=self._max_size,
            timeout=self._connect_timeout,
            # jsonb is decoded into Python objects rather than a string, so the
            # models receive what they expect.
            init=_init_connection,
        )

        async with self._pool.acquire() as connection:
            await connection.execute(SCHEMA_SQL)

        logger.info("memory store ready backend=%s dsn=%s", self.backend, _redact_dsn(self._dsn))

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def save_deployment(self, record: DeploymentRecord) -> DeploymentRecord:
        payload = record.model_dump(mode="json")
        args = _deployment_args(payload)
        row = (
            f"INSERT INTO deployments ({_DEPLOYMENT_COLUMNS}) VALUES ("
            # Generated from the argument count. Hand-written placeholder lists
            # drift from the column list the moment a field is added, and the
            # resulting SQL error only appears once a real database runs it.
            f"{_placeholders(len(args))}, now(), now()"
            ") ON CONFLICT (deployment_id) DO UPDATE SET "
            "run_id = EXCLUDED.run_id, repository = EXCLUDED.repository, "
            "repository_path = EXCLUDED.repository_path, commit_sha = EXCLUDED.commit_sha, "
            "commit_ref = EXCLUDED.commit_ref, image = EXCLUDED.image, "
            "container = EXCLUDED.container, status = EXCLUDED.status, "
            "recovered = EXCLUDED.recovered, escalated = EXCLUDED.escalated, "
            "escalation_reason = EXCLUDED.escalation_reason, dry_run = EXCLUDED.dry_run, "
            "error = EXCLUDED.error, action_count = EXCLUDED.action_count, "
            "failure_count = EXCLUDED.failure_count, plan = EXCLUDED.plan, "
            "tasks = EXCLUDED.tasks, actions = EXCLUDED.actions, "
            "verification = EXCLUDED.verification, "
            "failures = EXCLUDED.failures, "
            "recovery_attempts = EXCLUDED.recovery_attempts, incident = EXCLUDED.incident, "
            "request = EXCLUDED.request, started_at = EXCLUDED.started_at, "
            "finished_at = EXCLUDED.finished_at, "
            "duration_seconds = EXCLUDED.duration_seconds, updated_at = now() "
            "RETURNING created_at, updated_at"
        )
        async with self._acquire() as connection:
            stamps = await connection.fetchrow(row, *args)
        return record.model_copy(update=dict(stamps))

    async def get_deployment(self, deployment_id: str) -> DeploymentRecord | None:
        async with self._acquire() as connection:
            row = await connection.fetchrow(
                f"SELECT {_DEPLOYMENT_COLUMNS} FROM deployments WHERE deployment_id = $1",
                deployment_id,
            )
        # A miss is an ordinary outcome, not an error.
        return _deployment_from_row(row) if row is not None else None

    async def list_deployments(self, query: DeploymentQuery) -> DeploymentListResponse:
        conditions: list[str] = []
        args: list[Any] = []
        if query.repository:
            args.append(query.repository)
            # The path is matched exactly: it is caller-supplied, not normalised.
            conditions.append(f"(repository = ${len(args)} OR repository_path = ${len(args)})")
        if query.status is not None:
            args.append(str(query.status))
            conditions.append(f"status = ${len(args)}")

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

        # The count is over the whole match, so it must not see the paging
        # arguments — passing them here makes Postgres reject the query.
        filters = list(args)
        args.append(query.limit)
        limit_index = len(args)
        args.append(query.offset)
        offset_index = len(args)

        async with self._acquire() as connection:
            total = await connection.fetchval(f"SELECT count(*) FROM deployments {where}", *filters)
            rows = await connection.fetch(
                f"SELECT {_DEPLOYMENT_COLUMNS} FROM deployments {where} "
                # deployment_id breaks ties so paging is stable when timestamps
                # collide, which they will at second resolution.
                f"ORDER BY COALESCE(started_at, created_at) DESC, deployment_id DESC "
                f"LIMIT ${limit_index} OFFSET ${offset_index}",
                *args,
            )

        return DeploymentListResponse(
            items=[DeploymentSummary.of(_deployment_from_row(row)) for row in rows],
            total=total or 0,
            limit=query.limit,
            offset=query.offset,
        )

    async def save_lesson(self, lesson: Lesson) -> Lesson:
        async with self._acquire() as connection, connection.transaction():
            existing = await connection.fetchrow(
                f"SELECT {_LESSON_COLUMNS} FROM lessons WHERE fingerprint = $1",
                lesson.fingerprint,
            )
            # The same merge helper the in-memory store uses, so both backends
            # produce identical lessons from identical inputs.
            merged = merge_lesson(_lesson_from_row(existing), lesson)

            payload = merged.model_dump(mode="json")
            # `occurrences` and `deployment_ids` accumulate in SQL rather than from
            # the merged values, so two concurrent recordings of the same lesson
            # cannot lose a count to a read-modify-write race. The merged payload
            # supplies the prose fields only.
            args = _lesson_args(payload)
            row = await connection.fetchrow(
                f"INSERT INTO lessons ({_LESSON_COLUMNS}) VALUES ("
                f"{_placeholders(len(args))}, now(), now()"
                ") ON CONFLICT (fingerprint) DO UPDATE SET "
                "title = EXCLUDED.title, summary = EXCLUDED.summary, "
                "detail = EXCLUDED.detail, recommendation = EXCLUDED.recommendation, "
                "evidence = EXCLUDED.evidence, tags = EXCLUDED.tags, "
                "occurrences = lessons.occurrences + 1, "
                "last_seen_at = now(), updated_at = now(), "
                "deployment_ids = ("
                "  SELECT COALESCE(array_agg(id), '{}') FROM ("
                "    SELECT DISTINCT unnest("
                "      lessons.deployment_ids || EXCLUDED.deployment_ids"
                "    ) AS id"
                "  ) AS merged_ids LIMIT 25"
                ") "
                "RETURNING occurrences, first_seen_at, last_seen_at, created_at, "
                "updated_at, deployment_ids",
                *args,
            )

        return merged.model_copy(
            update={
                "occurrences": row["occurrences"],
                "first_seen_at": row["first_seen_at"],
                "last_seen_at": row["last_seen_at"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "deployment_ids": list(row["deployment_ids"] or []),
            }
        )

    async def list_lessons(self, *, repository: str | None = None, limit: int = 50) -> list[Lesson]:
        async with self._acquire() as connection:
            if repository:
                rows = await connection.fetch(
                    f"SELECT {_LESSON_COLUMNS} FROM lessons WHERE repository = $1 "
                    "ORDER BY last_seen_at DESC, occurrences DESC LIMIT $2",
                    repository,
                    limit,
                )
            else:
                rows = await connection.fetch(
                    f"SELECT {_LESSON_COLUMNS} FROM lessons "
                    "ORDER BY last_seen_at DESC, occurrences DESC LIMIT $1",
                    limit,
                )
        return [_lesson_from_row(row) for row in rows]

    async def health(self) -> MemoryHealth:
        try:
            async with self._acquire() as connection:
                deployments = await connection.fetchval("SELECT count(*) FROM deployments")
                lessons = await connection.fetchval("SELECT count(*) FROM lessons")
        except Exception as exc:  # noqa: BLE001 - health must not raise
            return MemoryHealth(backend=self.backend, status="error", detail=str(exc))
        return MemoryHealth(
            backend=self.backend,
            status="ok",
            detail=f"Connected to {_redact_dsn(self._dsn)}",
            deployments=deployments,
            lessons=lessons,
        )

    def _acquire(self) -> Any:
        if self._pool is None:
            raise RuntimeError("memory store used before start()")
        return self._pool.acquire()


async def _init_connection(connection: Any) -> None:
    """Decode jsonb into Python objects instead of leaving it as text."""
    await connection.set_type_codec(
        "jsonb",
        encoder=json.dumps,
        decoder=json.loads,
        schema="pg_catalog",
    )


def _tstz(value: str | None) -> datetime | None:
    """Recover a datetime from the ISO string ``model_dump(mode="json")`` produces.

    The JSONB columns need the stringified payload, but a TIMESTAMPTZ column needs
    a real datetime — asyncpg rejects a string rather than coercing it.
    """
    return datetime.fromisoformat(value) if value else None


def _placeholders(count: int) -> str:
    """``$1, $2, ...`` for ``count`` bound values."""
    return ", ".join(f"${index}" for index in range(1, count + 1))


def _deployment_args(payload: dict[str, Any]) -> tuple[Any, ...]:
    return (
        payload["deployment_id"],
        payload["run_id"],
        payload["repository"],
        payload["repository_path"],
        payload["commit_sha"],
        payload["commit_ref"],
        payload["image"],
        payload["container"],
        payload["status"],
        payload["recovered"],
        payload["escalated"],
        payload["escalation_reason"],
        payload["dry_run"],
        payload["error"],
        len(payload["actions"]),
        len(payload["failures"]),
        payload["plan"],
        payload["tasks"],
        payload["actions"],
        payload["verification"],
        payload["failures"],
        payload["recovery_attempts"],
        payload["incident"],
        payload["request"],
        _tstz(payload["started_at"]),
        _tstz(payload["finished_at"]),
        payload["duration_seconds"],
    )


def _lesson_args(payload: dict[str, Any]) -> tuple[Any, ...]:
    return (
        payload["lesson_id"],
        payload["fingerprint"],
        payload["title"],
        payload["summary"],
        payload["detail"],
        payload["repository"],
        payload["component"],
        payload["cause_id"],
        payload["severity"],
        payload["evidence"],
        payload["recommendation"],
        payload["tags"],
        payload["occurrences"],
        payload["deployment_ids"],
        _tstz(payload["first_seen_at"]),
        _tstz(payload["last_seen_at"]),
    )


def _deployment_from_row(row: Any) -> DeploymentRecord:
    return DeploymentRecord.model_validate(dict(row))


def _lesson_from_row(row: Any) -> Lesson | None:
    if row is None:
        return None
    data = dict(row)
    data["deployment_ids"] = list(data.get("deployment_ids") or [])
    return Lesson.model_validate(data)


__all__ = ["SCHEMA_SQL", "SCHEMA_VERSION", "PostgresMemoryStore"]
