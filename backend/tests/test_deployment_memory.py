"""Deployment history and lessons.

The store contract is exercised against every implementation available, so the
in-memory default cannot quietly diverge from Postgres. Postgres is included when
``AEGIS_TEST_DATABASE_URL`` is set — CI starts one, and locally:

    docker run -d --name aegis-pg-test -e POSTGRES_PASSWORD=testpw \\
      -e POSTGRES_USER=aegis -e POSTGRES_DB=aegis_test -p 55432:5432 postgres:16-alpine
    AEGIS_TEST_DATABASE_URL=postgresql://aegis:testpw@127.0.0.1:55432/aegis_test \\
      .venv/bin/python -m pytest tests/test_deployment_memory.py

Everything that does not need a database runs unconditionally, so the suite still
means something without one.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import SecretStr

from app.memory.base import LessonMatcher, MemoryStore
from app.memory.in_memory import InMemoryMemoryStore
from app.memory.lessons import derive_lessons, lesson_fingerprint, merge_lesson
from app.memory.matching import KeywordLessonMatcher
from app.memory.postgres import PostgresMemoryStore
from app.memory.service import DeploymentMemoryService, build_memory_service
from app.models.deployment_record import (
    DeploymentQuery,
    DeploymentRecord,
    DeploymentStatus,
    FailureRecord,
    FailureStage,
    Lesson,
)

pytestmark = pytest.mark.anyio

DATABASE_URL = os.environ.get("AEGIS_TEST_DATABASE_URL")


def _record(
    deployment_id: str = "d1",
    *,
    repository: str | None = "acme/api",
    status: DeploymentStatus = DeploymentStatus.FAILED,
    started_at: datetime | None = None,
    verification_status: str | None = "FAILED",
    failures: list[FailureRecord] | None = None,
    incident: Any = None,
    recovery: Any = None,
    execution: Any = None,
) -> DeploymentRecord:
    from app.models.verification import VerificationResult

    started = started_at or datetime(2026, 1, 1, tzinfo=UTC)
    return DeploymentRecord(
        deployment_id=deployment_id,
        repository=repository,
        repository_path="../examples/sample_app",
        commit_sha="a" * 40,
        commit_ref="main",
        image="acme/api:dev",
        container="acme-api",
        status=status,
        verification=VerificationResult(
            status=verification_status or "FAILED",
            failures=["container is not running"] if verification_status != "SUCCESS" else [],
        ),
        failures=failures if failures is not None else [],
        incident=incident,
        recovery_attempts=[],
        started_at=started,
        finished_at=started + timedelta(seconds=12),
        duration_seconds=12.0,
    )


@pytest.fixture(params=["memory", "postgres"])
async def store(request: pytest.FixtureRequest) -> Any:
    """Every store implementation, exercised by the same assertions.

    Parametrised rather than mocked: a mock only proves the code calls what the
    test author already expected, and the two implementations here have genuinely
    different failure modes — JSONB round-tripping, upsert conflicts, array merges.
    """
    if request.param == "memory":
        instance = InMemoryMemoryStore()
        await instance.start()
        yield instance
        await instance.close()
        return

    if not DATABASE_URL:
        pytest.skip("set AEGIS_TEST_DATABASE_URL to exercise the Postgres store")

    instance = PostgresMemoryStore(DATABASE_URL, min_size=1, max_size=2)
    await instance.start()

    # A fresh schema per test, so a previous run's rows cannot make a broken
    # implementation look correct.
    async with instance._acquire() as connection:  # noqa: SLF001 - test teardown
        await connection.execute("TRUNCATE deployments, lessons")

    yield instance
    await instance.close()


@pytest.fixture
async def service(store: MemoryStore) -> DeploymentMemoryService:
    return DeploymentMemoryService(store)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


async def test_a_deployment_round_trips(store: MemoryStore) -> None:
    saved = await store.save_deployment(_record())

    assert saved.deployment_id == "d1"
    loaded = await store.get_deployment("d1")
    assert loaded is not None
    assert loaded.repository == "acme/api"
    assert loaded.commit_sha == "a" * 40
    assert loaded.status is DeploymentStatus.FAILED
    assert loaded.verification is not None
    assert loaded.verification.status == "FAILED"


async def test_nested_structures_survive_the_round_trip(store: MemoryStore) -> None:
    """JSONB is where nested data quietly degrades.

    A stored action list that comes back as strings instead of objects still looks
    populated in a row count, and only breaks when a trace is read.
    """
    from app.models.execution import ExecutionAction

    record = _record()
    record.actions = [
        ExecutionAction(
            sequence=1,
            task_id="build",
            tool="docker.build_image",
            arguments={"tag": "acme/api:dev"},
            status="success",
            duration_seconds=4.5,
        )
    ]
    await store.save_deployment(record)

    loaded = await store.get_deployment("d1")
    assert loaded is not None
    assert len(loaded.actions) == 1
    action = loaded.actions[0]
    assert action.tool == "docker.build_image"
    assert action.arguments == {"tag": "acme/api:dev"}
    assert action.duration_seconds == 4.5


async def test_saving_the_same_id_twice_updates_rather_than_duplicates(
    store: MemoryStore,
) -> None:
    """A deployment is written once per stage; that must not become three rows."""
    await store.save_deployment(_record(status=DeploymentStatus.IN_PROGRESS))
    await store.save_deployment(_record(status=DeploymentStatus.SUCCEEDED))

    listed = await store.list_deployments(DeploymentQuery())
    assert listed.total == 1
    assert listed.items[0].status is DeploymentStatus.SUCCEEDED


async def test_the_original_created_time_survives_an_update(store: MemoryStore) -> None:
    first = await store.save_deployment(_record())
    second = await store.save_deployment(_record(status=DeploymentStatus.SUCCEEDED))

    assert second.created_at == first.created_at
    assert second.updated_at >= first.updated_at


async def test_an_unknown_id_is_none_not_an_error(store: MemoryStore) -> None:
    assert await store.get_deployment("nope") is None


async def test_a_timestamp_that_precedes_its_start_is_rejected() -> None:
    """A record whose end precedes its start is a bug, not a slow deployment."""
    with pytest.raises(ValueError, match="finished_at precedes started_at"):
        DeploymentRecord(
            deployment_id="d1",
            started_at=datetime(2026, 1, 2, tzinfo=UTC),
            finished_at=datetime(2026, 1, 1, tzinfo=UTC),
        )


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


async def test_history_is_newest_first(store: MemoryStore) -> None:
    base = datetime(2026, 1, 1, tzinfo=UTC)
    for index in range(3):
        await store.save_deployment(_record(f"d{index}", started_at=base + timedelta(hours=index)))

    listed = await store.list_deployments(DeploymentQuery())
    assert [item.deployment_id for item in listed.items] == ["d2", "d1", "d0"]
    assert listed.total == 3


async def test_listing_is_paged_and_reports_the_full_total(store: MemoryStore) -> None:
    base = datetime(2026, 1, 1, tzinfo=UTC)
    for index in range(5):
        await store.save_deployment(_record(f"d{index}", started_at=base + timedelta(hours=index)))

    page = await store.list_deployments(DeploymentQuery(limit=2, offset=0))
    assert [item.deployment_id for item in page.items] == ["d4", "d3"]
    assert page.total == 5, "total describes the whole match, not the page"

    second = await store.list_deployments(DeploymentQuery(limit=2, offset=2))
    assert [item.deployment_id for item in second.items] == ["d2", "d1"]


async def test_listing_filters_by_repository_and_status(store: MemoryStore) -> None:
    await store.save_deployment(_record("d1", repository="acme/api"))
    await store.save_deployment(_record("d2", repository="acme/other"))
    await store.save_deployment(
        _record("d3", repository="acme/api", status=DeploymentStatus.SUCCEEDED)
    )

    by_repo = await store.list_deployments(DeploymentQuery(repository="acme/api"))
    assert {item.deployment_id for item in by_repo.items} == {"d1", "d3"}

    by_status = await store.list_deployments(
        DeploymentQuery(repository="acme/api", status=DeploymentStatus.SUCCEEDED)
    )
    assert [item.deployment_id for item in by_status.items] == ["d3"]


async def test_summaries_carry_counts_without_the_weight(store: MemoryStore) -> None:
    """A list row should be readable without shipping the plan and action log."""
    from app.models.execution import ExecutionAction

    record = _record(failures=[FailureRecord(stage=FailureStage.VERIFY, kind="x", message="m")])
    record.actions = [
        ExecutionAction(sequence=1, task_id="build", tool="docker.build_image", status="success"),
        ExecutionAction(sequence=2, task_id="run", tool="docker.start_container", status="success"),
    ]
    await store.save_deployment(record)

    item = (await store.list_deployments(DeploymentQuery())).items[0]
    assert item.action_count == 2
    assert item.failure_count == 1
    assert "actions" not in type(item).model_fields
    assert "plan" not in type(item).model_fields


async def test_the_page_size_is_capped() -> None:
    """An unbounded history endpoint is a slow query wearing a costume."""
    with pytest.raises(ValueError):
        DeploymentQuery(limit=5000)


# ---------------------------------------------------------------------------
# Lessons
# ---------------------------------------------------------------------------


def _lesson(fingerprint: str = "fp1", **overrides: Any) -> Lesson:
    base = {
        "lesson_id": f"lsn_{fingerprint[:8]}",
        "fingerprint": fingerprint,
        "title": "out_of_memory: application",
        "summary": "The process exceeded its memory limit and was killed.",
        "repository": "acme/api",
        "component": "application",
        "cause_id": "out_of_memory",
        "severity": "critical",
        "evidence": ["exit_code=137, oom_killed=true"],
        "recommendation": "Restart the container and raise the memory limit.",
        "tags": ["out_of_memory", "application"],
    }
    base.update(overrides)
    return Lesson(**base)


async def test_a_repeated_lesson_accumulates_rather_than_duplicates(
    store: MemoryStore,
) -> None:
    first = await store.save_lesson(_lesson())
    second = await store.save_lesson(_lesson())
    third = await store.save_lesson(_lesson())

    assert first.occurrences == 1
    assert second.occurrences == 2
    assert third.occurrences == 3

    stored = await store.list_lessons()
    assert len(stored) == 1, "the same lesson must not become three rows"
    assert stored[0].occurrences == 3


async def test_a_repeat_keeps_the_first_seen_time(store: MemoryStore) -> None:
    first = await store.save_lesson(_lesson())
    await store.save_lesson(_lesson())

    stored = (await store.list_lessons())[0]
    assert stored.first_seen_at == first.first_seen_at
    assert stored.last_seen_at >= first.first_seen_at


async def test_a_repeat_tracks_which_deployments_saw_it(store: MemoryStore) -> None:
    await store.save_lesson(_lesson(deployment_ids=["d1"]))
    await store.save_lesson(_lesson(deployment_ids=["d2"]))

    stored = (await store.list_lessons())[0]
    assert sorted(stored.deployment_ids) == ["d1", "d2"]


async def test_different_causes_are_different_lessons(store: MemoryStore) -> None:
    await store.save_lesson(_lesson("fp1", cause_id="out_of_memory"))
    await store.save_lesson(_lesson("fp2", cause_id="port_conflict"))

    assert len(await store.list_lessons()) == 2


async def test_fingerprints_are_stable_across_casing_and_whitespace() -> None:
    assert lesson_fingerprint("Out_Of_Memory", "ACME/API", " Application ") == lesson_fingerprint(
        "out_of_memory", "acme/api", "application"
    )


def test_merging_a_new_lesson_stamps_the_timestamps() -> None:
    merged = merge_lesson(None, _lesson())
    assert merged.created_at is not None
    assert merged.first_seen_at is not None


def test_only_failures_produce_lessons() -> None:
    """A deployment that worked teaches nothing worth storing."""
    record = _record(status=DeploymentStatus.SUCCEEDED, verification_status="SUCCESS")
    assert derive_lessons(record) == []


def test_the_same_cause_in_one_deployment_is_one_lesson() -> None:
    """Six failed tasks for one reason is one thing that happened six times."""
    failures = [
        FailureRecord(
            stage=FailureStage.EXECUTE,
            kind="out_of_memory",
            message="Killed process 1",
            task_id=f"t{index}",
        )
        for index in range(6)
    ]
    lessons = derive_lessons(_record(failures=failures))
    assert len(lessons) == 1


def test_an_unrecognised_failure_still_produces_a_lesson() -> None:
    """Exactly the cases where a signature needs adding are the ones to keep."""
    failures = [
        FailureRecord(
            stage=FailureStage.EXECUTE, kind="something_new", message="no idea what this is"
        )
    ]
    lessons = derive_lessons(_record(failures=failures))
    assert [lesson.cause_id for lesson in lessons] == ["something_new"]


def test_a_lesson_carries_the_evidence_behind_it() -> None:
    lessons = derive_lessons(
        _record(failures=[FailureRecord(stage=FailureStage.VERIFY, kind="port", message="no port")])
    )
    assert lessons[0].evidence == ["no port"]


# ---------------------------------------------------------------------------
# Recall
# ---------------------------------------------------------------------------


class _StubMatcher(LessonMatcher):
    """Ranks by call order, so the service's use of a matcher is observable."""

    def __init__(self, scores: dict[str, float]) -> None:
        self.scores = scores
        self.calls: list[str | None] = []

    def score(
        self,
        lesson: Lesson,
        *,
        repository: str | None,
        component: str | None,
        query: str | None,
    ) -> float:
        self.calls.append(query)
        return self.scores.get(lesson.fingerprint, 0.0)


