"""Tests for the unified deployment planning workflow.

Repository fixtures are defined as *file trees plus file bodies*, exactly as the
read-only GitHub tools would return them, so the planner is exercised against the
same shapes it sees in production rather than against hand-built profile objects.
A hand-built profile would let the planner pass while disagreeing with the
detector that feeds it.

The central safety property under test is that planning stays read-only: no
fixture may cause the workflow to call a tool that writes.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.agents import deployment_planner as planner
from app.agents.deployment_planner import plan_repository_deployment
from app.core.exceptions import NotFoundError
from app.models.deployment_plan import DeploymentPlanRequest
from app.models.mcp import ToolCallRequest, ToolCallResult

#: The workflow is async; anyio's plugin supplies the asyncio backend.
pytestmark = pytest.mark.anyio

#: Tools that would change something. The workflow must never call one; the fake
#: raises on them so a regression fails loudly instead of passing quietly.
WRITE_TOOLS = {
    "create_commit",
    "create_branch",
    "create_pull_request",
    "create_issue",
    "create_repository",
    "push_files",
    "merge_pull_request",
}


class FakeGitHubRepo:
    """A stand-in GitHub server backed by a file tree and file bodies.

    Only the read tools the workflow uses are implemented. Any other name raises,
    which is what makes the read-only assertions real rather than decorative.
    """

    def __init__(
        self,
        *,
        tree: list[str],
        bodies: dict[str, str] | None = None,
        name: str = "demo",
        default_branch: str = "main",
        commits: list[dict[str, Any]] | None = None,
        unreadable: set[str] | None = None,
    ) -> None:
        self.tree = tree
        self.bodies = bodies or {}
        self.name = name
        self.default_branch = default_branch
        self.commits = (
            commits
            if commits is not None
            else [
                {
                    "sha": "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2",
                    "message": "Initial commit",
                    "author": "Dana",
                    "authored_at": "2024-04-30T17:20:00Z",
                    "author_login": "dana",
                }
            ]
        )
        self.unreadable = unreadable or set()
        self.calls: list[ToolCallRequest] = []

    async def discover_tools(self) -> list[Any]:
        return []

    def tools_called(self) -> list[str]:
        return [call.tool_name.split(".", 1)[1] for call in self.calls]

    async def call_tool(self, request: ToolCallRequest) -> ToolCallResult:
        self.calls.append(request)
        tool = request.tool_name
        name = tool.split(".", 1)[1]

        assert tool.startswith("github."), f"unexpected server in {tool!r}"
        assert name not in WRITE_TOOLS, f"workflow attempted a write: {name!r}"

        args = request.arguments

        if name == "get_repository":
            payload: Any = {
                "name": self.name,
                "full_name": f"octocat/{self.name}",
                "default_branch": self.default_branch,
                "private": False,
                "archived": False,
                "fork": False,
                "description": "A fixture repository.",
                "language": None,
                "default_branch_protected": False,
                "url": f"https://github.com/octocat/{self.name}",
            }
        elif name == "list_files":
            payload = {
                "ref": args.get("ref"),
                "path": "",
                "files": [{"path": path, "type": "file"} for path in self.tree],
                "count": len(self.tree),
                "truncated": False,
            }
        elif name == "get_file_contents":
            path = args["path"]
            if path in self.unreadable:
                return ToolCallResult(
                    tool_name=tool,
                    qualified_name=tool,
                    server="github",
                    success=False,
                    error_code="not_found",
                    error_message=f"{path} could not be read",
                    requested_by=request.requested_by,
                )
            body = self.bodies.get(path)
            assert body is not None, f"unexpected file read {path!r}"
            payload = {
                "path": path,
                "sha": "abc",
                "size": len(body),
                "encoding": "utf-8",
                "content": body,
                "truncated": False,
            }
        elif name == "list_commits":
            payload = {"commits": self.commits, "count": len(self.commits)}
        elif name == "list_branches":
            payload = {
                "branches": [
                    {"name": self.default_branch, "protected": True, "commit_sha": "a1b2c3"}
                ],
                "count": 1,
            }
        elif name == "list_issues":
            payload = {"issues": [], "count": 0, "state": "open"}
        elif name == "list_pull_requests":
            payload = {"pull_requests": [], "count": 0, "state": "open"}
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


# ---------------------------------------------------------------------------
# Repository fixtures: one per repository type
# ---------------------------------------------------------------------------

NODE_EXPRESS = {
    "name": "express-shop",
    "tree": [
        "package.json",
        "package-lock.json",
        "server.js",
        ".env.example",
        "Dockerfile",
        ".dockerignore",
        "test/server.test.js",
        ".github/workflows/ci.yml",
    ],
    "bodies": {
        "package.json": '{"name":"shop","dependencies":{"express":"^4.18.0","pg":"^8.11.0"},'
        '"devDependencies":{"jest":"^29.7.0"}}',
        "package-lock.json": '{"lockfileVersion":3}',
        ".env.example": "PORT=3000\nDATABASE_URL=postgres://user:pass@db:5432/shop\n"
        "SESSION_SECRET=replace-me-with-a-real-secret\n",
        "Dockerfile": (
            "FROM node:20-alpine\n"
            "WORKDIR /app\n"
            "COPY package*.json ./\n"
            "RUN npm ci --omit=dev\n"
            "COPY . .\n"
            "USER node\n"
            "EXPOSE 3000\n"
            "HEALTHCHECK --interval=30s CMD wget -qO- http://localhost:3000/health || exit 1\n"
            'CMD ["node", "server.js"]\n'
        ),
        ".dockerignore": "node_modules\n",
        "test/server.test.js": "test('works', () => expect(1).toBe(1));\n",
        ".github/workflows/ci.yml": "name: ci\non: push\n",
    },
}

PYTHON_BARE = {
    "name": "flask-notes",
    "tree": [
        "requirements.txt",
        "app.py",
        ".env.example",
        "README.md",
    ],
    "bodies": {
        "requirements.txt": "flask==3.0.0\npsycopg2-binary==2.9.9\n",
        "app.py": "from flask import Flask\napp = Flask(__name__)\n",
        # Placeholders: this is the repository that must end up blocked.
        ".env.example": "DATABASE_URL=changeme\nSECRET_KEY=your-secret-here\nDEBUG=\n",
        "README.md": "# Flask Notes\n",
    },
}

GO_SERVICE = {
    "name": "orders-api",
    "tree": [
        "go.mod",
        "main.go",
        "main_test.go",
        "Dockerfile",
    ],
    "bodies": {
        "go.mod": (
            "module example.com/orders\n\ngo 1.22\n\nrequire github.com/redis/go-redis/v9 v9.5.0\n"
        ),
        "main.go": "package main\n\nfunc main() {}\n",
        "main_test.go": 'package main\n\nimport "testing"\n\nfunc TestMain(t *testing.T) {}\n',
        # A Dockerfile with no HEALTHCHECK and no USER: two separate findings.
        "Dockerfile": "FROM golang:1.22-alpine\nRUN go build -o /app ./...\nEXPOSE 8080\n",
    },
}

JAVA_COMPOSE = {
    "name": "billing-service",
    "tree": [
        "pom.xml",
        "src/main/java/App.java",
        "src/test/java/AppTest.java",
        "docker-compose.yml",
        "Dockerfile",
        ".env.sample",
    ],
    "bodies": {
        "pom.xml": "<project><dependencies><dependency>org.postgresql</dependency>"
        "</dependencies></project>",
        "src/main/java/App.java": "class App {}\n",
        "src/test/java/AppTest.java": "class AppTest {}\n",
        "docker-compose.yml": (
            "services:\n  db:\n    image: postgres:16\n  cache:\n    image: redis:7\n"
            "  web:\n    build: .\n"
        ),
        "Dockerfile": "FROM eclipse-temurin:21-jre\nEXPOSE 8080\n",
        ".env.sample": "SPRING_DATASOURCE_URL=jdbc:postgresql://db:5432/billing\nSTRIPE_API_KEY=xxxxx\n",
    },
}

EMPTY_REPO = {
    "name": "empty",
    "tree": ["README.md"],
    "bodies": {"README.md": "# Nothing here yet\n"},
}

ALL_FIXTURES = {
    "node_express": NODE_EXPRESS,
    "python_bare": PYTHON_BARE,
    "go_service": GO_SERVICE,
    "java_compose": JAVA_COMPOSE,
    "empty_repo": EMPTY_REPO,
}


@pytest.fixture
def fake_github(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Install a fake manager. Use :meth:`FakeGitHubRepo.serve` to choose a fixture."""

    def factory(**kwargs: Any) -> FakeGitHubRepo:
        fake = FakeGitHubRepo(**kwargs)
        monkeypatch.setattr("app.agents.github_repository_analysis.get_manager", lambda: fake)
        return fake

    return factory


