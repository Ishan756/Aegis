"""GitHub MCP integration tests.

The MCP responses are mocked rather than mocked at the transport layer below the
SDK. What matters here is Aegis' side of the contract: that the workflow asks for
the right tool with the right arguments, handles a partial failure honestly, never
touches a credential, and cannot write anything. Mocking ``urllib`` instead would
leave all of that untested.

No test in this file performs network I/O or spawns a server process.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.agents.github_repository_analysis import analyze_github_repository
from app.core.config import get_settings
from app.models.github import (
    DeploymentReadiness,
    GitHubRepositoryAnalysisRequest,
    GitHubRepositoryProfile,
)
from app.models.mcp import ToolCallRequest, ToolCallResult
from app.services.mcp_manager import set_manager

pytestmark = pytest.mark.anyio


# --- Fixture payloads ----------------------------------------------------
#
# Shaped like the real GitHub REST responses the MCP server returns, so a change
# to the server's output shape breaks these tests rather than production.

REPOSITORY_METADATA: dict[str, Any] = {
    "id": 12345,
    "name": "checkout-service",
    "full_name": "acme/checkout-service",
    "description": "Handles customer checkouts.",
    "default_branch": "main",
    "language": "TypeScript",
    "license": "MIT",
    "topics": ["payments", "node"],
    "size_kb": 2048,
    "stars": 42,
    "forks": 3,
    "open_issues_count": 2,
    "archived": False,
    "fork": False,
    "visibility": "private",
    "created_at": "2023-01-04T10:00:00Z",
    "updated_at": "2024-05-01T09:00:00Z",
    "pushed_at": "2024-04-30T17:22:11Z",
}

FILE_TREE: list[dict[str, Any]] = [
    {"path": "README.md", "type": "blob", "size": 1200},
    {"path": "package.json", "type": "blob", "size": 900},
    {"path": "package-lock.json", "type": "blob", "size": 90000},
    {"path": "Dockerfile", "type": "blob", "size": 400},
    {"path": "src/index.ts", "type": "blob", "size": 2200},
    {"path": "src/routes/checkout.ts", "type": "blob", "size": 4400},
    {"path": "tests/checkout.test.ts", "type": "blob", "size": 1800},
    {"path": ".github/workflows/ci.yml", "type": "blob", "size": 700},
    {"path": ".env.example", "type": "blob", "size": 200},
]

MANIFEST_BODIES: dict[str, str] = {
    "package.json": """{
  "name": "checkout-service",
  "scripts": { "start": "node dist/index.js" },
  "dependencies": { "express": "^4.19.0", "react": "^18.2.0" },
  "devDependencies": { "vitest": "^1.6.0" }
}
""",
    "requirements.txt": "flask==3.0.0\ngunicorn==21.2.0\n",
}

PACKAGE_JSON = MANIFEST_BODIES["package.json"]

COMMITS: list[dict[str, Any]] = [
    {
        "sha": "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2",
        "message": "Add retry to payment capture",
        "author": "Dana",
        "authored_at": "2024-04-30T17:20:00Z",
        "author_login": "dana",
    },
    {
        "sha": "b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3",
        "message": "Fix currency rounding in totals",
        "author": "Sam",
        "authored_at": "2024-04-28T11:02:00Z",
        "authored_at_raw": None,
        "author_login": "sam",
    },
]

ISSUES: list[dict[str, Any]] = [
    {
        "number": 12,
        "title": "Totals drift for multi-currency carts",
        "state": "open",
        "labels": ["bug"],
        "comments": 4,
        "author": "priya",
        "assignees": [{"login": "sam"}],
        "created_at": "2024-04-20T08:00:00Z",
        "updated_at": "2024-04-29T08:00:00Z",
        "closed_at": None,
    }
]

PULL_REQUESTS: list[dict[str, Any]] = [
    {
        "number": 31,
        "title": "Bump postgres client to 8.13",
        "state": "open",
        "draft": False,
        "merged": False,
        "head": "chore/pg-8-13",
        "base": "main",
        "author": "dana",
        "created_at": "2024-04-29T12:00:00Z",
        "updated_at": "2024-04-29T12:00:00Z",
    }
]

BRANCHES: list[dict[str, Any]] = [
    {"name": "main", "protected": True, "commit_sha": "a1b2c3d4e5f6"},
    {"name": "release/2024-05", "protected": False, "commit_sha": "c3d4e5f6a1b2"},
]


class FakeGitHubMCP:
    """Stands in for the MCP manager, recording every call.

    Only the read tools the workflow uses are implemented. Any other tool name —
    especially a write — raises, so a test fails loudly if the workflow ever tries
    to mutate anything.
    """

    def __init__(
        self,
        *,
        commits: list[dict[str, Any]] | None = None,
        issues: list[dict[str, Any]] | None = None,
        fail: set[str] | None = None,
    ) -> None:
        self.calls: list[ToolCallRequest] = []
        self.commits = COMMITS if commits is None else commits
        self.issues = ISSUES if issues is None else issues
        self.fail = fail or set()

    @property
    def policy(self) -> Any:
        from app.services.mcp_policy import build_policy

        return build_policy()

    @property
    def connected_servers(self) -> list[str]:
        return ["github"]

    async def discover_tools(self) -> list[Any]:
        return []

    async def call_tool(self, request: ToolCallRequest) -> ToolCallResult:
        self.calls.append(request)
        tool = request.tool_name
        args = request.arguments

        if tool in self.fail:
            return ToolCallResult(
                tool_name=tool,
                qualified_name=tool,
                server="github",
                success=False,
                is_error=True,
                error_code="invalid_tool",
                error_message="boom",
                requested_by=request.requested_by,
            )

        assert tool.startswith("github."), f"unexpected server in {tool!r}"
        name = tool.split(".", 1)[1]

        if name == "get_repository":
            payload: Any = dict(REPOSITORY_METADATA)
        elif name == "list_files":
            payload = {
                "ref": args.get("ref"),
                "path": "",
                "files": FILE_TREE,
                "count": len(FILE_TREE),
                "truncated": False,
            }
        elif name == "get_file_contents":
            body = MANIFEST_BODIES.get(args["path"])
            assert body is not None, f"unexpected file read {args['path']!r}"
            payload = {
                "path": args["path"],
                "sha": "abc",
                "size": len(body),
                "encoding": "utf-8",
                "content": body,
                "truncated": False,
            }
        elif name == "list_commits":
            payload = {"commits": self.commits, "count": len(self.commits)}
        elif name == "list_branches":
            payload = {"branches": BRANCHES, "count": len(BRANCHES)}
        elif name == "list_issues":
            payload = {"issues": self.issues, "count": len(self.issues), "state": "open"}
        elif name == "list_pull_requests":
            payload = {
                "pull_requests": PULL_REQUESTS,
                "count": len(PULL_REQUESTS),
                "state": "open",
            }
        else:
            raise AssertionError(f"workflow called an unexpected tool: {tool!r}")

        return ToolCallResult(
            tool_name=tool,
            qualified_name=tool,
            server="github",
            success=True,
            content=payload,
            duration_ms=1.0,
            requested_by=request.requested_by,
        )

    def tools_called(self) -> list[str]:
        return [call.tool_name for call in self.calls]


@pytest.fixture
def fake_github(monkeypatch: pytest.MonkeyPatch) -> FakeGitHubMCP:
    fake = FakeGitHubMCP()
    set_manager(fake)  # type: ignore[arg-type]
    yield fake
    set_manager(None)


def _request(**overrides: Any) -> GitHubRepositoryAnalysisRequest:
    payload: dict[str, Any] = {"owner": "acme", "repository": "checkout-service"}
    payload.update(overrides)
    return GitHubRepositoryAnalysisRequest(**payload)


# --- Workflow ------------------------------------------------------------


async def test_analysis_builds_a_profile(fake_github: FakeGitHubMCP) -> None:
    profile, _readiness = await analyze_github_repository(_request())

    assert profile.source == "github"
    assert profile.full_name == "acme/checkout-service"
    assert profile.owner == "acme"
    assert profile.analysed_ref == "main"
    assert profile.default_branch == "main"
    assert profile.root == "github://acme/checkout-service@main"


async def test_analysis_detects_the_stack_from_remote_manifests(
    fake_github: FakeGitHubMCP,
) -> None:
    profile, _ = await analyze_github_repository(_request())

    assert "typescript" in profile.languages
    assert profile.has_dockerfile is True
    assert "github-actions" in profile.ci_cd
    assert profile.test_file_count >= 1
    assert profile.has_readme is True
    # The manifest was fetched over MCP and parsed by the shared extractor.
    assert "github.get_file_contents" in fake_github.tools_called()


async def test_workflow_only_calls_read_tools(fake_github: FakeGitHubMCP) -> None:
    """No write may be reachable from this workflow."""
    await analyze_github_repository(_request(include_issues=True))

    for tool in fake_github.tools_called():
        assert tool.startswith("github.")
        assert "create" not in tool
        assert "commit" not in tool or "list_commits" in tool
        assert "push" not in tool


async def test_requested_branch_overrides_the_default(
    fake_github: FakeGitHubMCP,
) -> None:
    profile, _ = await analyze_github_repository(_request(branch="release/2024-05"))

    assert profile.analysed_ref == "release/2024-05"
    list_files = next(c for c in fake_github.calls if c.tool_name == "github.list_files")
    assert list_files.arguments["ref"] == "release/2024-05"


async def test_every_call_records_the_requesting_agent(
    fake_github: FakeGitHubMCP,
) -> None:
    await analyze_github_repository(_request(include_issues=True))

    assert fake_github.calls
    for call in fake_github.calls:
        assert call.requested_by == "github-repository-analysis"
        assert call.reason


async def test_commits_and_branches_are_recorded(fake_github: FakeGitHubMCP) -> None:
    profile, _ = await analyze_github_repository(_request())

    assert len(profile.recent_commits) == 2
    assert profile.recent_commits[0].message == "Add retry to payment capture"
    assert {branch.name for branch in profile.branches} == {"main", "release/2024-05"}


async def test_issues_are_skipped_unless_requested(fake_github: FakeGitHubMCP) -> None:
    profile, _ = await analyze_github_repository(_request())

    assert "github.list_issues" not in fake_github.tools_called()
    assert profile.issues_inspected is False
    assert profile.open_issues == []
    # Absence must not be reported as "there are no issues".
    assert any("not inspected" in note for note in profile.notes)


async def test_issues_are_fetched_when_requested(fake_github: FakeGitHubMCP) -> None:
    profile, _ = await analyze_github_repository(_request(include_issues=True))

    assert "github.list_issues" in fake_github.tools_called()
    assert profile.issues_inspected is True
    assert [issue.number for issue in profile.open_issues] == [12]
    assert profile.open_issues[0].labels == ["bug"]
    assert [pull.number for pull in profile.open_pull_requests] == [31]


async def test_commit_failure_does_not_lose_the_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Commit history is context; losing it must not fail the analysis."""
    fake = FakeGitHubMCP(fail={"github.list_commits"})
    set_manager(fake)  # type: ignore[arg-type]
    try:
        profile, readiness = await analyze_github_repository(_request())
    finally:
        set_manager(None)

    assert profile.primary_language is not None
    assert profile.recent_commits == []
    assert any("commits could not be read" in note for note in profile.notes)
    # Readiness must not invent certainty about what it could not see.
    assert readiness.ready is True