async def test_recall_uses_the_matcher_rather_than_filtering(
    store: MemoryStore,
) -> None:
    """Scoring, not matching: a lesson with no keyword overlap is still relevant."""
    matcher = _StubMatcher({"low": 0.1, "high": 0.9, "none": 0.0})
    service = DeploymentMemoryService(store, matcher=matcher)
    await store.save_lesson(_lesson("high", title="High"))
    await store.save_lesson(_lesson("low", title="Low"))
    await store.save_lesson(_lesson("none", title="None"))

    recalled = await service.recall_lessons(repository="acme/api", limit=10)

    assert [lesson.fingerprint for lesson in recalled] == ["high", "low"]
    assert "none" not in [lesson.fingerprint for lesson in recalled]


async def test_recall_respects_its_limit(store: MemoryStore) -> None:
    for index in range(5):
        await store.save_lesson(_lesson(f"fp{index}"))

    recalled = await DeploymentMemoryService(store).recall_lessons(repository="acme/api", limit=2)
    assert len(recalled) == 2


async def test_recall_with_no_context_returns_nothing(store: MemoryStore) -> None:
    """A lesson only counts as relevant when something about the run matches it.

    Returning every lesson when the caller says nothing would flood the next
    deployment with warnings about code it has never run.
    """
    await store.save_lesson(_lesson())

    assert await DeploymentMemoryService(store).recall_lessons() == []


