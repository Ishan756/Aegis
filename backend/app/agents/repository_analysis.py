"""Repository analysis graph.

    START → resolve_target → scan_repository → detect_stack → build_profile → END

Runnable on its own via :func:`analyze_repository_path`, and callable node by
node. Path containment is a graph node rather than a helper so the safety
boundary is visible in the topology and can be tested on its own: no node after
``resolve_target`` ever sees a path the user supplied.

The repository is only ever read. Nothing here executes repository code.
"""

from __future__ import annotations

from pathlib import Path

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.core.config import get_settings
from app.core.exceptions import NotFoundError, PermissionDeniedError, ValidationError
from app.core.logging import get_logger
from app.models.repository import RepositoryProfile, RepositoryState
from app.services.repository import detect_stack, scan_repository

logger = get_logger(__name__)


def resolve_target(state: RepositoryState) -> dict[str, object]:
    """Resolve the requested path and confirm it stays inside the allowed root.

    This is the security boundary. ``..`` segments and symlinks are collapsed by
    :meth:`Path.resolve` before the containment check, so neither can be used to
    read outside the configured root.
    """
    requested = state["requested_path"]
    settings = get_settings()
    allowed_root = settings.repository_root_resolved

    if not requested or not requested.strip():
        # Path("") resolves to the current directory, which would silently
        # analyse the whole root instead of rejecting the request.
        raise ValidationError("A repository path is required.")

    try:
        # A relative path is interpreted against the allowed root, never the
        # server's own working directory.
        candidate = Path(requested).expanduser()
        if not candidate.is_absolute():
            candidate = allowed_root / candidate
        resolved = candidate.resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        # RuntimeError covers symlink loops; ValueError covers embedded nulls.
        raise ValidationError("The supplied path could not be resolved.") from exc

    if not allowed_root.is_dir():
        raise ValidationError(
            "The configured repository root does not exist or is not a directory.",
            details={"repository_root": str(allowed_root)},
        )

    if resolved != allowed_root and allowed_root not in resolved.parents:
        raise PermissionDeniedError(
            "The requested path is outside the allowed repository root.",
            details={"repository_root": str(allowed_root)},
        )

    if not resolved.exists():
        raise NotFoundError(
            "No such repository path.",
            details={"path": str(resolved)},
        )

    if not resolved.is_dir():
        raise ValidationError(
            "The supplied path is not a directory.",
            details={"path": str(resolved)},
        )

    logger.info("repository target resolved", extra={"repository_root": str(resolved)})
    return {"root": resolved}


def scan_node(state: RepositoryState) -> dict[str, object]:
    """Walk the repository and collect a bounded, symlink-free inventory."""
    root: Path = state["root"]  # type: ignore[assignment]
    inventory = scan_repository(root)

    logger.info(
        "repository scanned",
        extra={
            "repository": root.name,
            "files": inventory.file_count,
            "truncated": inventory.truncated,
        },
    )
    return {"inventory": inventory}


def detect_node(state: RepositoryState) -> dict[str, RepositoryProfile]:
    """Derive the stack profile from the inventory."""
    profile = detect_stack(state["inventory"])  # type: ignore[arg-type]

    logger.info(
        "repository analysed",
        extra={
            "repository": profile.name,
            "primary_language": profile.primary_language,
            "frontend_framework": profile.frontend_framework,
            "backend_framework": profile.backend_framework,
        },
    )
    return {"profile": profile}


def build_profile(state: RepositoryState) -> dict[str, RepositoryProfile]:
    """Final pass: confirm the profile is usable before returning it.

    A repository with no recognised content is still a valid answer, so this
    only annotates rather than rejects.
    """
    profile = state["profile"]

    if profile.scanned_file_count == 0:
        profile.notes = [*profile.notes, "The directory appears to be empty."]

    logger.info("repository profile built", extra={"repository": profile.name})
    return {"profile": profile}


def build_analysis_graph() -> CompiledStateGraph:
    """Assemble and compile the repository analysis graph."""
    graph = StateGraph(RepositoryState)
    graph.add_node("resolve_target", resolve_target)
    graph.add_node("scan_repository", scan_node)
    graph.add_node("detect_stack", detect_node)
    graph.add_node("build_profile", build_profile)

    graph.add_edge(START, "resolve_target")
    graph.add_edge("resolve_target", "scan_repository")
    graph.add_edge("scan_repository", "detect_stack")
    graph.add_edge("detect_stack", "build_profile")
    graph.add_edge("build_profile", END)

    return graph.compile()


#: Shared compiled graph; compiling has no side effects.
repository_analysis_graph = build_analysis_graph()


def analyze_repository_path(path: str) -> RepositoryProfile:
    """Run the whole workflow and return the profile.

    The public entry point for the graph, independent of HTTP.
    """
    state = repository_analysis_graph.invoke({"requested_path": path})
    return state["profile"]


__all__ = [
    "analyze_repository_path",
    "build_analysis_graph",
    "build_profile",
    "detect_node",
    "repository_analysis_graph",
    "resolve_target",
    "scan_node",
]
