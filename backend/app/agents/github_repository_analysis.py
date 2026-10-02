"""GitHub repository analysis graph.

    START → fetch_repository → inspect_files → detect_stack
          → inspect_commits → inspect_issues → assess_readiness → END

Every GitHub interaction goes through the MCP tool manager, so this graph holds no
GitHub credentials, builds no HTTP client and contains no GitHub URLs. Swapping the
transport, rate-limiting the calls or adding a cache is a change to the MCP layer,
not to this workflow.

Nothing here writes. The GitHub server exposes no mutating tool, and every tool it
does expose is annotated read-only, so the execution policy classifies them as low
risk and runs them without human approval.

Stack detection is shared with the local analyzer: :func:`detect_stack` reads only
paths and manifest text, so a synthetic inventory built from remote paths produces
the same kind of profile without a second detector drifting out of step.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.core.exceptions import NotFoundError, UpstreamError
from app.core.logging import get_logger
from app.models.github import (
    BranchSummary,
    CommitSummary,
    DeploymentReadiness,
    GitHubRepositoryAnalysisRequest,
    GitHubRepositoryProfile,
    GitHubRepositoryState,
    IssueSummary,
    PullRequestSummary,
    ReadinessCheck,
)
from app.models.repository import FileInventory
from app.services.mcp_manager import get_manager
from app.services.repository import (
    MANIFEST_NAMES,
    extract_dependencies,
)
from app.services.repository import (
    # Aliased: this module's graph node is also called detect_stack, and an
    # unaliased import would be shadowed by it and silently return a coroutine.
    detect_stack as detect_repository_stack,
)

logger = get_logger(__name__)

#: Server name the GitHub tools are expected to be registered under.
GITHUB_SERVER = "github"

#: Virtual root for the synthetic inventory. Absolute, so
#: ``FileInventory.relative`` works; nothing is ever read from disk.
_REMOTE_ROOT = Path("/github")

#: Upper bound on manifests fetched in one analysis, so a repository with dozens of
#: per-service manifests cannot fan out into hundreds of API calls.
MAX_MANIFESTS_READ = 12


async def _call_tool(tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Invoke a read-only GitHub tool through the MCP layer.

    The manager applies the execution policy before the call, so this cannot reach
    a server that has not been discovered, and a tool named like a command would
    be refused rather than forwarded.
    """
    from app.models.mcp import ToolCallRequest

    result = await get_manager().call_tool(
        ToolCallRequest(
            tool_name=f"{GITHUB_SERVER}.{tool}",
            arguments=arguments,
            requested_by="github-repository-analysis",
            reason="building a repository profile and deployment-readiness assessment",
        )
    )
    if not result.success:
        message = result.error_message or result.error_code or "unknown error"
        if result.error_code in {"policy_denied", "approval_required"}:
            raise UpstreamError(f"Refused to call {tool!r}: {message}")
        raise UpstreamError(f"GitHub tool {tool!r} failed: {message}")
    return result.content if isinstance(result.content, dict) else {}


async def fetch_repository(state: GitHubRepositoryState) -> dict[str, object]:
    """Read repository metadata and settle which ref to analyse.

    A caller-supplied branch is used as given; otherwise the repository's default
    branch is resolved here so every later node works against one known ref.
    """
    owner = state["owner"]
    repository = state["repository"]
    metadata = await _call_tool("get_repository", {"owner": owner, "repo": repository})

    requested = state.get("requested_ref")
    default_branch = metadata.get("default_branch")
    ref = requested or default_branch
    if not ref:
        raise NotFoundError(f"Repository {owner}/{repository} has no default branch.")

    logger.info(
        "github repository resolved",
        extra={"repository": f"{owner}/{repository}", "ref": ref},
    )
    return {"metadata": metadata, "ref": ref}


async def inspect_files(state: GitHubRepositoryState) -> dict[str, object]:
    """List the repository tree at the analysed ref."""
    listing = await _call_tool(
        "list_files",
        {
            "owner": state["owner"],
            "repo": state["repository"],
            "ref": state["ref"],
            "limit": state["file_limit"],
        },
    )
    entries = listing.get("files") or []
    paths = [str(item["path"]) for item in entries if item.get("path")]
    directories = sorted(
        {part for path in paths for part in Path(path).parent.parts if part not in {".", "/"}}
    )
    logger.info(
        "github files listed",
        extra={
            "repository": f"{state['owner']}/{state['repository']}",
            "file_count": len(paths),
            "truncated": bool(listing.get("truncated")),
        },
    )
    return {
        "files": entries,
        "file_paths": paths,
        "directories": directories,
        "truncated": bool(listing.get("truncated")),
        "has_lockfile": _detect_lockfile(paths),
    }