def test_the_keyword_matcher_prefers_the_same_repository() -> None:
    matcher = KeywordLessonMatcher()
    same = matcher.score(
        _lesson(repository="acme/api"), repository="acme/api", component=None, query=None
    )
    other = matcher.score(
        _lesson(repository="acme/other"), repository="acme/api", component=None, query=None
    )
    assert same > other


def test_the_keyword_matcher_scores_within_range() -> None:
    matcher = KeywordLessonMatcher()
    for repository in ("acme/api", "other/repo"):
        score = matcher.score(
            _lesson(repository=repository),
            repository="acme/api",
            component="application",
            query="out of memory killed the process",
        )
        assert 0.0 <= score <= 1.0


def test_a_much_repeated_lesson_scores_higher() -> None:
    """Nine occurrences is a stronger warning than one."""
    matcher = KeywordLessonMatcher()
    once = matcher.score(
        _lesson(occurrences=1), repository=None, component=None, query="out of memory"
    )
    often = matcher.score(
        _lesson(occurrences=9), repository=None, component=None, query="out of memory"
    )
    assert often > once


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


def _request(dry_run: bool = False) -> Any:
    from app.models.docker import DockerDeployRequest

    return DockerDeployRequest(
        repository_path="../examples/sample_app",
        image="acme/api:dev",
        container_name="acme-api",
        approve=True,
        approval_reference="tester",
        dry_run=dry_run,
    )


