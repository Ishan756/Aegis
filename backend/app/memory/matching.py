"""Keyword lesson recall.

Scores a lesson against the deployment about to run using overlap on repository,
component, tags and free text, weighted so that a lesson from the same repository
outranks a generic one. Repetition counts: a root cause seen nine times is a
stronger warning than one seen once.

This is the current implementation of :class:`~app.memory.base.LessonMatcher`. It
is intentionally simple and deterministic — no model, no network, no index. When
that stops being good enough, the replacement is a different implementation of the
same one-method protocol, and the only file that changes is this one plus whatever
column backs it.
"""

from __future__ import annotations

import re

from app.memory.base import LessonMatcher
from app.models.deployment_record import Lesson

_TOKEN = re.compile(r"[a-z0-9_]+")

#: Words too common to say anything about relevance.
_STOPWORDS = frozenset(
    {
        "the",
        "and",
        "for",
        "with",
        "was",
        "not",
        "but",
        "has",
        "have",
        "this",
        "that",
        "from",
        "its",
        "are",
        "container",
        "error",
        "failed",
    }
)

# Weights sum to 1.0 so a score is a proportion rather than an accident.
_W_SAME_REPOSITORY = 0.35
_W_SAME_COMPONENT = 0.20
_W_CAUSE = 0.20
_W_TOKEN_OVERLAP = 0.15
_W_TAG_OVERLAP = 0.05
_W_RECURRENCE = 0.05


def _tokens(text: str) -> set[str]:
    return {token for token in _TOKEN.findall(text.lower()) if token not in _STOPWORDS}


class KeywordLessonMatcher(LessonMatcher):
    """Deterministic token overlap with a recurrence bonus."""

    def score(
        self,
        lesson: Lesson,
        *,
        repository: str | None,
        component: str | None,
        query: str | None,
    ) -> float:
        score = 0.0

        if repository and lesson.repository and lesson.repository.lower() == repository.lower():
            score += _W_SAME_REPOSITORY
        if component and lesson.component and lesson.component.lower() == component.lower():
            score += _W_SAME_COMPONENT

        # A matching cause id is a strong signal: it means the same failure
        # signature fired, which is a more reliable match than shared vocabulary.
        if query and lesson.cause_id and lesson.cause_id.lower() in query.lower():
            score += _W_CAUSE

        if query:
            lesson_tokens = _tokens(f"{lesson.title} {lesson.summary} {lesson.detail or ''}")
            query_tokens = _tokens(query)
            if lesson_tokens and query_tokens:
                overlap = len(lesson_tokens & query_tokens) / len(query_tokens)
                score += _W_TOKEN_OVERLAP * min(overlap, 1.0)

            if lesson.tags:
                tag_tokens = _tokens(" ".join(lesson.tags))
                if tag_tokens:
                    score += _W_TAG_OVERLAP * min(
                        len(tag_tokens & query_tokens) / len(tag_tokens), 1.0
                    )

        # Saturating, so a lesson seen 50 times is not 50x louder than one seen once.
        score += _W_RECURRENCE * min((lesson.occurrences - 1) / 4, 1.0)

        return min(score, 1.0)


__all__ = ["KeywordLessonMatcher"]
