"""GitHub repository analysis contracts.

The request names a repository; the response is a :class:`GitHubRepositoryProfile`
(which extends the local :class:`~app.models.repository.RepositoryProfile` with
repository metadata, recent commits and open issues) plus a
:class:`DeploymentReadiness` assessment.

No model in this module carries a credential. Nothing here is populated from a
token, and no field exists that a token could be written into by accident.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, TypedDict

from pydantic import BaseModel, Field, field_validator

from app.models.repository import RepositoryProfile

ReadinessStatus = Literal["pass", "warn", "fail", "unknown"]


class GitHubRepositoryAnalysisRequest(BaseModel):
    """Body of ``POST /api/github/repository/analyze``."""

    owner: str = Field(
        min_length=1,
        max_length=39,
        description="Repository owner: a user or organisation login.",
    )
    repository: str = Field(
        min_length=1,
        max_length=100,
        description="Repository name, without the owner prefix.",
    )
    branch: str | None = Field(
        default=None,
        max_length=255,
        description="Branch or ref to analyse. Defaults to the repository's default branch.",
    )
    include_issues: bool = Field(
        default=False,
        description="Fetch open issues and pull requests. Costs extra API calls.",
    )
    issue_limit: int = Field(default=30, ge=1, le=100)
    commit_limit: int = Field(default=20, ge=1, le=50)
    file_limit: int = Field(default=2000, ge=1, le=2000)

    @field_validator("owner", "repository")
    @classmethod
    def _reject_path_syntax(cls, value: str) -> str:
        """Reject owner and repository names that are not GitHub identifiers.

        Both end up as URL path segments in requests to the GitHub API. Allowing
        a slash or a dot segment here would let a caller address a different path
        than the repository they named, so anything outside the character set
        GitHub itself allows is refused up front rather than escaped later.
        """
        allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_._")
        invalid = sorted(set(value) - allowed)
        if invalid:
            raise ValueError(f"Invalid characters in name: {''.join(invalid)!r}")
        if value.startswith(".") or ".." in value:
            raise ValueError("Name may not begin with a dot or contain '..'")
        return value


class BranchSummary(BaseModel):
    """A branch as GitHub reports it."""

    name: str
    protected: bool = False


class CommitSummary(BaseModel):
    """One recent commit. Only the first line of the message is kept."""

    sha: str | None = None
    message: str = ""
    author: str | None = None
    authored_at: datetime | None = None


class IssueSummary(BaseModel):
    """One open issue."""

    number: int
    title: str = ""
    state: str = "open"
    labels: list[str] = Field(default_factory=list)
    comments: int = 0
    author: str | None = None
    created_at: datetime | None = None


class PullRequestSummary(BaseModel):
    """One open pull request."""

    number: int
    title: str = ""
    state: str = "open"
    draft: bool = False
    head: str | None = None
    base: str | None = None
    author: str | None = None


class GitHubRepositoryProfile(RepositoryProfile):
    """A :class:`RepositoryProfile` enriched with what only GitHub knows."""

    # `name` and `root` are inherited. For a remote repository they are the
    # repository name and a github:// reference rather than a filesystem path.
    source: Literal["github"] = "github"
    owner: str = Field(description="Repository owner.")
    full_name: str = Field(description="owner/name.")
    default_branch: str | None = Field(
        default=None, description="The branch GitHub treats as the default."
    )
    analysed_ref: str | None = Field(
        default=None, description="The ref actually analysed; may differ from the default."
    )
    description: str | None = None
    license: str | None = Field(default=None, description="SPDX licence identifier.")
    topics: list[str] = Field(default_factory=list)
    visibility: str | None = None
    stars: int | None = None
    forks: int | None = None
    archived: bool = False
    is_fork: bool = False
    pushed_at: datetime | None = None

    # --- Activity ---------------------------------------------------------
    branches: list[BranchSummary] = Field(default_factory=list)
    recent_commits: list[CommitSummary] = Field(default_factory=list)
    open_issues: list[IssueSummary] = Field(default_factory=list)
    open_pull_requests: list[PullRequestSummary] = Field(default_factory=list)
    issues_inspected: bool = Field(
        default=False,
        description="Whether issues were fetched. False means unknown, not zero.",
    )


class ReadinessCheck(BaseModel):
    """One named signal behind the readiness score."""

    name: str
    status: ReadinessStatus
    detail: str = ""


class DeploymentReadiness(BaseModel):
    """How ready this repository is to be deployed, and why."""

    score: int = Field(ge=0, le=100, description="0 is not deployable, 100 is ready to ship.")
    ready: bool = Field(description="True only when there are no blockers.")
    blockers: list[str] = Field(
        default_factory=list, description="Problems that must be resolved first."
    )
    warnings: list[str] = Field(
        default_factory=list, description="Risks worth a human's attention."
    )
    recommendations: list[str] = Field(default_factory=list)
    checks: list[ReadinessCheck] = Field(default_factory=list)
    notes: list[str] = Field(
        default_factory=list,
        description="Caveats about this assessment, including its heuristic nature.",
    )


class GitHubRepositoryAnalysisResponse(BaseModel):
    """What ``POST /api/github/repository/analyze`` returns."""

    profile: GitHubRepositoryProfile
    readiness: DeploymentReadiness


class GitHubRepositoryState(TypedDict, total=False):
    """LangGraph state for the GitHub repository analysis workflow.

    ``files`` and ``dependencies`` feed a synthetic
    :class:`~app.models.repository.FileInventory` so remote and local analysis
    share one stack-detection implementation rather than two that drift apart.
    """

    owner: str
    repository: str
    requested_ref: str | None
    include_issues: bool
    issue_limit: int
    commit_limit: int
    file_limit: int

    metadata: dict
    ref: str | None
    files: list[dict]
    truncated: bool
    file_paths: list[str]
    directories: list[str]
    has_lockfile: bool
    dependencies: dict[str, str]
    profile: GitHubRepositoryProfile
    readiness: DeploymentReadiness