async def _plan(fake: FakeGitHubRepo, **kwargs: Any) -> Any:
    return await plan_repository_deployment(
        DeploymentPlanRequest(owner="octocat", repository=fake.name, **kwargs)
    )


# ---------------------------------------------------------------------------
# Rule: Dockerfile present -> Docker
# ---------------------------------------------------------------------------


async def test_dockerfile_selects_docker_build(fake_github: Any) -> None:
    plan = await _plan(fake_github(**NODE_EXPRESS))

    assert plan.build_strategy.approach == "docker"
    assert "docker.build_image" in plan.build_strategy.tools
    assert plan.deployment_strategy.approach == "docker-container"
    assert any("node:20-alpine" in item for item in plan.build_strategy.rationale)
    assert plan.build_strategy.recommended_changes == [], (
        "A Dockerfile with a non-root USER needs no changes."
    )


async def test_missing_dockerfile_recommends_generation(fake_github: Any) -> None:
    plan = await _plan(fake_github(**PYTHON_BARE))

    assert plan.build_strategy.approach == "generate-dockerfile"
    assert plan.build_strategy.tools == []
    assert plan.build_strategy.recommended_changes, "must say what to add"
    assert any(
        "not create or modify" in change for change in plan.build_strategy.recommended_changes
    )
    assert plan.deployment_strategy.approach == "blocked-no-dockerfile"


