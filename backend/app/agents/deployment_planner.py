"""Unified deployment planning workflow.

Combines three concerns that are usually three separate agents and three
inconsistent answers:

* **Repository analysis** -- reuse :func:`app.agents.github_repository_analysis.
  analyze_github_repository` wholesale, then read the few files that decide how a
  deployment must work: the Dockerfile, a compose file, and the environment
  templates.
* **DevOps planning** -- derive a strategy per dimension from those facts.
* **Risk assessment** -- turn the same facts into risks and approval
  requirements, then order the steps.

All three read one state, so they cannot disagree: the plan that says "block on
``DATABASE_URL``" and the step list that blocks on ``DATABASE_URL`` are produced
from the same value.

The workflow reads only. It never writes to the repository, never creates a
Dockerfile, never runs a command. It returns a plan; a separate, approval-gated
executor would consume it. Keeping planning and execution apart is the point: a
planner that edits files cannot be reviewed before it changes anything.

Decision rules, all driven by the profile rather than by guesswork:

===========================  ==================================================
Condition                    Decision
===========================  ==================================================
Dockerfile present           Build and deploy via Docker.
No Dockerfile                Recommend generating one. Do not modify anything.
Tests present                Run the suite as a blocking gate.
No tests                     Testing is limited; flag it as a risk.
Required env var unresolved  Block the deployment until resolved.
===========================  ==================================================
"""

from __future__ import annotations

import logging
import re
from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.agents.github_repository_analysis import _call_tool, analyze_github_repository
from app.core.exceptions import UpstreamError
from app.models.deployment_plan import (
    ApprovalRequirement,
    ContainerAssets,
    DeploymentPlanRequest,
    DeploymentPlanState,
    DetectedStack,
    EnvironmentVariable,
    OrderedStep,
    RepositoryDeploymentPlan,
    RepositoryReference,
    RequiredService,
    Risk,
    Strategy,
)

logger = logging.getLogger(__name__)

#: Upper bound on container/config files fetched for a plan. Each one is a real
#: API call, and a plan does not need every compose variant in a monorepo.
MAX_DEPLOYMENT_FILES_READ = 6

#: Values that mean "a human must fill this in". Templates ship with these, and
#: treating them as satisfied is how ``changeme`` reaches production.
_PLACEHOLDER_TOKENS = (
    "changeme",
    "change_me",
    "change-me",
    "your-",
    "your_",
    "yourkey",
    "todo",
    "tbd",
    "xxx",
    "placeholder",
    "replace-me",
    "fixme",
    "<",
)

#: Environment filenames that *declare* variables without carrying live secrets.
#: ``.env`` itself is deliberately absent: it holds real values, and a plan has no
#: business reading it.
_ENV_TEMPLATE_NAMES = (".env.example", ".env.sample", ".env.template", "env.example")

_ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")

_HEALTHCHECK = re.compile(r"^\s*HEALTHCHECK\b", re.IGNORECASE | re.MULTILINE)
_EXPOSE = re.compile(r"^\s*EXPOSE\s+(.+)$", re.IGNORECASE | re.MULTILINE)
_FROM = re.compile(r"^\s*FROM\s+(\S+)", re.IGNORECASE | re.MULTILINE)
_ENTRYPOINT = re.compile(r"^\s*(?:ENTRYPOINT|CMD)\s+(.+)$", re.IGNORECASE | re.MULTILINE)
_USER = re.compile(r"^\s*USER\s+(\S+)", re.IGNORECASE | re.MULTILINE)

_COMPOSE_SERVICE = re.compile(r"^\s{2}([A-Za-z0-9_.-]+)\s*:", re.MULTILINE)

#: Env var name fragment -> (service, why). A hint, never a proof, so it only ever
#: adds a service whose origin the plan can state out loud.
_SERVICE_HINTS: tuple[tuple[str, str, str], ...] = (
    ("DATABASE_URL", "postgres", "A PostgreSQL connection string"),
    ("POSTGRES", "postgres", "A PostgreSQL connection string"),
    ("MONGO", "mongodb", "A MongoDB connection string"),
    ("REDIS", "redis", "A Redis connection string"),
    ("ELASTIC", "elasticsearch", "An Elasticsearch endpoint"),
    ("RABBIT", "rabbitmq", "A RabbitMQ broker"),
    ("KAFKA", "kafka", "A Kafka broker"),
    ("SENTRY_DSN", "sentry", "Error reporting"),
    ("STRIPE", "stripe", "Payments"),
)

#: Framework label -> the port its dev server conventionally listens on.
_FRAMEWORK_PORTS: tuple[tuple[str, int], ...] = (
    ("django", 8000),
    ("fastapi", 8000),
    ("flask", 5000),
    ("express", 3000),
    ("next.js", 3000),
    ("rails", 3000),
    ("laravel", 8000),
    ("spring", 8080),
)


def _stack_blob(profile: Any) -> str:
    """One lowercase blob for substring tests across every stack label."""
    parts: list[str] = [
        str(getattr(profile, "primary_language", "") or ""),
        str(getattr(profile, "backend_framework", "") or ""),
        str(getattr(profile, "frontend_framework", "") or ""),
        str(getattr(profile, "package_manager", "") or ""),
    ]
    parts.extend(str(language) for language in getattr(profile, "languages", []) or [])
    return " ".join(parts).lower()