async def detect_stack(state: GitHubRepositoryState) -> dict[str, object]:
    """Identify the technology stack from manifests fetched over MCP.

    Only manifests whose text is cheap and bounded are fetched: the stack
    detectors already treat a missing manifest as "unknown", so reading every
    file in the tree would spend API calls to learn nothing.
    """
    owner, repository = state["owner"], state["repository"]
    ref = state["ref"]

    candidates = [
        path
        for path in state["file_paths"]
        if Path(path).name.lower() in MANIFEST_NAMES and len(Path(path).parts) <= 2
    ][:MAX_MANIFESTS_READ]

    dependencies: dict[str, str] = {}
    for path in candidates:
        payload = await _call_tool(
            "get_file_contents",
            {"owner": owner, "repo": repository, "path": path, "ref": ref},
        )
        content = payload.get("content")
        if isinstance(content, str):
            dependencies[path] = content

    inventory = FileInventory(
        root=_REMOTE_ROOT,
        files=tuple(_REMOTE_ROOT / path for path in state["file_paths"]),
        directories=frozenset(state["directories"]),
        file_count=len(state["file_paths"]),
        truncated=state["truncated"],
        dependencies={
            path: frozenset(extract_dependencies(Path(path).name, text))
            for path, text in dependencies.items()
        },
    )

    # detect_stack returns a RepositoryProfile; enrich it into a GitHub profile
    # after the readiness assessment has added nothing the stack does not know.
    profile = detect_repository_stack(inventory)
    return {"profile": _to_github_profile(profile, state)}


def _to_github_profile(profile: Any, state: GitHubRepositoryState) -> GitHubRepositoryProfile:
    """Lift a detected :class:`RepositoryProfile` into the GitHub shape."""
    metadata = state.get("metadata") or {}
    owner, repository = state["owner"], state["repository"]
    ref = state.get("ref")

    pushed_at = metadata.get("pushed_at")
    return GitHubRepositoryProfile(
        name=repository,
        root=f"github://{owner}/{repository}" + (f"@{ref}" if ref else ""),
        file_count=profile.file_count,
        scanned_file_count=profile.scanned_file_count,
        truncated=profile.truncated,
        languages=profile.languages,
        primary_language=profile.primary_language,
        frontend_framework=profile.frontend_framework,
        backend_framework=profile.backend_framework,
        package_manager=profile.package_manager,
        package_managers=profile.package_managers,
        package_files=profile.package_files,
        entry_points=profile.entry_points,
        has_dockerfile=profile.has_dockerfile,
        dockerfiles=profile.dockerfiles,
        has_docker_compose=profile.has_docker_compose,
        docker_compose_files=profile.docker_compose_files,
        test_framework=profile.test_framework,
        test_file_count=profile.test_file_count,
        test_files=profile.test_files,
        env_files=profile.env_files,
        databases=profile.databases,
        ci_cd=profile.ci_cd,
        ci_files=profile.ci_files,
        has_readme=profile.has_readme,
        readme_files=profile.readme_files,
        notes=list(profile.notes),
        owner=owner,
        full_name=metadata.get("full_name") or f"{owner}/{repository}",
        default_branch=metadata.get("default_branch"),
        analysed_ref=ref,
        description=metadata.get("description"),
        license=metadata.get("license"),
        topics=list(metadata.get("topics") or []),
        visibility=metadata.get("visibility"),
        stars=metadata.get("stars"),
        forks=metadata.get("forks"),
        archived=bool(metadata.get("archived")),
        is_fork=bool(metadata.get("fork")),
        pushed_at=_parse_datetime(pushed_at),
    )


def _parse_datetime(value: Any) -> Any:
    """Let pydantic parse a GitHub timestamp, tolerating absent or odd values."""
    return value if isinstance(value, str) and value else None