async def test_a_verified_deployment_is_recorded_as_succeeded(service: Any) -> None:
    from app.models.verification import VerificationResult

    record = await service.record_deployment(
        deployment_id="d1",
        request=_request(),
        verification=VerificationResult(status="SUCCESS"),
    )
    assert record.status is DeploymentStatus.SUCCEEDED
    assert record.recovered is False


async def test_a_recovered_deployment_is_succeeded_and_says_so(service: Any) -> None:
    """ "Worked first time" and "worked after a restart" are different facts."""
    from app.models.verification import VerificationResult

    record = await service.record_deployment(
        deployment_id="d1",
        request=_request(),
        verification=VerificationResult(status="SUCCESS"),
        recovered=True,
    )
    assert record.status is DeploymentStatus.SUCCEEDED
    assert record.recovered is True


async def test_a_warning_is_degraded_not_succeeded(service: Any) -> None:
    from app.models.verification import VerificationResult

    record = await service.record_deployment(
        deployment_id="d1",
        request=_request(),
        verification=VerificationResult(status="WARNING"),
    )
    assert record.status is DeploymentStatus.DEGRADED


async def test_a_stopped_execution_fails_without_claiming_verification(
    service: Any,
) -> None:
    record = await service.record_deployment(
        deployment_id="d1",
        request=_request(),
        stopped=True,
        stop_reason="docker.build_image failed",
    )
    assert record.status is DeploymentStatus.FAILED
    assert "docker.build_image" in record.error