def _is_placeholder(value: str) -> bool:
    """True when a declared value is a template rather than a setting."""
    stripped = value.strip().strip("\"'").strip()
    if not stripped:
        return True
    lowered = stripped.lower()
    return any(token in lowered for token in _PLACEHOLDER_TOKENS)


def _mask(value: str) -> str:
    """A hint that distinguishes ``changeme`` from ``postgres://…`` without echoing a secret."""
    stripped = value.strip().strip("\"'").strip()
    if not stripped:
        return "(empty)"
    if len(stripped) <= 4:
        return "(set)"
    return f"{stripped[:2]}… ({len(stripped)} chars, value withheld)"


async def _read_text(owner: str, repo: str, path: str, ref: str) -> str | None:
    """Read one repository file, or ``None`` if it cannot be read.

    An unreadable file is a gap in the plan, not a reason to fail the request.
    Callers record the gap as a note so a partial plan is never mistaken for a
    complete one.
    """
    try:
        payload = await _call_tool(
            "get_file_contents", {"owner": owner, "repo": repo, "path": path, "ref": ref}
        )
    except UpstreamError as error:
        logger.info("deployment asset unreadable", extra={"path": path, "error": str(error)})
        return None
    content = payload.get("content") if isinstance(payload, dict) else None
    return content if isinstance(content, str) else None


async def analyze_repository(state: DeploymentPlanState) -> dict[str, Any]:
    """Run the read-only GitHub analysis the rest of the plan is built on.

    Delegating keeps one definition of "what the repository is". A second,
    slightly different detector inside the planner would be a second set of
    opinions, and disagreements between the two are what make a plan untrustworthy.
    """
    from app.models.github import GitHubRepositoryAnalysisRequest

    request = GitHubRepositoryAnalysisRequest(
        owner=state["owner"],
        repository=state["repository"],
        branch=state.get("requested_ref"),
        include_issues=bool(state.get("include_issues")),
        issue_limit=int(state.get("issue_limit", 20)),
    )
    profile, readiness = await analyze_github_repository(request)
    logger.info(
        "deployment plan repository analysed",
        extra={"repository": f"{state['owner']}/{state['repository']}"},
    )
    return {
        "profile": profile,
        "readiness": readiness,
        "readiness_checks": list(getattr(readiness, "checks", []) or []),
        "ref": getattr(profile, "default_branch", None) or request.branch or "HEAD",
    }


def _to_detected_stack(profile: Any) -> DetectedStack:
    """Copy the stack facts the plan reasons about, so the plan stands alone."""
    return DetectedStack(
        primary_language=getattr(profile, "primary_language", None),
        languages=list(getattr(profile, "languages", []) or []),
        backend_framework=getattr(profile, "backend_framework", None),
        frontend_framework=getattr(profile, "frontend_framework", None),
        package_manager=getattr(profile, "package_manager", None),
        package_managers=list(getattr(profile, "package_managers", []) or []),
        test_framework=getattr(profile, "test_framework", None),
        test_file_count=int(getattr(profile, "test_file_count", 0) or 0),
        has_dockerfile=bool(getattr(profile, "has_dockerfile", False)),
        dockerfiles=list(getattr(profile, "dockerfiles", []) or []),
        has_docker_compose=bool(getattr(profile, "has_docker_compose", False)),
        ci_cd=list(getattr(profile, "ci_cd", []) or []),
        databases=list(getattr(profile, "databases", []) or []),
    )


async def inspect_deployment_assets(state: DeploymentPlanState) -> dict[str, Any]:
    """Read the Dockerfile, compose file and env templates that shape the plan.

    Contents are parsed rather than assumed: "a Dockerfile exists" and "the
    Dockerfile declares a health check" lead to different plans, and only reading
    it can tell them apart.
    """
    profile = state["profile"]
    ref = state.get("ref") or "HEAD"
    owner = state["owner"]
    repo = state["repository"]

    assets = ContainerAssets()
    notes: list[str] = []
    env_variables: list[EnvironmentVariable] = []
    budget = MAX_DEPLOYMENT_FILES_READ

    dockerfile = next(iter(getattr(profile, "dockerfiles", []) or []), None)
    if dockerfile:
        text = None
        if budget > 0:
            text = await _read_text(owner, repo, dockerfile, ref)
            budget -= 1
        if text is None:
            notes.append(f"Could not read {dockerfile}; container facts are assumed, not verified.")
        else:
            assets = _parse_dockerfile(text)

    compose = next(iter(getattr(profile, "docker_compose_files", []) or []), None)
    if compose:
        text = None
        if budget > 0:
            text = await _read_text(owner, repo, compose, ref)
            budget -= 1
        if text is None:
            notes.append(f"Could not read {compose}; compose services are unknown.")
        else:
            assets.compose_read = True
            assets.compose_services = _parse_compose(text)

    for name in _ENV_TEMPLATE_NAMES:
        path = _find_env_template(getattr(profile, "env_files", []) or [], name)
        if path is None or budget <= 0:
            continue
        text = await _read_text(owner, repo, path, ref)
        budget -= 1
        if text is None:
            notes.append(f"Could not read {path}; its variables are missing from this plan.")
            continue
        env_variables.extend(_parse_env_template(text, path))

    services = _infer_services(
        env_variables, getattr(profile, "databases", []) or [], assets.compose_services
    )

    return {
        "container_assets": assets,
        "environment_variables": _merge_env_variables(env_variables),
        "services": services,
        "notes": notes,
    }


