"""Deployment memory: durable history and reusable lessons.

:func:`get_memory_service` is the accessor the rest of the service uses, mirroring
the MCP manager's ``get_manager``/``set_manager`` pair. A module-level singleton
keeps wiring out of the call sites; the lifespan in :mod:`app.main` builds and
installs it, and tests install their own.
"""

from __future__ import annotations

import logging

from app.memory.base import LessonMatcher, MemoryStore
from app.memory.in_memory import InMemoryMemoryStore
from app.memory.lessons import derive_lessons, lesson_fingerprint, merge_lesson
from app.memory.matching import KeywordLessonMatcher
from app.memory.service import DeploymentMemoryService, build_memory_service

logger = logging.getLogger(__name__)

_service: DeploymentMemoryService | None = None


def set_memory_service(service: DeploymentMemoryService | None) -> None:
    """Install the process-wide service. Passing ``None`` clears it."""
    global _service
    _service = service


def get_memory_service() -> DeploymentMemoryService:
    """Return the installed service.

    Falls back to an in-memory one rather than raising. History is an observation
    of what happened, not a precondition for deploying, so a service that has not
    been wired up yet should still let deployments run and simply have nothing to
    record.
    """
    global _service
    if _service is None:
        logger.debug("no memory service installed; using a temporary in-memory one")
        _service = DeploymentMemoryService(InMemoryMemoryStore())
    return _service


__all__ = [
    "DeploymentMemoryService",
    "InMemoryMemoryStore",
    "KeywordLessonMatcher",
    "LessonMatcher",
    "MemoryStore",
    "build_memory_service",
    "derive_lessons",
    "get_memory_service",
    "lesson_fingerprint",
    "merge_lesson",
    "set_memory_service",
]