async def test_repository_failure_is_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.exceptions import UpstreamError

    fake = FakeGitHubMCP(fail={"github.get_repository"})
    set_manager(fake)  # type: ignore[arg-type]
    try:
        with pytest.raises(UpstreamError, match="get_repository"):
            await analyze_github_repository(_request())
    finally:
        set_manager(None)


# --- Readiness -----------------------------------------------------------


async def test_ready_repository_scores_well(fake_github: FakeGitHubMCP) -> None:
    profile, readiness = await analyze_github_repository(_request(include_issues=True))

    assert readiness.ready is True
    assert readiness.blockers == []
    assert readiness.score >= 85
    assert any(
        check.name == "container_build" and check.status == "pass" for check in readiness.checks
    )
    # The assessment must be explainable, not a bare number.
    assert readiness.checks
    assert readiness.notes


async def test_archived_repository_is_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeGitHubMCP()
    original = fake.call_tool

    async def archived(request: ToolCallRequest) -> ToolCallResult:
        result = await original(request)
        if request.tool_name == "github.get_repository":
            content = dict(result.content)
            content["archived"] = True
            result = result.model_copy(update={"content": content})
        return result

    set_manager(fake)  # type: ignore[arg-type]
    try:
        _baseline_profile, baseline = await analyze_github_repository(_request())
        fake.call_tool = archived  # type: ignore[method-assign]
        _profile, readiness = await analyze_github_repository(_request())
    finally:
        set_manager(None)

    assert baseline.ready is True
    assert readiness.ready is False
    assert any("archived" in blocker for blocker in readiness.blockers)
    assert readiness.score < baseline.score