async def inspect_commits(state: GitHubRepositoryState) -> dict[str, object]:
    """Read recent commits on the analysed ref.

    A failure here is recorded rather than raised: commit history is context, not
    the subject of the analysis, and losing it should not lose the profile.
    """
    try:
        payload = await _call_tool(
            "list_commits",
            {
                "owner": state["owner"],
                "repo": state["repository"],
                "ref": state["ref"],
                "limit": state["commit_limit"],
            },
        )
    except UpstreamError as exc:
        logger.warning("github commits unavailable", extra={"error": str(exc)})
        profile: GitHubRepositoryProfile = state["profile"]
        profile.notes = [*profile.notes, f"Recent commits could not be read: {exc}"]
        return {}

    profile = state["profile"]
    profile.recent_commits = [
        CommitSummary(
            sha=item.get("sha"),
            message=item.get("message") or "",
            author=item.get("author"),
            authored_at=_parse_datetime(item.get("authored_at")),
        )
        for item in payload.get("commits") or []
    ]

    # Branches are cheap and make the ref auditable: a caller can confirm which
    # branch was analysed without trusting the request.
    try:
        branches = await _call_tool(
            "list_branches",
            {"owner": state["owner"], "repo": state["repository"], "limit": 100},
        )
        profile.branches = [
            BranchSummary(name=str(item.get("name")), protected=bool(item.get("protected")))
            for item in branches.get("branches") or []
            if item.get("name")
        ]
    except UpstreamError as exc:
        logger.warning("github branches unavailable", extra={"error": str(exc)})
        profile.notes = [*profile.notes, f"Branches could not be read: {exc}"]

    return {}


async def inspect_issues(state: GitHubRepositoryState) -> dict[str, object]:
    """Read open issues and pull requests, when the caller asked for them.

    Skipped entirely when ``include_issues`` is false, and recorded as *not
    inspected* rather than as zero issues, so a caller cannot mistake "not
    looked at" for "nothing open".
    """
    profile: GitHubRepositoryProfile = state["profile"]

    if not state.get("include_issues"):
        profile.notes = [
            *profile.notes,
            "Issues were not inspected; pass include_issues to include them.",
        ]
        return {}

    owner, repository = state["owner"], state["repository"]
    limit = state["issue_limit"]

    try:
        issues = await _call_tool(
            "list_issues",
            {"owner": owner, "repo": repository, "state": "open", "limit": limit},
        )
        profile.open_issues = [
            IssueSummary(
                number=int(item.get("number") or 0),
                title=item.get("title") or "",
                state=item.get("state") or "open",
                labels=[str(label) for label in item.get("labels") or []],
                comments=int(item.get("comments") or 0),
                author=item.get("author"),
                created_at=_parse_datetime(item.get("created_at")),
            )
            for item in issues.get("issues") or []
        ]
    except UpstreamError as exc:
        logger.warning("github issues unavailable", extra={"error": str(exc)})
        profile.notes = [*profile.notes, f"Issues could not be read: {exc}"]
        return {}

    try:
        pulls = await _call_tool(
            "list_pull_requests",
            {"owner": owner, "repo": repository, "state": "open", "limit": limit},
        )
        profile.open_pull_requests = [
            PullRequestSummary(
                number=int(item.get("number") or 0),
                title=item.get("title") or "",
                state=item.get("state") or "open",
                draft=bool(item.get("draft")),
                head=item.get("head"),
                base=item.get("base"),
                author=item.get("author"),
            )
            for item in pulls.get("pull_requests") or []
        ]
    except UpstreamError as exc:
        logger.warning("github pull requests unavailable", extra={"error": str(exc)})
        profile.notes = [*profile.notes, f"Pull requests could not be read: {exc}"]
        return {}

    profile.issues_inspected = True
    return {}