async def test_a_dry_run_is_never_a_success(service: Any) -> None:
    """It deployed nothing, so a clean plan is not a working deployment."""
    from app.models.verification import VerificationResult

    record = await service.record_deployment(
        deployment_id="d1",
        request=_request(dry_run=True),
        verification=VerificationResult(status="SUCCESS"),
    )
    assert record.status is DeploymentStatus.DRY_RUN


async def test_failures_from_every_stage_are_collected(service: Any) -> None:
    """Four stages report failure differently; a trace should show them together."""
    from app.models.execution import ExecutionResponse, TaskResult
    from app.models.execution import Task as TaskModel
    from app.models.incident import IncidentReport, Observation, SuspectedRootCause
    from app.models.self_healing import (
        FixCategory,
        FixRisk,
        RecoveryAttempt,
        RecoveryOutcome,
    )
    from app.models.verification import VerificationResult

    execution = ExecutionResponse(
        run_id="r1",
        status="failed",
        results=[
            TaskResult(
                task=TaskModel(id="build", title="Build", tool="docker.build_image"),
                status="failed",
                error="build failed",
                error_code="tool_error",
            )
        ],
    )
    incident = IncidentReport(
        incident_id="i1",
        symptom="s",
        confidence="high",
        affected_component="application",
        suspected_root_causes=[
            SuspectedRootCause(
                id="out_of_memory",
                cause="killed",
                component="application",
                evidence=[Observation(source="docker.container_status")],
            )
        ],
        next_action="restart the container",
    )
    recovery = RecoveryOutcome(
        attempts=[
            RecoveryAttempt(
                index=1,
                action="restart_container",
                category=FixCategory.RESTART_CONTAINER,
                risk=FixRisk.SAFE,
                description="restart",
                applied=True,
                error="start failed",
            )
        ]
    )

    record = await service.record_deployment(
        deployment_id="d1",
        request=_request(),
        execution=execution,
        verification=VerificationResult(status="FAILED", failures=["container not running"]),
        incident=incident,
        recovery=recovery,
    )

    stages = {failure.stage for failure in record.failures}
    assert stages == {
        FailureStage.EXECUTE,
        FailureStage.VERIFY,
        FailureStage.INVESTIGATE,
        FailureStage.RECOVER,
    }


async def test_the_service_reports_whether_the_deployment_worked(service: Any) -> None:
    """A deployment that never recovered is not allowed to look recovered."""
    from app.models.verification import VerificationResult

    record = await service.record_deployment(
        deployment_id="d1",
        request=_request(),
        verification=VerificationResult(status="FAILED", failures=["still down"]),
    )
    assert record.status is DeploymentStatus.FAILED
    assert record.recovered is False