async def test_planner_never_writes_to_the_repository(fake_github: Any) -> None:
    """The explicit requirement: recommend a Dockerfile, never create one."""
    fake = fake_github(**PYTHON_BARE)
    await _plan(fake)

    for name in fake.tools_called():
        assert name not in WRITE_TOOLS
    assert "create_commit" not in fake.tools_called()
    assert "push_files" not in fake.tools_called()


# ---------------------------------------------------------------------------
# Rule: tests present -> include execution; absent -> limited
# ---------------------------------------------------------------------------


async def test_tests_present_are_included_as_a_gate(fake_github: Any) -> None:
    plan = await _plan(fake_github(**NODE_EXPRESS))

    assert plan.detected_stack.test_file_count > 0
    assert plan.test_strategy.approach == "run-suite"
    assert any(step.title == "Run the test suite" for step in plan.ordered_steps)


async def test_no_tests_marks_testing_limited(fake_github: Any) -> None:
    plan = await _plan(fake_github(**PYTHON_BARE))

    assert plan.detected_stack.test_file_count == 0
    assert plan.test_strategy.approach == "limited-no-tests"
    assert plan.test_strategy.commands == [], "must not invent a test command"
    assert any(limit for limit in plan.test_strategy.limitations)
    assert any(risk.id == "no-tests" for risk in plan.risks)


async def test_go_tests_are_detected(fake_github: Any) -> None:
    plan = await _plan(fake_github(**GO_SERVICE))

    assert plan.detected_stack.test_file_count == 1
    assert plan.test_strategy.approach != "limited-no-tests"


# ---------------------------------------------------------------------------
# Rule: unresolved required env var -> blocked
# ---------------------------------------------------------------------------