async def assess_readiness(state: GitHubRepositoryState) -> dict[str, object]:
    """Turn the profile into a scored, explainable readiness assessment.

    The score is a heuristic and says so in ``notes``. Each signal is recorded as a
    named check so a caller can see which facts moved the score rather than being
    handed an unexplained number.
    """
    profile: GitHubRepositoryProfile = state["profile"]
    checks: list[ReadinessCheck] = []
    blockers: list[str] = []
    warnings: list[str] = []
    recommendations: list[str] = []
    score = 100

    def fail(name: str, detail: str, penalty: int, message: str) -> None:
        nonlocal score
        checks.append(ReadinessCheck(name=name, status="fail", detail=detail))
        blockers.append(message)
        score -= penalty

    def warn(name: str, detail: str, penalty: int, message: str) -> None:
        nonlocal score
        checks.append(ReadinessCheck(name=name, status="warn", detail=detail))
        warnings.append(message)
        score -= penalty

    def passed(name: str, detail: str) -> None:
        checks.append(ReadinessCheck(name=name, status="pass", detail=detail))

    # --- Blockers ---------------------------------------------------------
    if profile.archived:
        fail(
            "repository_archived",
            "The repository is archived and no longer accepts changes.",
            40,
            "The repository is archived.",
        )
    else:
        passed("repository_archived", "The repository is active.")

    if profile.has_dockerfile:
        passed("container_build", f"Found {', '.join(profile.dockerfiles[:3])}.")
    else:
        fail(
            "container_build",
            "No Dockerfile was found in the repository tree.",
            25,
            "No Dockerfile found, so there is no defined way to build an image.",
        )

    if not profile.ci_cd:
        fail(
            "continuous_integration",
            "No CI configuration was found.",
            20,
            "No CI pipeline found, so nothing verifies a build before deployment.",
        )
    else:
        passed("continuous_integration", f"Detected {', '.join(profile.ci_cd)}.")

    if profile.test_file_count == 0:
        fail(
            "tests",
            "No test files were found.",
            20,
            "No tests found, so a deployment has no automated safety net.",
        )
    elif profile.test_framework in (None, "unknown"):
        warn(
            "tests",
            f"Found {profile.test_file_count} test files but no identifiable framework.",
            5,
            "Test files exist but the framework could not be identified.",
        )
    else:
        passed("tests", f"{profile.test_file_count} test files using {profile.test_framework}.")

    if not profile.has_readme:
        warn(
            "documentation",
            "No README was found.",
            5,
            "No README, so deployment steps are undocumented.",
        )

    # --- Warnings ---------------------------------------------------------
    if profile.truncated:
        warn(
            "completeness",
            "The file listing was capped, so some files were not inspected.",
            10,
            "The file listing was truncated; the assessment may be incomplete.",
        )

    if profile.package_manager is None:
        warn(
            "dependency_locking",
            "No package manager matched the primary language.",
            10,
            "Could not determine the package manager for the primary language.",
        )
    elif not state.get("has_lockfile"):
        warn(
            "dependency_locking",
            f"{profile.package_manager} found without a lockfile.",
            10,
            f"No lockfile found for {profile.package_manager}; builds are not reproducible.",
        )
    else:
        passed("dependency_locking", f"{profile.package_manager} with a lockfile.")

    if len(profile.languages) > 1:
        warn(
            "language_mix",
            f"Multiple languages: {', '.join(profile.languages[:5])}.",
            5,
            "Multiple languages present; confirm the build covers all of them.",
        )

    if profile.issues_inspected:
        issues, pulls = len(profile.open_issues), len(profile.open_pull_requests)
        if issues >= 25:
            warn(
                "issue_backlog",
                f"{issues} open issues.",
                10,
                f"{issues} open issues suggest unresolved work before deployment.",
            )
        elif issues:
            checks.append(
                ReadinessCheck(name="issue_backlog", status="pass", detail=f"{issues} open issues.")
            )
        if pulls:
            checks.append(
                ReadinessCheck(
                    name="open_pull_requests",
                    status="warn",
                    detail=f"{pulls} open pull requests.",
                )
            )
            warnings.append(f"{pulls} open pull requests may not be included in the deployed ref.")
        elif pulls == 0:
            checks.append(
                ReadinessCheck(
                    name="open_pull_requests", status="pass", detail="No open pull requests."
                )
            )
    else:
        checks.append(
            ReadinessCheck(
                name="issue_backlog",
                status="unknown",
                detail="Issues were not inspected, so the backlog is unknown.",
            )
        )

    if (
        profile.stars is not None
        and profile.forks is not None
        and profile.stars > 0
        and profile.forks == 0
        and profile.stars < 5
    ):
        warn(
            "project_maturity",
            f"{profile.stars} stars and no forks.",
            5,
            "Very low external usage; confirm this repository is the right deployment target.",
        )

    # --- Recommendations, driven by what actually failed -------------------
    if not profile.has_dockerfile:
        recommendations.append("Add a Dockerfile so the deployment has a reproducible build.")
    if not profile.ci_cd:
        recommendations.append("Add a CI workflow that builds and tests on every push.")
    if profile.test_file_count == 0:
        recommendations.append(
            "Add tests before deploying, so a regression can be caught automatically."
        )
    if not profile.ci_files:
        recommendations.append(
            "Review .github/workflows to confirm CI covers the build used in deployment."
        )
    if profile.primary_language is None:
        recommendations.append(
            "No source language was recognised; confirm the repository contents are complete."
        )

    readiness = DeploymentReadiness(
        score=max(0, min(100, score)),
        ready=not blockers,
        blockers=blockers,
        warnings=warnings,
        recommendations=recommendations,
        checks=checks,
        notes=[
            "This is a heuristic assessment derived from repository metadata and file "
            "contents. It is not a substitute for reviewing the deployment itself.",
            "No code was executed and nothing was changed.",
        ],
    )
    logger.info(
        "github readiness assessed",
        extra={
            "repository": profile.full_name,
            "score": readiness.score,
            "ready": readiness.ready,
            "blockers": len(blockers),
        },
    )
    return {"readiness": readiness}