def _parse_dockerfile(text: str) -> ContainerAssets:
    """Extract the facts a deployment plan needs from a Dockerfile."""
    assets = ContainerAssets(dockerfile_read=True)

    base = _FROM.search(text)
    if base:
        assets.base_image = base.group(1)

    for match in _EXPOSE.finditer(text):
        for token in match.group(1).split():
            port = token.split("/")[0]
            if port.isdigit():
                assets.exposed_ports.append(int(port))

    health = _HEALTHCHECK.search(text)
    if health:
        assets.has_healthcheck = True
        remainder = text[health.start() :].splitlines()
        if len(remainder) > 1:
            assets.healthcheck_command = remainder[1].strip()

    entry = _ENTRYPOINT.search(text)
    if entry:
        assets.has_entrypoint = True
        assets.entrypoint = entry.group(1).strip()[:200]

    user = _USER.search(text)
    if user and user.group(1).strip().lower() not in {"root", "0"}:
        assets.runs_as_non_root = True

    if not assets.exposed_ports:
        assets.notes.append("Dockerfile declares no EXPOSE; the exposed port is a guess.")
    return assets


def _parse_compose(text: str) -> list[str]:
    """List service names from a compose file, ignoring comments and prose."""
    services: list[str] = []
    for match in _COMPOSE_SERVICE.finditer(text):
        name = match.group(1)
        if name in {"services", "volumes", "networks", "configs", "secrets"}:
            continue
        if name not in services:
            services.append(name)
    return services


def _find_env_template(env_files: list[str], name: str) -> str | None:
    """Locate an env template by filename, matching on the basename."""
    for candidate in env_files:
        if candidate.rsplit("/", 1)[-1].lower() == name:
            return candidate
    return None


def _parse_env_template(text: str, source: str) -> list[EnvironmentVariable]:
    """Parse ``KEY=value`` declarations, flagging placeholders as unresolved.

    ``required`` defaults to True because a variable declared in a template is
    almost always needed to start the app. The planner does not invent variables
    the repository never declared: guessing produces confident, wrong plans.
    """
    variables: list[EnvironmentVariable] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _ENV_LINE.match(line)
        if match is None:
            continue
        name, raw = match.group(1), match.group(2)
        resolved = not _is_placeholder(raw)
        reason: str | None = None
        if not resolved:
            reason = "empty" if not raw.strip().strip("\"'").strip() else "placeholder"
        variables.append(
            EnvironmentVariable(
                name=name,
                source=source,
                required=True,
                resolved=resolved,
                unresolved_reason=reason,
                value_hint=None if resolved else _mask(raw),
                notes="" if resolved else "Template value. Must be supplied before deploying.",
            )
        )
    return variables


def _merge_env_variables(variables: list[EnvironmentVariable]) -> list[EnvironmentVariable]:
    """Collapse duplicates across templates, keeping the most satisfied declaration.

    A variable declared in two files is one variable, not two. Treating the
    second declaration as unresolved when the first is resolved would invent a
    blocker that does not exist.
    """
    merged: dict[str, EnvironmentVariable] = {}
    for variable in variables:
        existing = merged.get(variable.name)
        if existing is None:
            merged[variable.name] = variable
            continue
        winner = variable if variable.resolved and not existing.resolved else existing
        if winner is existing and variable.resolved:
            winner = variable
        merged[variable.name] = winner
    return sorted(merged.values(), key=lambda item: (not item.required, item.name.lower()))


def _infer_services(
    variables: list[EnvironmentVariable], databases: list[str], compose_services: list[str]
) -> list[RequiredService]:
    """Derive required backing services, recording the evidence for each.

    A service appears only with a reason attached: a compose service named in the
    file, a database the analyser matched, or an env var that names one. Anything
    less specific would be a guess presented as a requirement.
    """
    services: dict[str, RequiredService] = {}

    for name in compose_services:
        services.setdefault(
            name,
            RequiredService(
                name=name,
                purpose="Declared in the compose file.",
                detected_from=["docker-compose"],
            ),
        )

    for database in databases:
        key = database.lower()
        services.setdefault(
            key,
            RequiredService(
                name=key,
                purpose="Referenced by the application code.",
                detected_from=["repository profile"],
            ),
        )

    for variable in variables:
        upper = variable.name.upper()
        for fragment, service, purpose in _SERVICE_HINTS:
            if fragment in upper and service not in services:
                services[service] = RequiredService(
                    name=service,
                    purpose=purpose,
                    detected_from=[f"{variable.name} ({variable.source})"],
                )
                break

    return sorted(services.values(), key=lambda item: item.name)


# ---------------------------------------------------------------------------
# DevOps planning
# ---------------------------------------------------------------------------