async def test_unresolved_environment_variable_blocks(fake_github: Any) -> None:
    plan = await _plan(fake_github(**PYTHON_BARE))

    assert plan.blocked is True
    assert any("DATABASE_URL" in reason for reason in plan.blocked_by)
    assert any("SECRET_KEY" in reason for reason in plan.blocked_by)

    names = {variable.name for variable in plan.unresolved_environment_variables}
    assert {"DATABASE_URL", "SECRET_KEY"} <= names


async def test_placeholder_values_are_not_treated_as_resolved(fake_github: Any) -> None:
    plan = await _plan(fake_github(**PYTHON_BARE))

    for variable in plan.required_environment_variables:
        if variable.name in {"DATABASE_URL", "SECRET_KEY"}:
            assert variable.resolved is False
            assert variable.unresolved_reason == "placeholder"


async def test_empty_value_is_reported_as_empty_not_placeholder(fake_github: Any) -> None:
    plan = await _plan(fake_github(**PYTHON_BARE))

    debug = next(v for v in plan.required_environment_variables if v.name == "DEBUG")
    assert debug.resolved is False
    assert debug.unresolved_reason == "empty"


async def test_resolved_environment_variables_do_not_block(fake_github: Any) -> None:
    """`replace-me` is still a placeholder, so this repo must remain blocked."""
    plan = await _plan(fake_github(**NODE_EXPRESS))

    port = next(v for v in plan.required_environment_variables if v.name == "PORT")
    database = next(v for v in plan.required_environment_variables if v.name == "DATABASE_URL")
    assert port.resolved is True
    assert database.resolved is True
    session = next(v for v in plan.required_environment_variables if v.name == "SESSION_SECRET")
    assert session.resolved is False
    assert session.unresolved_reason == "placeholder"


async def test_placeholder_values_are_never_echoed(fake_github: Any) -> None:
    plan = await _plan(fake_github(**PYTHON_BARE))

    for variable in plan.required_environment_variables:
        if not variable.resolved:
            assert "value withheld" in (variable.value_hint or "") or variable.value_hint in {
                "(empty)",
                "(set)",
            }


async def test_env_block_propagates_to_the_run_step(fake_github: Any) -> None:
    plan = await _plan(fake_github(**PYTHON_BARE))

    run_step = next(step for step in plan.ordered_steps if step.title == "Start the container")
    assert run_step.blocked is True
    assert "DATABASE_URL" in (run_step.blocked_reason or "")


# ---------------------------------------------------------------------------
# Rule: all required fields present
# ---------------------------------------------------------------------------


REQUIRED_FIELDS = (
    "repository",
    "detected_stack",
    "build_strategy",
    "test_strategy",
    "deployment_strategy",
    "required_environment_variables",
    "required_services",
    "health_check_strategy",
    "rollback_strategy",
    "risks",
    "approval_requirements",
    "ordered_steps",
)


@pytest.mark.parametrize("fixture_name", sorted(ALL_FIXTURES))
async def test_plan_contains_every_required_field(fake_github: Any, fixture_name: str) -> None:
    plan = await _plan(fake_github(**ALL_FIXTURES[fixture_name]))

    for field in REQUIRED_FIELDS:
        assert hasattr(plan, field), f"{fixture_name}: missing {field}"
        getattr(plan, field)  # not None

    assert plan.repository.full_name.startswith("octocat/")
    assert plan.repository.owner == "octocat"
    assert plan.repository.ref


# ---------------------------------------------------------------------------
# Human-readable and machine-readable
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture_name", sorted(ALL_FIXTURES))
async def test_markdown_and_structured_form_agree(fake_github: Any, fixture_name: str) -> None:
    plan = await _plan(fake_github(**ALL_FIXTURES[fixture_name]))
    markdown = plan.summary_markdown()

    assert markdown.startswith(f"# Deployment plan: {plan.repository.full_name}")
    for heading in (
        "## Build",
        "## Tests",
        "## Deployment",
        "## Health checks",
        "## Rollback",
        "## Ordered steps",
    ):
        assert heading in markdown, f"{fixture_name}: missing {heading}"

    for step in plan.ordered_steps:
        assert step.title in markdown, f"{fixture_name}: step {step.title} not rendered"
    assert plan.blocked == ("## Blocked" in markdown)