async def test_missing_ci_tests_and_dockerfile_are_blockers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A repository with no build, no tests and no CI is not deployable."""
    bare_tree = [
        {"path": "README.md", "type": "blob", "size": 10},
        {"path": "requirements.txt", "type": "blob", "size": 40},
        {"path": "app.py", "type": "blob", "size": 100},
    ]

    fake = FakeGitHubMCP()
    original = fake.call_tool

    async def bare(request: ToolCallRequest) -> ToolCallResult:
        result = await original(request)
        if request.tool_name == "github.list_files":
            result = result.model_copy(
                update={
                    "content": {
                        "ref": request.arguments.get("ref"),
                        "path": "",
                        "files": bare_tree,
                        "count": len(bare_tree),
                        "truncated": False,
                    }
                }
            )
        return result

    fake.call_tool = bare  # type: ignore[method-assign]
    set_manager(fake)  # type: ignore[arg-type]
    try:
        _profile, readiness = await analyze_github_repository(_request())
    finally:
        set_manager(None)

    assert readiness.ready is False
    assert len(readiness.blockers) >= 3
    assert readiness.score < 50
    names = {check.name for check in readiness.checks}
    assert {"container_build", "continuous_integration", "tests"} <= names


async def test_large_issue_backlog_warns(fake_github: FakeGitHubMCP) -> None:
    fake = FakeGitHubMCP(issues=[dict(issue, number=n) for n, issue in enumerate(ISSUES * 30, 1)])
    set_manager(fake)  # type: ignore[arg-type]
    try:
        _profile, readiness = await analyze_github_repository(_request(include_issues=True))
    finally:
        set_manager(None)

    assert any("open issues" in warning for warning in readiness.warnings)
    assert any(
        check.name == "issue_backlog" and check.status == "warn" for check in readiness.checks
    )


async def test_issue_backlog_unknown_when_not_inspected(
    fake_github: FakeGitHubMCP,
) -> None:
    _profile, readiness = await analyze_github_repository(_request())
    check = next(c for c in readiness.checks if c.name == "issue_backlog")
    assert check.status == "unknown"


def test_readiness_score_is_bounded() -> None:
    """The model must not be constructible with an arbitrary score."""
    with pytest.raises(ValueError):
        DeploymentReadiness(score=150, ready=False)
    with pytest.raises(ValueError):
        DeploymentReadiness(score=-1, ready=False)


# --- Credentials ---------------------------------------------------------


def test_token_is_a_secret_in_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AEGIS_GITHUB__TOKEN", "ghp_supersecrettoken1234567890")
    get_settings.cache_clear()
    try:
        settings = get_settings()
        assert settings.github.is_configured is True
        # repr and the log-safe summary must not reveal the value.
        assert "supersecret" not in repr(settings)
        assert "supersecret" not in str(settings.safe_summary())
        # The value is still retrievable for the code that needs to use it.
        token = settings.github.token
        assert token is not None
        assert token.get_secret_value() == "ghp_supersecrettoken1234567890"
    finally:
        get_settings.cache_clear()


def test_safe_summary_reports_github_without_the_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AEGIS_GITHUB__TOKEN", "ghp_supersecrettoken1234567890")
    get_settings.cache_clear()
    try:
        summary = get_settings().safe_summary()
        assert summary["configured_integrations"]["github"] is True
        assert "supersecret" not in str(summary)
    finally:
        get_settings.cache_clear()


def test_github_server_scrubs_the_token() -> None:
    """The server's scrubber must remove a token from arbitrary text."""
    import importlib
    import sys
    from pathlib import Path

    server_path = Path(__file__).resolve().parents[2] / "mcp_servers" / "github" / "server.py"
    sys.path.insert(0, str(server_path.parent))
    try:
        module = importlib.import_module("server")
        importlib.reload(module)
        module.os.environ["AEGIS_GITHUB__TOKEN"] = "ghp_supersecrettoken1234567890"
        assert module._scrub("failed for ghp_supersecrettoken1234567890") == "failed for [redacted]"
    finally:
        sys.path.pop(0)
        sys.modules.pop("server", None)