#: Lockfile per package manager. A manifest pins ranges, a lockfile pins exact
#: versions, so the lockfile is what makes a build reproducible.
LOCKFILES: dict[str, tuple[str, ...]] = {
    "npm": ("package-lock.json",),
    "pnpm": ("pnpm-lock.yaml",),
    "yarn": ("yarn.lock",),
    "bun": ("bun.lockb", "bun.lock"),
    "uv": ("uv.lock",),
    "poetry": ("poetry.lock",),
    "go": ("go.sum",),
    "cargo": ("Cargo.lock",),
    "maven": ("pom.xml",),
    "gradle": ("gradle.lockfile",),
}


def _detect_lockfile(paths: list[str]) -> bool:
    """Whether the tree contains any lockfile at all.

    Deliberately manager-agnostic: this runs before the package manager is known,
    and any lockfile is positive evidence that the project pins its
    dependencies. The precise per-manager check happens in ``detect_stack``.
    """
    known = {name.casefold() for names in LOCKFILES.values() for name in names}
    return any(path.rsplit("/", 1)[-1].casefold() in known for path in paths)


def build_github_analysis_graph() -> CompiledStateGraph:
    """Assemble and compile the GitHub repository analysis graph."""
    graph = StateGraph(GitHubRepositoryState)
    graph.add_node("fetch_repository", fetch_repository)
    graph.add_node("inspect_files", inspect_files)
    graph.add_node("detect_stack", detect_stack)
    graph.add_node("inspect_commits", inspect_commits)
    graph.add_node("inspect_issues", inspect_issues)
    graph.add_node("assess_readiness", assess_readiness)

    graph.add_edge(START, "fetch_repository")
    graph.add_edge("fetch_repository", "inspect_files")
    graph.add_edge("inspect_files", "detect_stack")
    graph.add_edge("detect_stack", "inspect_commits")
    graph.add_edge("inspect_commits", "inspect_issues")
    graph.add_edge("inspect_issues", "assess_readiness")
    graph.add_edge("assess_readiness", END)

    return graph.compile()


#: Shared compiled graph; compiling has no side effects.
github_analysis_graph = build_github_analysis_graph()


async def analyze_github_repository(
    request: GitHubRepositoryAnalysisRequest,
) -> tuple[GitHubRepositoryProfile, DeploymentReadiness]:
    """Run the whole workflow and return the profile and readiness assessment."""
    state: GitHubRepositoryState = {
        "owner": request.owner,
        "repository": request.repository,
        "requested_ref": request.branch,
        "include_issues": request.include_issues,
        "issue_limit": request.issue_limit,
        "commit_limit": request.commit_limit,
        "file_limit": request.file_limit,
    }
    result = await github_analysis_graph.ainvoke(state)
    return result["profile"], result["readiness"]


__all__ = [
    "analyze_github_repository",
    "assess_readiness",
    "build_github_analysis_graph",
    "detect_stack",
    "fetch_repository",
    "github_analysis_graph",
    "inspect_commits",
    "inspect_files",
    "inspect_issues",
]