async def test_markdown_shows_blocked_reasons_and_placeholders(fake_github: Any) -> None:
    plan = await _plan(fake_github(**PYTHON_BARE))
    markdown = plan.summary_markdown()

    assert "## Blocked" in markdown
    assert "DATABASE_URL" in markdown
    assert "⛔" in markdown, "a blocked step must be visibly marked"


async def test_markdown_reports_limited_testing_honestly(fake_github: Any) -> None:
    plan = await _plan(fake_github(**PYTHON_BARE))
    markdown = plan.summary_markdown()

    assert "No test suite detected" in markdown
    assert "limited" in markdown.lower()


async def test_markdown_never_contains_a_resolved_secret(fake_github: Any) -> None:
    fake = fake_github(**PYTHON_BARE)
    plan = await _plan(fake)
    markdown = plan.summary_markdown()

    assert "postgres://user:pass" not in markdown
    assert "your-secret-here" not in markdown


# ---------------------------------------------------------------------------
# Strategy detail per repository type
# ---------------------------------------------------------------------------


async def test_declared_healthcheck_is_used(fake_github: Any) -> None:
    plan = await _plan(fake_github(**NODE_EXPRESS))

    assert plan.container_assets.has_healthcheck is True
    assert plan.container_assets.base_image == "node:20-alpine"
    assert plan.container_assets.exposed_ports == [3000]
    assert plan.container_assets.runs_as_non_root is True
    assert plan.health_check_strategy.approach == "docker-healthcheck"


async def test_missing_healthcheck_requires_definition(fake_github: Any) -> None:
    plan = await _plan(fake_github(**GO_SERVICE))

    assert plan.container_assets.has_healthcheck is False
    assert plan.health_check_strategy.approach == "http-probe-required"
    assert any(risk.id == "no-healthcheck" for risk in plan.risks)

    health_step = next(step for step in plan.ordered_steps if step.title == "Define a health check")
    assert health_step.blocked is True


async def test_container_runs_as_root_is_flagged(fake_github: Any) -> None:
    plan = await _plan(fake_github(**GO_SERVICE))

    assert plan.container_assets.runs_as_non_root is False
    assert any(risk.id == "runs-as-root" for risk in plan.risks)


async def test_compose_services_are_detected(fake_github: Any) -> None:
    plan = await _plan(fake_github(**JAVA_COMPOSE))

    assert plan.container_assets.compose_read is True
    assert {"db", "cache"} <= set(plan.container_assets.compose_services)
    assert "services" not in plan.container_assets.compose_services, (
        "the top-level 'services:' key is not a service"
    )
    service_names = {service.name for service in plan.required_services}
    assert "db" in service_names


async def test_services_inferred_from_environment_variables(fake_github: Any) -> None:
    plan = await _plan(fake_github(**PYTHON_BARE))

    service_names = {service.name for service in plan.required_services}
    assert "postgresql" in service_names
    for service in plan.required_services:
        assert service.detected_from, "every service must state its evidence"


async def test_rollback_differs_when_nothing_can_be_built(fake_github: Any) -> None:
    built = await _plan(fake_github(**NODE_EXPRESS))
    bare = await _plan(fake_github(**PYTHON_BARE))

    assert built.rollback_strategy.approach == "image-tag-rollback"
    assert bare.rollback_strategy.approach == "none-available"


async def test_rollback_warns_about_persistent_data(fake_github: Any) -> None:
    plan = await _plan(fake_github(**JAVA_COMPOSE))

    assert any("data" in limit.lower() for limit in plan.rollback_strategy.limitations)
    assert any(risk.id == "persistent-data" for risk in plan.risks)