def plan_build(state: DeploymentPlanState) -> dict[str, Any]:
    """Choose a build strategy.

    A Dockerfile means Docker. Its absence means recommending one -- explicitly
    *not* writing one. Generating it here would mutate a repository the operator
    only asked to be analysed.
    """
    stack = _to_detected_stack(state["profile"])
    assets = state["container_assets"]

    if stack.has_dockerfile:
        context: list[str] = [f"{len(stack.dockerfiles)} Dockerfile(s) found."]
        if assets.dockerfile_read:
            if assets.base_image:
                context.append(f"Base image {assets.base_image}.")
            if assets.exposed_ports:
                ports = ", ".join(str(port) for port in assets.exposed_ports)
                context.append(f"EXPOSE declares {ports}.")
            else:
                context.append("No EXPOSE; the port must be supplied at run time.")
            if not assets.runs_as_non_root:
                context.append("No non-root USER; the container runs as root.")
        return {
            "build_strategy": Strategy(
                approach="docker",
                summary="Build a container image from the repository's Dockerfile.",
                rationale=context,
                tools=["docker.build_image"],
                commands=["docker build -t <image> ."],
                confidence=0.9 if assets.dockerfile_read else 0.7,
                limitations=[
                    "Dockerfile RUN steps execute as repository code during the build.",
                    "No registry is configured; the image stays on the local daemon.",
                ],
                recommended_changes=(
                    []
                    if assets.runs_as_non_root
                    else ["Add a non-root USER to the Dockerfile before production use."]
                ),
            )
        }

    alternatives: list[str] = []
    if stack.has_docker_compose:
        alternatives.append("A compose file exists, so a build section may already be defined.")
    if stack.package_manager:
        alternatives.append(f"{stack.package_manager} can install and run the app without Docker.")

    recommended = [
        "Add a Dockerfile. Aegis will not create or modify one automatically: this plan "
        "reports what is missing, and any repository change stays a reviewed, separate action."
    ]
    if stack.backend_framework:
        recommended.append(
            f"Base the image on an official "
            f"{stack.primary_language or stack.backend_framework} runtime."
        )

    return {
        "build_strategy": Strategy(
            approach="generate-dockerfile",
            summary="No Dockerfile found. A container build cannot start until one is added.",
            rationale=[
                "No Dockerfile was found in the repository.",
                *alternatives,
            ],
            tools=[],
            commands=[],
            confidence=0.85,
            limitations=[
                "No container image can be built from this repository as it stands.",
                "The build step is blocked, and every step that depends on it is blocked too.",
            ],
            recommended_changes=recommended,
        ),
    }


def plan_tests(state: DeploymentPlanState) -> dict[str, Any]:
    """Include test execution when a suite exists; mark testing limited when it does not.

    An absent suite is reported as limited, never as a pass. The plan does not
    invent coverage, and a deployment whose tests were silently skipped should
    not look identical to one whose tests ran.
    """
    stack = _to_detected_stack(state["profile"])
    ci = ", ".join(stack.ci_cd) if stack.ci_cd else "none detected"

    if stack.test_file_count > 0 and stack.test_framework:
        return {
            "test_strategy": Strategy(
                approach="run-suite",
                summary=f"Run the {stack.test_framework} suite as a blocking gate.",
                rationale=[
                    f"{stack.test_file_count} test file(s) detected.",
                    f"Test framework: {stack.test_framework}.",
                    f"CI/CD: {ci}.",
                ],
                tools=["docker.build_image", "container_health"],
                commands=[f"{stack.package_manager or 'the language'} test"],
                confidence=0.9,
                limitations=[
                    "The suite runs inside the built image, so a failing "
                    "dependency blocks the build.",
                    "Existing CI results are not fetched; this step runs the suite fresh.",
                ],
                recommended_changes=[],
            )
        }

    if stack.test_file_count > 0:
        return {
            "test_strategy": Strategy(
                approach="run-suite-unknown-runner",
                summary="Test files exist but no runner was identified.",
                rationale=[
                    f"{stack.test_file_count} test file(s) detected.",
                    "No known test framework or runner was matched.",
                ],
                tools=[],
                commands=[],
                confidence=0.4,
                limitations=[
                    "The plan cannot name a test command without guessing.",
                    "Testing is limited: the command must be supplied manually.",
                ],
                recommended_changes=["Declare the test command so the deployment can run it."],
            )
        }

    return {
        "test_strategy": Strategy(
            approach="limited-no-tests",
            summary="No test suite detected. Testing is limited to a smoke check.",
            rationale=[
                "No test files were found in the repository.",
                f"CI/CD: {ci}.",
            ],
            tools=["container_health"],
            commands=[],
            confidence=0.85,
            limitations=[
                "No automated regression gate exists for this deployment.",
                "A health check verifies the service starts, not that it is correct.",
            ],
            recommended_changes=[
                "Add tests before treating deployments as safe.",
                "A container health check is the only automated signal available.",
            ],
        )
    }


def plan_deployment(state: DeploymentPlanState) -> dict[str, Any]:
    """Choose where and how the app runs, respecting a blocked build."""
    stack = _to_detected_stack(state["profile"])
    assets = state["container_assets"]
    build = state["build_strategy"]
    unresolved = [v for v in state["environment_variables"] if v.required and not v.resolved]

    limitations = ["Local Docker daemon only. No AWS deployment and no registry push."]
    if stack.has_docker_compose:
        limitations.append(
            "A compose file exists and defines additional services; the plan does not "
            "start them. Use compose if the app needs them."
        )
    if unresolved:
        limitations.append(
            f"{len(unresolved)} required environment variable(s) unresolved; deployment is blocked."
        )

    if build.approach == "docker":
        ports = assets.exposed_ports or [_framework_port(stack)]
        port_text = ", ".join(f"{port}:{port}" for port in ports if port)
        summary = "Run the built image as a local container."
        commands = [f"docker run -d -p {port_text or '<port>:<port>'} <image>"] if port_text else []
        confidence = 0.9 if assets.dockerfile_read else 0.7
    else:
        summary = "Container deployment is not possible yet: no Dockerfile exists."
        commands = []
        confidence = 0.9
        limitations.insert(
            0, "Every deployment step is blocked until a Dockerfile is added to the repository."
        )

    return {
        "deployment_strategy": Strategy(
            approach="docker-container" if build.approach == "docker" else "blocked-no-dockerfile",
            summary=summary,
            rationale=[
                f"Build strategy is '{build.approach}'.",
                f"Package manager: {stack.package_manager or 'unknown'}.",
                "Exposed ports: "
                f"{', '.join(str(p) for p in assets.exposed_ports) or 'undeclared'}.",
            ],
            tools=["docker.start_container", "docker.stop_container"],
            commands=commands,
            confidence=confidence,
            limitations=limitations,
            recommended_changes=[],
        )
    }


