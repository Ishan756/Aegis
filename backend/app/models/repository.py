"""Repository analysis contracts.

:class:`RepositoryProfile` is what ``POST /api/repository/analyze`` returns. It
describes what a static read of a repository found: no code was run, no service
was contacted, and nothing on disk was modified.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, TypedDict

from pydantic import BaseModel, Field

PackageManager = Literal[
    "npm",
    "pnpm",
    "yarn",
    "bun",
    "pip",
    "uv",
    "poetry",
    "maven",
    "gradle",
    "go",
    "cargo",
    "bundler",
    "composer",
    "nuget",
]


class RepositoryAnalysisRequest(BaseModel):
    """Body of ``POST /api/repository/analyze``."""

    path: str = Field(
        min_length=1,
        max_length=4096,
        description=(
            "Path to a repository, absolute or relative to the configured "
            "repository root. Must resolve inside that root."
        ),
    )


class RepositoryProfile(BaseModel):
    """What a static read of a repository reveals."""

    # --- Identity ---------------------------------------------------------
    name: str = Field(description="Directory name of the repository.")
    root: str = Field(description="Absolute, resolved path that was analysed.")
    detected_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), description="When the analysis ran."
    )

    # --- Scan accounting --------------------------------------------------
    file_count: int = Field(ge=0, description="Files seen, including pruned trees.")
    scanned_file_count: int = Field(ge=0, description="Files actually inspected.")
    truncated: bool = Field(
        default=False, description="True when a scan limit was hit; results are partial."
    )

    # --- Stack ------------------------------------------------------------
    languages: list[str] = Field(default_factory=list, description="Languages detected.")
    primary_language: str | None = Field(
        default=None, description="Most common language, by file count."
    )
    frontend_framework: str | None = None
    backend_framework: str | None = None
    package_manager: PackageManager | None = None
    package_managers: list[PackageManager] = Field(
        default_factory=list, description="Every manager whose manifest or lockfile exists."
    )
    package_files: list[str] = Field(default_factory=list, description="Manifests found.")
    entry_points: list[str] = Field(default_factory=list, description="Likely runnable files.")

    # --- Tooling ----------------------------------------------------------
    has_dockerfile: bool = False
    dockerfiles: list[str] = Field(default_factory=list)
    has_docker_compose: bool = False
    docker_compose_files: list[str] = Field(default_factory=list)
    test_framework: str | None = None
    test_file_count: int = Field(default=0, ge=0)
    test_files: list[str] = Field(default_factory=list, description="Sample of test files.")

    # --- Configuration ----------------------------------------------------
    env_files: list[str] = Field(
        default_factory=list,
        description="Environment files by name only. Contents are never read.",
    )
    databases: list[str] = Field(
        default_factory=list, description="Databases and datastores referenced."
    )
    ci_cd: list[str] = Field(default_factory=list, description="CI/CD systems detected.")
    ci_files: list[str] = Field(default_factory=list)

    # --- Docs -------------------------------------------------------------
    has_readme: bool = False
    readme_files: list[str] = Field(default_factory=list)

    # --- Honesty ----------------------------------------------------------
    notes: list[str] = Field(
        default_factory=list,
        description="Caveats a caller should know: limits hit, ambiguous signals.",
    )


@dataclass(slots=True)
class FileInventory:
    """What a bounded walk of the repository found.

    Internal to the workflow: this is an intermediate result, not part of the API
    contract. Paths are relative to :attr:`root` so nothing outside the resolved
    root can be echoed back.
    """

    root: Path
    files: tuple[Path, ...] = ()
    directories: frozenset[str] = frozenset()
    file_count: int = 0
    truncated: bool = False
    #: Relative path -> parsed dependency names, for manifests that were read.
    dependencies: dict[str, frozenset[str]] = field(default_factory=dict)

    def relative(self, path: Path) -> str:
        """Return ``path`` relative to the scan root."""
        try:
            return path.relative_to(self.root).as_posix()
        except ValueError:  # pragma: no cover - defensive; walk yields children only
            return path.name


class RepositoryState(TypedDict, total=False):
    """LangGraph state for the repository analysis workflow.

    ``FileInventory`` is what the scan node produces and the detect node consumes;
    ``profile`` is the public result. ``total=False`` because LangGraph merges
    each node's return value into the accumulated state.
    """

    requested_path: str
    root: Path
    inventory: FileInventory
    profile: RepositoryProfile