async def test_no_local_aws_deployment_is_planned(fake_github: Any) -> None:
    plan = await _plan(fake_github(**NODE_EXPRESS))

    assert any("No AWS" in limit for limit in plan.deployment_strategy.limitations)
    assert all(
        step.command is None or "aws" not in step.command.lower() for step in plan.ordered_steps
    ), "no step may suggest an AWS command"


# ---------------------------------------------------------------------------
# Risks, approvals, ordering
# ---------------------------------------------------------------------------


async def test_blocking_risks_match_blocking_reasons(fake_github: Any) -> None:
    plan = await _plan(fake_github(**PYTHON_BARE))

    blocking_ids = {risk.id for risk in plan.risks if risk.blocking}
    assert any(item.startswith("env-unresolved:") for item in blocking_ids)
    assert "no-dockerfile" in blocking_ids
    assert plan.blocked is True


@pytest.mark.parametrize("fixture_name", sorted(ALL_FIXTURES))
async def test_every_risk_states_a_mitigation(fake_github: Any, fixture_name: str) -> None:
    plan = await _plan(fake_github(**ALL_FIXTURES[fixture_name]))

    for risk in plan.risks:
        assert risk.mitigation, f"{fixture_name}: risk {risk.id} has no mitigation"
        assert risk.detail, f"{fixture_name}: risk {risk.id} has no detail"


@pytest.mark.parametrize("fixture_name", sorted(ALL_FIXTURES))
async def test_approvals_are_required_and_explained(fake_github: Any, fixture_name: str) -> None:
    plan = await _plan(fake_github(**ALL_FIXTURES[fixture_name]))

    assert plan.requires_human_approval is True
    assert plan.approval_requirements
    for approval in plan.approval_requirements:
        assert approval.reason, f"{fixture_name}: approval {approval.action} has no reason"

    destructive = [
        approval for approval in plan.approval_requirements if approval.risk_level == "critical"
    ]
    assert destructive, "cloud deployment must always require the highest approval"


@pytest.mark.parametrize("fixture_name", sorted(ALL_FIXTURES))
async def test_steps_are_ordered_and_dependency_consistent(
    fake_github: Any, fixture_name: str
) -> None:
    plan = await _plan(fake_github(**ALL_FIXTURES[fixture_name]))

    orders = [step.order for step in plan.ordered_steps]
    assert orders == list(range(1, len(orders) + 1)), f"{fixture_name}: steps not sequential"

    for step in plan.ordered_steps:
        for dependency in step.depends_on:
            assert dependency < step.order, (
                f"{fixture_name}: step {step.order} depends on {dependency}, which is later"
            )


async def test_build_and_start_require_approval(fake_github: Any) -> None:
    plan = await _plan(fake_github(**NODE_EXPRESS))

    build = next(step for step in plan.ordered_steps if step.title == "Build the container image")
    run = next(step for step in plan.ordered_steps if step.title == "Start the container")
    assert build.requires_approval is True
    assert run.requires_approval is True
    assert build.tool == "docker.build_image"
    assert run.tool == "docker.start_container"


async def test_dockerfile_absence_blocks_downstream_steps(fake_github: Any) -> None:
    plan = await _plan(fake_github(**EMPTY_REPO))

    build = next(step for step in plan.ordered_steps if step.title == "Prepare the container build")
    image = next(step for step in plan.ordered_steps if step.title == "Build the container image")
    logs = next(step for step in plan.ordered_steps if step.title == "Collect startup logs")

    assert build.blocked is True
    assert build.automated is False, "a human must add the Dockerfile"
    assert image.blocked is True
    assert logs.blocked is True


async def test_no_ci_is_flagged(fake_github: Any) -> None:
    plan = await _plan(fake_github(**GO_SERVICE))

    assert plan.detected_stack.ci_cd == []
    assert any(risk.id == "no-ci" for risk in plan.risks)