def _framework_port(stack: DetectedStack) -> int | None:
    """The conventional port for the detected framework, if one is known."""
    blob = " ".join(
        filter(None, [stack.primary_language, stack.backend_framework, stack.frontend_framework])
    ).lower()
    for label, port in _FRAMEWORK_PORTS:
        if label in blob:
            return port
    return None


def plan_health_check(state: DeploymentPlanState) -> dict[str, Any]:
    """Prefer a declared health check; otherwise propose one and say it is a proposal."""
    assets = state["container_assets"]
    stack = _to_detected_stack(state["profile"])

    if assets.has_healthcheck:
        return {
            "health_check_strategy": Strategy(
                approach="docker-healthcheck",
                summary="Use the image's own HEALTHCHECK via the Docker health subcommand.",
                rationale=[
                    "The Dockerfile declares a HEALTHCHECK instruction.",
                    "The image knows best how to be probed.",
                ],
                tools=["docker.container_health", "docker.container_logs"],
                commands=["docker inspect --format '{{json .State.Health}}' <container>"],
                confidence=0.95,
                limitations=[
                    "Docker reports the last probe result; a healthy container "
                    "can still serve errors.",
                ],
                recommended_changes=[],
            )
        }

    port = assets.exposed_ports[0] if assets.exposed_ports else _framework_port(stack)
    probe = f"GET /health on port {port}" if port else "GET /health on the discovered port"
    return {
        "health_check_strategy": Strategy(
            approach="http-probe-required",
            summary=(
                "No HEALTHCHECK declared. One must be added before the container can be judged."
            ),
            rationale=[
                "The Dockerfile declares no HEALTHCHECK instruction.",
                "An image with no health check can never be reported healthy by this plan.",
            ],
            tools=["docker.container_logs"],
            commands=[f"curl -fsS http://localhost:{port or 8000}/health"] if port else [],
            confidence=0.5,
            limitations=[
                "Without a HEALTHCHECK, container_health returns 'no_healthcheck', not 'healthy'.",
                f"Recommended probe: {probe}.",
            ],
            recommended_changes=[
                "Add a HEALTHCHECK to the Dockerfile, backed by a real /health endpoint.",
                "The endpoint should verify dependencies, not just that the process is alive.",
            ],
        )
    }


def plan_rollback(state: DeploymentPlanState) -> dict[str, Any]:
    """Describe how to undo the deployment, and be honest when that is not possible."""
    build = state["build_strategy"]
    has_persistence = any(service.required for service in state.get("services", []))

    if build.approach != "docker":
        return {
            "rollback_strategy": Strategy(
                approach="none-available",
                summary="No rollback path exists, because nothing can be deployed yet.",
                rationale=["No Dockerfile, therefore no image and no container to roll back to."],
                limitations=[
                    "There is nothing to roll back until a deployment has succeeded once.",
                    "Tag every successful image so a later rollback has somewhere to go.",
                ],
                recommended_changes=[
                    "Use immutable, versioned image tags from the first deployment."
                ],
            )
        }

    steps = ["docker stop <container>", "docker run -d <previous-image-tag>"]
    limitations = [
        "The image is local; rollback is limited to images still on this daemon.",
        "Rollback does not revert data migrations or any schema change the new version made.",
    ]
    if has_persistence:
        limitations.append(
            "The application depends on a backing service, so rollback must also consider "
            "data written by the failed version."
        )

    return {
        "rollback_strategy": Strategy(
            approach="image-tag-rollback",
            summary="Roll back by restarting the container on the previous image tag.",
            rationale=[
                "A container image is immutable once built, so the previous version survives.",
                "No orchestrator is involved, so rollback is a stop and a start.",
            ],
            tools=["docker.stop_container", "docker.start_container"],
            commands=steps,
            confidence=0.85,
            limitations=limitations,
            recommended_changes=[
                "Tag each deployed image with its version so rollback has a target.",
                "Define and rehearse a database rollback path before the first release.",
            ],
        )
    }


# ---------------------------------------------------------------------------
# Risk assessment
# ---------------------------------------------------------------------------


