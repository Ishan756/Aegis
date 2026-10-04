"""Lesson identity, merging and derivation.

Shared by both memory stores and the recording service, because these are the rules
that decide what "the same lesson" means. When two backends each had their own copy,
a lesson could read differently depending on which one was configured — and a
history that changes shape with your infrastructure is not one anybody trusts.

Kept free of storage concerns: no SQL, no dictionaries keyed by deployment, just
the logic of turning a deployment into something worth remembering.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from app.models.deployment_record import DeploymentRecord, FailureRecord, Lesson

#: Most deployments retained on a lesson. Older ones are dropped, keeping the list
#: a record of what this repository has been doing lately rather than all of history.
MAX_LESSON_PROVENANCE = 25


def now() -> datetime:
    return datetime.now(UTC)


def lesson_fingerprint(cause_id: str | None, repository: str | None, component: str | None) -> str:
    """A stable identity for "this lesson, in this place".

    Normalised so that casing and path differences do not fork a lesson into two.
    Without a component, the cause alone is the identity — a lesson scoped to
    nothing still needs to deduplicate against itself.
    """
    parts = [
        (cause_id or "unknown").strip().lower(),
        (repository or "any").strip().lower(),
        (component or "any").strip().lower(),
    ]
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
    return digest[:32]


def lesson_id_for(fingerprint: str) -> str:
    """Short, URL-safe id derived from the fingerprint, so it is stable across runs."""
    return f"lsn_{fingerprint[:16]}"


def merge_lesson(existing: Lesson | None, incoming: Lesson) -> Lesson:
    """Fold a repeated lesson into the stored one.

    A repeat keeps the first-seen time and adds to the count; later prose wins,
    because a fresher observation of the same root cause is more informative than
    the first. Evidence is replaced rather than appended — accumulating near
    duplicates of the same observation would grow the row without adding meaning.
    """
    if existing is None:
        return incoming.model_copy(
            update={
                "created_at": incoming.created_at or now(),
                "updated_at": now(),
                "first_seen_at": incoming.first_seen_at or now(),
                "last_seen_at": now(),
            }
        )

    seen = list(existing.deployment_ids)
    for deployment_id in incoming.deployment_ids:
        if deployment_id not in seen:
            seen.append(deployment_id)

    return existing.model_copy(
        update={
            "title": incoming.title or existing.title,
            "summary": incoming.summary or existing.summary,
            "detail": incoming.detail or existing.detail,
            "recommendation": incoming.recommendation or existing.recommendation,
            "evidence": incoming.evidence or existing.evidence,
            "tags": sorted({*existing.tags, *incoming.tags}),
            "occurrences": existing.occurrences + max(incoming.occurrences, 1),
            "deployment_ids": seen[-MAX_LESSON_PROVENANCE:],
            "updated_at": now(),
            "last_seen_at": now(),
        }
    )


def derive_lessons(record: DeploymentRecord) -> list[Lesson]:
    """Turn a deployment into the lessons worth keeping.

    Only failures produce lessons. A deployment that worked teaches nothing worth
    storing, and a store that accumulates successful runs as "lessons" buries the
    ones that matter under noise.

    One lesson per distinct cause. A deployment that failed six tasks for the same
    reason is one thing that happened six times, and recording it six times would
    make the occurrence count mean nothing.
    """
    lessons: list[Lesson] = []
    seen: set[str] = set()

    def add(
        *,
        cause_id: str,
        title: str,
        summary: str,
        detail: str | None,
        component: str | None,
        severity: str,
        evidence: list[str],
        recommendation: str | None,
        tags: list[str],
    ) -> None:
        fingerprint = lesson_fingerprint(cause_id, record.repository, component)
        if fingerprint in seen:
            return
        seen.add(fingerprint)
        lessons.append(
            Lesson(
                lesson_id=lesson_id_for(fingerprint),
                fingerprint=fingerprint,
                title=title,
                summary=summary,
                detail=detail,
                repository=record.repository or record.repository_path,
                component=component,
                cause_id=cause_id,
                severity=severity,
                evidence=evidence,
                recommendation=recommendation,
                tags=sorted(set(tags)),
                occurrences=1,
                deployment_ids=[record.deployment_id],
                first_seen_at=record.finished_at or record.started_at or now(),
                last_seen_at=record.finished_at or record.started_at or now(),
            )
        )

    # Incident causes carry the diagnosis, so they make the best lessons.
    incident = record.incident
    if incident is not None:
        for cause in incident.suspected_root_causes:
            recommendation = None
            if incident.recommended_fix is not None:
                recommendation = incident.recommended_fix.description
            add(
                cause_id=cause.id,
                title=f"{cause.id}: {cause.component}",
                summary=cause.cause,
                detail=cause.confirm,
                component=cause.component,
                severity=_severity_for_confidence(cause.confidence),
                evidence=[item.summary() for item in cause.evidence[:5]],
                recommendation=recommendation,
                tags=[cause.id, cause.component, incident.affected_component],
            )

    # Failures with no matching cause still teach something: the deployment broke
    # and nothing recognised why. Dropping them would lose exactly the cases where
    # a signature needs adding.
    for failure in record.failures:
        if failure.kind in seen:
            continue
        add(
            cause_id=failure.kind or "unknown_failure",
            title=f"{failure.stage} failed: {failure.kind}",
            summary=failure.message,
            detail=failure.detail,
            component=record.container,
            severity="warning",
            evidence=_evidence_for_failure(failure),
            recommendation=record.escalation_reason or record.error,
            tags=[str(failure.stage), failure.kind],
        )

    return lessons


def _severity_for_confidence(confidence: str) -> str:
    """High confidence in a cause means it is worth taking seriously, not critical.

    Severity here is how loudly the lesson should be raised, not how bad the failure
    was — an unrecognised failure is louder than a well-understood one.
    """
    return {"high": "critical", "medium": "warning"}.get(confidence, "info")


def _evidence_for_failure(failure: FailureRecord) -> list[str]:
    parts = [failure.message]
    if failure.detail:
        parts.append(failure.detail)
    if failure.task_id:
        parts.append(f"task={failure.task_id}")
    if failure.tool:
        parts.append(f"tool={failure.tool}")
    return [part for part in parts if part]


__all__ = [
    "MAX_LESSON_PROVENANCE",
    "derive_lessons",
    "lesson_fingerprint",
    "lesson_id_for",
    "merge_lesson",
    "now",
]