# ---------------------------------------------------------------------------
# Read-only and failure behaviour
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture_name", sorted(ALL_FIXTURES))
async def test_only_read_tools_are_called(fake_github: Any, fixture_name: str) -> None:
    fake = fake_github(**ALL_FIXTURES[fixture_name])
    await _plan(fake)

    called = set(fake.tools_called())
    assert called <= {
        "get_repository",
        "list_files",
        "get_file_contents",
        "list_commits",
        "list_branches",
        "list_issues",
        "list_pull_requests",
    }, f"{fixture_name} called something unexpected: {called}"
    assert called.isdisjoint(WRITE_TOOLS)


async def test_unreadable_dockerfile_degrades_to_a_note(fake_github: Any) -> None:
    fixture = dict(NODE_EXPRESS)
    fake = fake_github(**fixture, unreadable={"Dockerfile"})
    plan = await _plan(fake)

    assert plan.container_assets.dockerfile_read is False
    assert any("Could not read Dockerfile" in note for note in plan.notes)
    assert plan.build_strategy.approach == "docker", (
        "the file exists, so the strategy holds even though it could not be read"
    )
    assert plan.build_strategy.confidence < 0.9, "unverified facts must lower confidence"


async def test_missing_env_template_leaves_no_invented_variables(fake_github: Any) -> None:
    fixture = dict(NODE_EXPRESS)
    tree = [path for path in fixture["tree"] if path != ".env.example"]
    bodies = {k: v for k, v in fixture["bodies"].items() if k != ".env.example"}
    plan = await _plan(fake_github(**{**fixture, "tree": tree, "bodies": bodies}))

    assert plan.required_environment_variables == []
    assert plan.unresolved_environment_variables == []


async def test_repository_with_no_default_branch_fails_clearly(
    fake_github: Any,
) -> None:
    fake = fake_github(**EMPTY_REPO, default_branch="")

    with pytest.raises(NotFoundError) as error:
        await _plan(fake)
    assert "default branch" in str(error.value).lower()


# ---------------------------------------------------------------------------
# Graph and request validation
# ---------------------------------------------------------------------------


def test_graph_runs_all_three_phases() -> None:
    nodes = set(planner.deployment_plan_graph.nodes)

    for node in (
        "analyze_repository",
        "plan_build",
        "plan_tests",
        "plan_deployment",
        "plan_health_check",
        "plan_rollback",
        "assess_risks",
        "assess_approvals",
        "order_steps",
        "assemble_plan",
    ):
        assert node in nodes


def test_request_rejects_bad_repository_names() -> None:
    from pydantic import ValidationError

    for bad in ("../etc", "a/b", "", "x" * 300):
        with pytest.raises(ValidationError):
            DeploymentPlanRequest(owner=bad, repository="demo")


def test_request_caps_issue_limit() -> None:
    request = DeploymentPlanRequest(owner="octocat", repository="demo", issue_limit=500)

    assert request.issue_limit == 30


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Any:
    from fastapi.testclient import TestClient

    from app.main import create_app

    return TestClient(create_app())


def test_endpoint_returns_plan_and_markdown(client: Any, fake_github: Any) -> None:
    """No route patching needed: the fake manager is installed module-wide."""
    fake = fake_github(**NODE_EXPRESS)

    response = client.post(
        "/api/deployment/plan", json={"owner": "octocat", "repository": fake.name}
    )

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"plan", "summary_markdown"}
    assert body["plan"]["repository"]["owner"] == "octocat"
    assert body["summary_markdown"].startswith("# Deployment plan:")
    assert body["plan"]["build_strategy"]["approach"] == "docker"
    assert body["plan"]["requires_human_approval"] is True


def test_endpoint_markdown_format(client: Any, fake_github: Any) -> None:
    fake = fake_github(**PYTHON_BARE)

    response = client.post(
        "/api/deployment/plan?format=markdown",
        json={"owner": "octocat", "repository": fake.name},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    body = response.text
    assert body.startswith("# Deployment plan:")
    assert "## Blocked" in body
    assert "DATABASE_URL" in body


def test_endpoint_rejects_a_bad_repository_name(client: Any) -> None:
    response = client.post("/api/deployment/plan", json={"owner": "../etc", "repository": "demo"})

    assert response.status_code == 422