def assess_risks(state: DeploymentPlanState) -> dict[str, Any]:
    """Derive risks from profile facts. Each risk states its own mitigation."""
    stack = _to_detected_stack(state["profile"])
    assets = state["container_assets"]
    risks: list[Risk] = []

    if not stack.has_dockerfile:
        risks.append(
            Risk(
                id="no-dockerfile",
                severity="high",
                title="No Dockerfile in the repository",
                detail="There is no reproducible, self-contained build for this application.",
                mitigation="Add a Dockerfile, then re-run this plan.",
                blocking=True,
            )
        )

    if stack.test_file_count == 0:
        risks.append(
            Risk(
                id="no-tests",
                severity="high",
                title="No automated tests detected",
                detail="Deploying without tests means no regression gate.",
                mitigation="Add tests. Until then, rely on a health check and a manual smoke test.",
                blocking=False,
            )
        )

    for variable in state["environment_variables"]:
        if not variable.required or variable.resolved:
            continue
        risks.append(
            Risk(
                id=f"env-unresolved:{variable.name.lower()}",
                severity="high",
                title=f"Required environment variable {variable.name} is unresolved",
                detail=(
                    f"{variable.source} declares {variable.name} with a template value. "
                    "Deploying now would ship the placeholder."
                ),
                mitigation=f"Supply a real value for {variable.name} and re-run this plan.",
                blocking=True,
            )
        )

    if stack.has_dockerfile and not assets.has_healthcheck:
        risks.append(
            Risk(
                id="no-healthcheck",
                severity="medium",
                title="No HEALTHCHECK in the Dockerfile",
                detail="The container cannot be automatically judged healthy or unhealthy.",
                mitigation="Add a HEALTHCHECK backed by a real /health endpoint.",
                blocking=False,
            )
        )

    if stack.has_dockerfile and assets.dockerfile_read and not assets.runs_as_non_root:
        risks.append(
            Risk(
                id="runs-as-root",
                severity="medium",
                title="Container runs as root",
                detail="No non-root USER instruction, so a compromise inside is host-root inside.",
                mitigation="Add a non-root USER and drop unnecessary capabilities.",
                blocking=False,
            )
        )

    if not stack.ci_cd:
        risks.append(
            Risk(
                id="no-ci",
                severity="medium",
                title="No CI/CD detected",
                detail="Nothing enforces tests or build success before a deployment.",
                mitigation="Add a workflow that runs the suite and the build.",
                blocking=False,
            )
        )

    if stack.databases:
        risks.append(
            Risk(
                id="persistent-data",
                severity="medium",
                title="Deployment touches persistent data",
                detail=(
                    f"References to {', '.join(stack.databases)} mean a release may need a "
                    "migration, and a container rollback will not undo it."
                ),
                mitigation=(
                    "Test migrations separately and decide the rollback path before release."
                ),
                blocking=False,
            )
        )

    for service in state.get("services", []):
        if not service.required:
            continue
        risks.append(
            Risk(
                id=f"service:{service.name}",
                severity="low",
                title=f"Depends on the {service.name} service",
                detail=f"{service.purpose} Evidence: {', '.join(service.detected_from)}.",
                mitigation=f"Ensure {service.name} is reachable before starting the app.",
                blocking=False,
            )
        )

    if stack.languages and len(stack.languages) > 3:
        risks.append(
            Risk(
                id="polyglot",
                severity="low",
                title="Multiple languages in one repository",
                detail=(
                    f"{len(stack.languages)} languages detected "
                    f"({', '.join(stack.languages[:5])}); the runtime may not be obvious."
                ),
                mitigation="Confirm which component is actually deployed.",
                blocking=False,
            )
        )

    if getattr(state.get("profile"), "truncated", False):
        risks.append(
            Risk(
                id="truncated-analysis",
                severity="medium",
                title="Repository analysis hit a scan limit",
                detail="Some directories were not inspected, so the plan may be incomplete.",
                mitigation="Re-run with a higher file limit to get a complete profile.",
                blocking=False,
            )
        )

    return {"risks": risks}


def assess_approvals(state: DeploymentPlanState) -> dict[str, Any]:
    """Derive approval requirements from the risks and strategies.

    Generated from the plan rather than hard-coded, so a low-risk plan does not
    ask for sign-off it does not need, and a high-risk one cannot quietly skip it.
    """
    build = state["build_strategy"]
    approval_requirements: list[ApprovalRequirement] = []

    if build.approach == "docker":
        approval_requirements.append(
            ApprovalRequirement(
                action="Build the container image",
                reason="Dockerfile RUN steps execute repository code on this machine.",
                risk_level="medium",
                tool="docker.build_image",
            )
        )
        approval_requirements.append(
            ApprovalRequirement(
                action="Start the container",
                reason="Changes the state of the local Docker daemon.",
                risk_level="medium",
                tool="docker.start_container",
            )
        )

    approval_requirements.append(
        ApprovalRequirement(
            action="Stop or roll back the container",
            reason="Destructive: stops a running workload and replaces it with a previous image.",
            risk_level="high",
            tool="docker.stop_container",
        )
    )

    approval_requirements.append(
        ApprovalRequirement(
            action="Deploy to a managed cloud target",
            reason="Would create or change billable infrastructure. Not configured yet; "
            "the plan stops at the local daemon.",
            risk_level="critical",
            tool=None,
        )
    )

    for risk in state.get("risks", []):
        if risk.blocking:
            approval_requirements.append(
                ApprovalRequirement(
                    action=f"Resolve blocking risk: {risk.title}",
                    reason=risk.detail,
                    risk_level=risk.severity,
                    tool=None,
                )
            )

    return {
        "approval_requirements": approval_requirements,
        "requires_human_approval": bool(approval_requirements),
    }