def test_github_server_requires_a_token(monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib
    import sys
    from pathlib import Path

    server_path = Path(__file__).resolve().parents[2] / "mcp_servers" / "github" / "server.py"
    sys.path.insert(0, str(server_path.parent))
    try:
        module = importlib.import_module("server")
        importlib.reload(module)
        monkeypatch.delenv("AEGIS_GITHUB__TOKEN", raising=False)
        with pytest.raises(module.GitHubAuthError):
            module._token()
    finally:
        sys.path.pop(0)
        sys.modules.pop("server", None)


def test_github_server_builds_an_authorization_header(monkeypatch: pytest.MonkeyPatch) -> None:
    """The token must travel in a header, never in a URL or an argv."""
    import importlib
    import sys
    from pathlib import Path
    from unittest.mock import patch

    server_path = Path(__file__).resolve().parents[2] / "mcp_servers" / "github" / "server.py"
    sys.path.insert(0, str(server_path.parent))
    try:
        module = importlib.import_module("server")
        importlib.reload(module)
        monkeypatch.setenv("AEGIS_GITHUB__TOKEN", "ghp_tokenvalue")

        captured: dict[str, Any] = {}

        class FakeResponse:
            def __enter__(self) -> FakeResponse:
                return self

            def __exit__(self, *_: object) -> None:
                return None

            def read(self) -> bytes:
                return b'{"default_branch": "main"}'

        def fake_urlopen(request: Any, timeout: float = 0) -> FakeResponse:
            captured["url"] = request.full_url
            captured["headers"] = dict(request.header_items())
            captured["timeout"] = timeout
            return FakeResponse()

        with patch.object(module.urllib.request, "urlopen", fake_urlopen):
            module._request("/repos/acme/checkout-service")

        assert "ghp_tokenvalue" not in captured["url"]
        assert captured["headers"]["Authorization"] == "Bearer ghp_tokenvalue"
        assert captured["headers"]["Accept"] == "application/vnd.github+json"
    finally:
        sys.path.pop(0)
        sys.modules.pop("server", None)


def test_github_server_error_does_not_leak_the_token(monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib
    import io
    import sys
    import urllib.error
    from pathlib import Path
    from unittest.mock import patch

    server_path = Path(__file__).resolve().parents[2] / "mcp_servers" / "github" / "server.py"
    sys.path.insert(0, str(server_path.parent))
    try:
        module = importlib.import_module("server")
        importlib.reload(module)
        monkeypatch.setenv("AEGIS_GITHUB__TOKEN", "ghp_tokenvalue")

        body = io.BytesIO(b'{"message": "Bad credentials for ghp_tokenvalue"}')
        error = urllib.error.HTTPError("url", 401, "Unauthorized", {}, body)

        # A ToolError is an anticipated failure, so the SDK returns this message
        # to the caller rather than hiding it as a crash.
        with (
            patch.object(module.urllib.request, "urlopen", side_effect=error),
            pytest.raises(module.ToolError) as caught,
        ):
            module._request("/repos/acme/checkout-service")

        assert "ghp_tokenvalue" not in str(caught.value)
        assert "[redacted]" in str(caught.value)
    finally:
        sys.path.pop(0)
        sys.modules.pop("server", None)


def test_github_server_never_exposes_a_mutating_tool() -> None:
    """The read-only promise is checked against the server's real tool list."""
    import importlib
    import sys
    from pathlib import Path

    server_path = Path(__file__).resolve().parents[2] / "mcp_servers" / "github" / "server.py"
    sys.path.insert(0, str(server_path.parent))
    try:
        module = importlib.import_module("server")
        importlib.reload(module)
        tools = module.server._tool_manager.list_tools()
        names = {tool.name for tool in tools}

        assert {
            "get_repository",
            "list_branches",
            "list_commits",
            "list_issues",
            "list_pull_requests",
            "list_files",
            "get_file_contents",
        } <= names

        from app.services.mcp_policy import is_command_execution_tool

        for name in names:
            assert not is_command_execution_tool(name), f"{name} would be refused by policy"
            assert not any(
                verb in name
                for verb in ("create", "update", "delete", "merge", "push", "open_pull")
            ), f"{name} looks mutating"
    finally:
        sys.path.pop(0)
        sys.modules.pop("server", None)


def test_forwarded_environment_is_named_not_valued(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the names are forwarded, so an unnamed secret cannot leak."""
    from app.core.config import Settings

    monkeypatch.setenv("AEGIS_MCP__FORWARD_ENVIRONMENT", "AEGIS_GITHUB__TOKEN, HOME")
    monkeypatch.setenv("AEGIS_LLM__API_KEY", "sk-should-not-be-forwarded")
    get_settings.cache_clear()
    try:
        settings = Settings()
        assert settings.mcp.forward_environment == ["AEGIS_GITHUB__TOKEN", "HOME"]
        assert "AEGIS_LLM__API_KEY" not in settings.mcp.forward_environment
    finally:
        get_settings.cache_clear()


# --- Request validation --------------------------------------------------


@pytest.mark.parametrize("field", ["owner", "repository"])
@pytest.mark.parametrize("value", ["acme/other", "../etc", "acme repo", "acme;rm"])
def test_owner_and_repository_reject_path_syntax(field: str, value: str) -> None:
    with pytest.raises(ValueError):
        GitHubRepositoryAnalysisRequest(**{field: value, "repository": "x"})


def test_leading_dot_is_rejected() -> None:
    with pytest.raises(ValueError):
        GitHubRepositoryAnalysisRequest(owner=".hidden", repository="repo")


# --- API -----------------------------------------------------------------


@pytest.fixture
def github_client(monkeypatch: pytest.MonkeyPatch) -> Any:
    """A client whose workflow is backed by the mocked MCP manager.

    The patch targets the agent module, which imports ``get_manager`` directly, and
    it has to win over the lifespan that installs a real manager at startup.
    """
    fake = FakeGitHubMCP()
    monkeypatch.setattr("app.agents.github_repository_analysis.get_manager", lambda: fake)
    from app.main import create_app

    with TestClient(create_app()) as client:
        yield client


def test_analyze_endpoint_returns_profile_and_readiness(github_client: TestClient) -> None:
    response = github_client.post(
        "/api/github/repository/analyze",
        json={"owner": "acme", "repository": "checkout-service", "include_issues": True},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["profile"]["full_name"] == "acme/checkout-service"
    assert body["profile"]["source"] == "github"
    assert body["readiness"]["ready"] is True
    assert body["readiness"]["score"] > 0


def test_analyze_endpoint_rejects_a_traversal_owner(github_client: TestClient) -> None:
    response = github_client.post(
        "/api/github/repository/analyze",
        json={"owner": "../acme", "repository": "checkout-service"},
    )
    assert response.status_code == 422


def test_analyze_endpoint_response_has_no_credential_field(github_client: TestClient) -> None:
    response = github_client.post(
        "/api/github/repository/analyze",
        json={"owner": "acme", "repository": "checkout-service"},
    )

    body = response.text.lower()
    for forbidden in ("token", "authorization", "bearer", "ghp_", "password", "secret"):
        assert forbidden not in body, f"response leaked {forbidden!r}"


def test_analyze_endpoint_passes_the_branch(github_client: TestClient) -> None:
    response = github_client.post(
        "/api/github/repository/analyze",
        json={"owner": "acme", "repository": "checkout-service", "branch": "release/2024-05"},
    )
    assert response.status_code == 200
    assert response.json()["profile"]["analysed_ref"] == "release/2024-05"


def test_profile_model_carries_no_secret_fields() -> None:
    """Guard the contract: a credential field must never be added here."""
    fields = set(GitHubRepositoryProfile.model_fields)
    for forbidden in ("token", "authorization", "credential", "secret", "password"):
        assert not any(forbidden in field for field in fields)