async def test_beginning_a_deployment_records_it_as_in_progress(service: Any) -> None:
    """An interrupted run must appear in history rather than be absent from it."""
    record = await service.begin_deployment(request=_request())
    assert record.status is DeploymentStatus.IN_PROGRESS
    assert record.terminal is False

    stored = await service.get_deployment(record.deployment_id)
    assert stored is not None
    assert stored.image == "acme/api:dev"


async def test_recording_replaces_the_in_progress_row(service: Any) -> None:
    from app.models.verification import VerificationResult

    record = await service.begin_deployment(request=_request())
    await service.record_deployment(
        deployment_id=record.deployment_id,
        request=_request(),
        verification=VerificationResult(status="SUCCESS"),
    )

    detail = await service.get_deployment(record.deployment_id)
    assert detail is not None
    assert detail.status is DeploymentStatus.SUCCEEDED
    assert detail.terminal is True

    listed = await service.list_deployments(DeploymentQuery())
    assert listed.total == 1


async def test_a_detail_carries_the_lessons_that_deployment_produced(service: Any) -> None:
    from app.models.verification import VerificationResult

    record = await service.record_deployment(
        deployment_id="d1",
        request=_request(),
        verification=VerificationResult(status="FAILED", failures=["port 8080 in use"]),
    )
    assert record.failures, "the fixture must actually fail to produce a lesson"

    detail = await service.get_deployment("d1")
    assert detail is not None
    assert [lesson.cause_id for lesson in detail.lessons] == ["verification_failed"]
    assert detail.lessons[0].deployment_ids == ["d1"]
    assert detail.lessons[0].occurrences == 1


async def test_an_unknown_deployment_is_none(service: Any) -> None:
    assert await service.get_deployment("nope") is None


async def test_a_failing_store_does_not_fail_the_deployment() -> None:
    """A database outage must not refuse a deployment that otherwise succeeded.

    Losing a history row is recoverable; refusing to deploy because a ledger is
    down is not. The failure is logged loudly, not raised.
    """

    class BrokenStore(InMemoryMemoryStore):
        async def save_deployment(self, record: DeploymentRecord) -> DeploymentRecord:
            raise RuntimeError("database is on fire")

    service = DeploymentMemoryService(BrokenStore())

    from app.models.verification import VerificationResult

    record = await service.record_deployment(
        deployment_id="d1",
        request=_request(),
        verification=VerificationResult(status="SUCCESS"),
    )
    assert record.status is DeploymentStatus.SUCCEEDED


async def test_health_reports_a_broken_store_instead_of_raising() -> None:
    """Health is a status, not an exception. A caller must be able to read it."""

    class BrokenStore(InMemoryMemoryStore):
        async def health(self) -> Any:
            raise RuntimeError("unreachable")

    health = await DeploymentMemoryService(BrokenStore()).health()

    assert health.status == "error"
    assert "unreachable" in (health.detail or "")


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


def test_no_database_url_means_in_memory_history() -> None:
    """A history feature that refuses to run without Postgres is one nobody enables."""
    from app.core.config import Settings

    service = build_memory_service(Settings())
    assert service.backend == "memory"


def test_a_database_url_means_postgres() -> None:
    from app.core.config import Settings

    settings = Settings()
    settings.database.url = SecretStr("postgresql://u:p@localhost:5432/db")
    assert build_memory_service(settings).backend == "postgres"


def test_a_database_password_never_reaches_a_log_line() -> None:
    """A DSN carries a password, so it must never be logged verbatim."""
    from app.memory.postgres import _redact_dsn

    redacted = _redact_dsn("postgresql://aegis:hunter2@db.internal:5432/aegis")
    assert "hunter2" not in redacted
    assert "db.internal" in redacted


async def test_the_postgres_store_refuses_to_be_used_before_start() -> None:
    if not DATABASE_URL:
        pytest.skip("set AEGIS_TEST_DATABASE_URL to exercise the Postgres store")

    store = PostgresMemoryStore(DATABASE_URL)
    with pytest.raises(RuntimeError, match="before start"):
        await store.list_deployments(DeploymentQuery())