def order_steps(state: DeploymentPlanState) -> dict[str, Any]:
    """Order the plan's actions, marking each blocked step with its reason.

    Blocking is contagious: if the build is blocked because there is no Dockerfile,
    the tests that need the image and the run that needs the image are blocked too.
    Marking only the first step would invite someone to skip ahead.
    """
    stack = _to_detected_stack(state["profile"])
    assets = state["container_assets"]
    build = state["build_strategy"]
    tests = state["test_strategy"]
    health = state["health_check_strategy"]
    services = state.get("services", [])
    unresolved = [v for v in state["environment_variables"] if v.required and not v.resolved]

    steps: list[OrderedStep] = []
    order = 0

    def add(
        title: str,
        description: str,
        *,
        tool: str | None = None,
        command: str | None = None,
        approval: bool = False,
        automated: bool = True,
        blocked: bool = False,
        blocked_reason: str | None = None,
        depends_on: list[int] | None = None,
    ) -> OrderedStep:
        nonlocal order
        order += 1
        step = OrderedStep(
            order=order,
            title=title,
            description=description,
            tool=tool,
            command=command,
            requires_approval=approval,
            automated=automated,
            blocked=blocked,
            blocked_reason=blocked_reason,
            depends_on=depends_on or [],
        )
        steps.append(step)
        return step

    add(
        "Confirm prerequisites",
        "Confirm the local Docker daemon is available and the checked-out ref is the one intended.",
        tool="docker.docker_available",
        command="docker version",
    )

    if services:
        add(
            "Start backing services",
            "Make "
            + ", ".join(service.name for service in services)
            + " available before the app starts.",
            command=(
                "docker compose up -d"
                if stack.has_docker_compose
                else f"# provision {', '.join(service.name for service in services)}"
            ),
            approval=False,
            depends_on=[1],
        )

    if unresolved:
        names = ", ".join(variable.name for variable in unresolved)
        add(
            "Resolve required environment variables",
            f"Supply real values for {names}. These are declared in the repository templates "
            "with placeholder values.",
            blocked=True,
            blocked_reason=f"{len(unresolved)} required variable(s) unresolved: {names}",
            automated=False,
        )

    build_blocked = build.approach != "docker"
    build_step = add(
        "Prepare the container build",
        (
            "Add a Dockerfile to the repository. This plan does not create or modify files; "
            "the change is a separate, reviewed action."
            if build_blocked
            else "Use the repository's Dockerfile as the build definition."
        ),
        automated=not build_blocked,
        blocked=build_blocked,
        blocked_reason="No Dockerfile in the repository." if build_blocked else None,
    )

    test_step = add(
        "Run the test suite",
        tests.summary,
        tool=tests.tools[0] if tests.tools else None,
        command=tests.commands[0] if tests.commands else None,
        blocked=build_blocked and tests.approach == "run-suite",
        blocked_reason=(
            "Tests run against the built image, and there is no image to build."
            if build_blocked and tests.approach == "run-suite"
            else None
        ),
        depends_on=[build_step.order],
    )

    build_order = add(
        "Build the container image",
        "Build the image locally. Dockerfile RUN steps execute repository code.",
        tool="docker.build_image",
        command="docker build -t <image> .",
        approval=True,
        blocked=build_blocked,
        blocked_reason="No Dockerfile in the repository." if build_blocked else None,
        depends_on=[test_step.order],
    )

    health_blocked = build_blocked or (stack.has_dockerfile and not assets.has_healthcheck)
    add(
        "Define a health check",
        health.summary,
        automated=False,
        blocked=health_blocked,
        blocked_reason=(
            "No Dockerfile to add a HEALTHCHECK to."
            if build_blocked
            else "No HEALTHCHECK declared; the image cannot be judged healthy."
            if health_blocked
            else None
        ),
        depends_on=[build_order.order],
    )

    run_order = add(
        "Start the container",
        "Run the built image on the local daemon.",
        tool="docker.start_container",
        command=(
            state["deployment_strategy"].commands[0]
            if state["deployment_strategy"].commands
            else None
        ),
        approval=True,
        blocked=build_blocked or bool(unresolved),
        blocked_reason=_run_block_reason(build_blocked, unresolved),
        depends_on=[build_order.order],
    )

    add(
        "Verify health",
        health.summary,
        tool="docker.container_health",
        command="docker inspect --format '{{json .State.Health}}' <container>",
        blocked=build_blocked or not assets.has_healthcheck,
        blocked_reason=(
            None
            if assets.has_healthcheck and not build_blocked
            else "Depends on a started container that declares a HEALTHCHECK."
        ),
        depends_on=[run_order.order],
    )

    add(
        "Collect startup logs",
        "Capture logs to confirm the service started as expected.",
        tool="docker.container_logs",
        blocked=build_blocked,
        blocked_reason="No container to read logs from." if build_blocked else None,
        depends_on=[run_order.order],
    )

    add(
        "Prepare the rollback path",
        state["rollback_strategy"].summary,
        tool="docker.stop_container",
        approval=True,
        blocked=False,
        depends_on=[run_order.order],
    )

    blocked_by: list[str] = []
    if build_blocked:
        blocked_by.append(
            "No Dockerfile in the repository. One must be added before anything "
            "can be built or run."
        )
    if unresolved:
        names = ", ".join(variable.name for variable in unresolved)
        blocked_by.append(
            f"{len(unresolved)} required environment variable(s) unresolved: {names}. "
            "The deployment is blocked until they are supplied."
        )

    # `requires_human_approval` is deliberately not set here. It was computed by
    # `assess_approvals`, and returning a fresh value would overwrite it: a plan
    # that lists four approvals must not report that it needs none.
    return {
        "steps": steps,
        "blocked": bool(blocked_by),
        "blocked_by": blocked_by,
    }


def _run_block_reason(build_blocked: bool, unresolved: list[EnvironmentVariable]) -> str | None:
    """Why the run step is blocked, or ``None`` when it is not."""
    reasons: list[str] = []
    if build_blocked:
        reasons.append("no Dockerfile to build an image from")
    if unresolved:
        names = ", ".join(variable.name for variable in unresolved)
        reasons.append(f"unresolved environment variables: {names}")
    return "Cannot start the container: " + "; ".join(reasons) + "." if reasons else None


def assemble_plan(state: DeploymentPlanState) -> dict[str, Any]:
    """Fold every stage into the final plan object.

    This is the only place the two representations are produced, so the JSON and
    the Markdown cannot drift apart.
    """
    profile = state["profile"]
    ref = state.get("ref") or "HEAD"
    owner = state["owner"]
    repository = state["repository"]

    name = getattr(profile, "name", None) or repository
    full_name = f"{owner}/{getattr(profile, 'name', None) or repository}"

    reference = RepositoryReference(
        owner=owner,
        name=str(name),
        full_name=full_name,
        ref=ref,
        default_branch=getattr(profile, "default_branch", None),
    )

    notes = list(state.get("notes", []))
    for note in getattr(profile, "notes", []) or []:
        notes.append(str(note))

    plan = RepositoryDeploymentPlan(
        repository=reference,
        detected_stack=_to_detected_stack(profile),
        container_assets=state["container_assets"],
        build_strategy=state["build_strategy"],
        test_strategy=state["test_strategy"],
        deployment_strategy=state["deployment_strategy"],
        health_check_strategy=state["health_check_strategy"],
        rollback_strategy=state["rollback_strategy"],
        required_environment_variables=list(state.get("environment_variables", [])),
        required_services=list(state.get("services", [])),
        risks=list(state.get("risks", [])),
        approval_requirements=list(state.get("approval_requirements", [])),
        ordered_steps=list(state.get("steps", [])),
        readiness=state.get("readiness"),
        readiness_checks=list(state.get("readiness_checks", [])),
        blocked=bool(state.get("blocked")),
        blocked_by=list(state.get("blocked_by", [])),
        requires_human_approval=bool(state.get("requires_human_approval")),
        notes=notes,
    )

    logger.info(
        "deployment plan assembled",
        extra={
            "repository": full_name,
            "blocked": plan.blocked,
            "risk_count": len(plan.risks),
            "step_count": len(plan.ordered_steps),
        },
    )
    return {"plan": plan}


def build_deployment_plan_graph() -> CompiledStateGraph:
    """Assemble the unified planning graph.

    Three phases in one graph: analyse the repository, plan against what was
    found, then assess risk and order the work. Planning reads the analysis and
    risk reads the plans, so a strategy cannot be recorded without the risk it
    implies.
    """
    builder = StateGraph(DeploymentPlanState)
    builder.add_node("analyze_repository", analyze_repository)
    builder.add_node("inspect_deployment_assets", inspect_deployment_assets)
    builder.add_node("plan_build", plan_build)
    builder.add_node("plan_tests", plan_tests)
    builder.add_node("plan_deployment", plan_deployment)
    builder.add_node("plan_health_check", plan_health_check)
    builder.add_node("plan_rollback", plan_rollback)
    builder.add_node("assess_risks", assess_risks)
    builder.add_node("assess_approvals", assess_approvals)
    builder.add_node("order_steps", order_steps)
    builder.add_node("assemble_plan", assemble_plan)

    builder.add_edge(START, "analyze_repository")
    builder.add_edge("analyze_repository", "inspect_deployment_assets")
    builder.add_edge("inspect_deployment_assets", "plan_build")
    builder.add_edge("plan_build", "plan_tests")
    builder.add_edge("plan_tests", "plan_deployment")
    builder.add_edge("plan_deployment", "plan_health_check")
    builder.add_edge("plan_health_check", "plan_rollback")
    builder.add_edge("plan_rollback", "assess_risks")
    builder.add_edge("assess_risks", "assess_approvals")
    builder.add_edge("assess_approvals", "order_steps")
    builder.add_edge("order_steps", "assemble_plan")
    builder.add_edge("assemble_plan", END)

    return builder.compile()


deployment_plan_graph = build_deployment_plan_graph()


async def plan_repository_deployment(
    request: DeploymentPlanRequest,
) -> RepositoryDeploymentPlan:
    """Run the whole workflow and return the unified plan.

    Read-only end to end: it analyses a GitHub repository and returns a plan. It
    creates no file, pushes nothing and starts nothing.
    """
    state: DeploymentPlanState = {
        "owner": request.owner,
        "repository": request.repository,
        "requested_ref": request.branch,
        "include_issues": request.include_issues,
        "issue_limit": request.issue_limit,
    }
    result = await deployment_plan_graph.ainvoke(state)
    return result["plan"]


__all__ = [
    "MAX_DEPLOYMENT_FILES_READ",
    "assemble_plan",
    "assess_approvals",
    "assess_risks",
    "analyze_repository",
    "build_deployment_plan_graph",
    "deployment_plan_graph",
    "inspect_deployment_assets",
    "order_steps",
    "plan_health_check",
    "plan_build",
    "plan_deployment",
    "plan_repository_deployment",
    "plan_rollback",
    "plan_tests",
]
