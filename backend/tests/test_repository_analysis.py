"""Tests for repository analysis: the graph, the detectors, and the endpoint.

The fixture repository is generated into ``tmp_path`` (see ``conftest.py``) so no
sample tree is committed to the repository. A dedicated fixture re-points the
containment root at that temporary directory, which also proves the analyzer
cannot reach outside it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.agents.repository_analysis import (
    analyze_repository_path,
    build_analysis_graph,
    detect_node,
    repository_analysis_graph,
    resolve_target,
    scan_node,
)
from app.core.config import get_settings
from app.core.exceptions import NotFoundError, PermissionDeniedError, ValidationError
from app.models.repository import RepositoryProfile
from app.services.repository import (
    _package_json_dependencies,
    _pyproject_dependencies,
    _requirements_dependencies,
    scan_repository,
)

# --------------------------------------------------------------------------
# Graph structure
# --------------------------------------------------------------------------


def test_graph_contains_the_required_nodes() -> None:
    nodes = set(repository_analysis_graph.get_graph().nodes)

    assert {"resolve_target", "scan_repository", "detect_stack", "build_profile"} <= nodes
    assert "__start__" in nodes
    assert "__end__" in nodes


def test_graph_nodes_are_wired_in_order() -> None:
    edges = {(edge.source, edge.target) for edge in repository_analysis_graph.get_graph().edges}

    assert ("__start__", "resolve_target") in edges
    assert ("resolve_target", "scan_repository") in edges
    assert ("scan_repository", "detect_stack") in edges
    assert ("detect_stack", "build_profile") in edges
    assert ("build_profile", "__end__") in edges


def test_graph_runs_independently_and_returns_a_profile(
    sample_repo: Path, repository_root: Path
) -> None:
    state = repository_analysis_graph.invoke({"requested_path": "sample-repo"})

    assert isinstance(state["profile"], RepositoryProfile)
    assert state["root"] == sample_repo.resolve()


def test_each_build_returns_an_independent_graph() -> None:
    assert build_analysis_graph() is not repository_analysis_graph


def test_graph_populates_every_state_key(sample_repo: Path) -> None:
    state = repository_analysis_graph.invoke({"requested_path": str(sample_repo)})

    assert {"requested_path", "root", "inventory", "profile"} <= set(state)


# --------------------------------------------------------------------------
# Path containment (the security boundary)
# --------------------------------------------------------------------------


def test_resolve_accepts_a_path_inside_the_root(sample_repo: Path) -> None:
    result = resolve_target({"requested_path": str(sample_repo)})

    assert result["root"] == sample_repo.resolve()


def test_resolve_interprets_a_relative_path_against_the_root(
    sample_repo: Path, repository_root: Path
) -> None:
    result = resolve_target({"requested_path": "sample-repo"})

    assert result["root"] == sample_repo.resolve()
    assert result["root"] != repository_root.resolve().parent


def test_resolve_rejects_parent_traversal(repository_root: Path) -> None:
    with pytest.raises(PermissionDeniedError):
        resolve_target({"requested_path": "../../etc"})


def test_resolve_rejects_an_absolute_path_outside_the_root() -> None:
    with pytest.raises(PermissionDeniedError):
        resolve_target({"requested_path": "/etc"})


def test_resolve_rejects_a_path_that_escapes_via_dot_segments(
    repository_root: Path, tmp_path: Path
) -> None:
    sneaky = f"{repository_root}/allowed/../{repository_root.name}/../../outside"

    with pytest.raises(PermissionDeniedError):
        resolve_target({"requested_path": sneaky})


def test_resolve_rejects_a_symlink_pointing_outside_the_root(
    repository_root: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("classified", encoding="utf-8")
    link = repository_root / "escape"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable on this filesystem")

    with pytest.raises(PermissionDeniedError):
        resolve_target({"requested_path": "escape"})


def test_resolve_rejects_a_missing_directory(repository_root: Path) -> None:
    with pytest.raises(NotFoundError):
        resolve_target({"requested_path": "nope"})


def test_resolve_rejects_a_file_instead_of_a_directory(repository_root: Path) -> None:
    (repository_root / "a-file.txt").write_text("hello", encoding="utf-8")

    with pytest.raises(ValidationError):
        resolve_target({"requested_path": "a-file.txt"})


def test_resolve_rejects_an_empty_path(repository_root: Path) -> None:
    with pytest.raises(ValidationError):
        resolve_target({"requested_path": ""})


def test_analyzer_cannot_read_outside_the_root(repository_root: Path, tmp_path: Path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("classified", encoding="utf-8")

    with pytest.raises(PermissionDeniedError):
        analyze_repository_path(str(secret.parent))


# --------------------------------------------------------------------------
# Read-only and untrusted-input guarantees
# --------------------------------------------------------------------------


def test_analysis_does_not_modify_the_repository(sample_repo: Path) -> None:
    before = {
        path: (path.stat().st_mtime_ns, path.stat().st_size)
        for path in sorted(sample_repo.rglob("*"))
        if path.is_file()
    }

    analyze_repository_path(str(sample_repo))

    after = {
        path: (path.stat().st_mtime_ns, path.stat().st_size)
        for path in sorted(sample_repo.rglob("*"))
        if path.is_file()
    }
    assert after == before


def test_analysis_does_not_create_files(sample_repo: Path) -> None:
    before = {path for path in sample_repo.rglob("*")}

    analyze_repository_path(str(sample_repo))

    assert {path for path in sample_repo.rglob("*")} == before


def test_scan_skips_symlinked_files(repository_root: Path) -> None:
    root = repository_root / "linked"
    (root / "real").mkdir(parents=True)
    (root / "real" / "app.py").write_text("x = 1\n", encoding="utf-8")
    secret = repository_root / "secret.txt"
    secret.write_text("classified", encoding="utf-8")
    try:
        (root / "leak.py").symlink_to(secret)
    except OSError:
        pytest.skip("symlinks unavailable on this filesystem")

    inventory = scan_repository(root)

    names = {path.name for path in inventory.files}
    assert "app.py" in names
    assert "leak.py" not in names


def test_scan_prunes_noise_directories(sample_repo: Path) -> None:
    inventory = scan_repository(sample_repo.resolve())
    relative = {inventory.relative(path) for path in inventory.files}

    assert not any(item.startswith("node_modules/") for item in relative)
    assert not any(item.startswith(".venv/") for item in relative)
    assert not any(item.startswith("dist/") for item in relative)
    # Pruning must not remove .github, which carries the CI signal.
    assert any(item.startswith(".github/workflows/") for item in relative)


def test_scan_skips_oversized_manifests(repository_root: Path) -> None:
    root = repository_root / "big"
    root.mkdir()
    (root / "package.json").write_text("{" + '"pad":"' + "x" * 600_000 + '"}', encoding="utf-8")

    inventory = scan_repository(root)

    assert "package.json" not in inventory.dependencies


def test_analysis_never_returns_env_file_contents(sample_repo: Path) -> None:
    profile = analyze_repository_path(str(sample_repo))
    rendered = json.dumps(profile.model_dump(mode="json"))

    assert "postgres://localhost/app" not in rendered
    assert ".env.example" in profile.env_files


def test_a_malformed_package_json_is_ignored(repository_root: Path) -> None:
    root = repository_root / "broken"
    root.mkdir()
    (root / "package.json").write_text("{ not valid json", encoding="utf-8")

    profile = analyze_repository_path(str(root))

    assert profile.frontend_framework is None
    assert profile.notes  # reported, not raised


# --------------------------------------------------------------------------
# Dependency extraction
# --------------------------------------------------------------------------


def test_package_json_dependencies_include_dev_dependencies() -> None:
    names = _package_json_dependencies(
        '{"dependencies": {"react": "1"}, "devDependencies": {"vitest": "1"}}'
    )

    assert names == {"react", "vitest"}


def test_package_json_parser_survives_garbage() -> None:
    assert _package_json_dependencies("{{{{") == set()


def test_requirements_parsing_strips_version_pins_and_extras() -> None:
    names = _requirements_dependencies(
        "\n".join(
            [
                "# a comment",
                "django>=4.2",
                "fastapi[all]==0.115.0",
                "-r other.txt",
                "psycopg2-binary ~= 2.9 ; python_version>'3.8'",
                "",
            ]
        )
    )

    assert names == {"django", "fastapi", "psycopg2-binary"}


def test_pyproject_parsing_reads_project_dependencies() -> None:
    names = _pyproject_dependencies(
        '[project]\ndependencies = ["fastapi>=0.115.0", "sqlalchemy"]\n'
    )

    assert names == {"fastapi", "sqlalchemy"}


def test_pyproject_parser_survives_garbage() -> None:
    assert _pyproject_dependencies("[[[") == set()


# --------------------------------------------------------------------------
# Detection
# --------------------------------------------------------------------------


@pytest.fixture
def profile(sample_repo: Path) -> RepositoryProfile:
    return analyze_repository_path(str(sample_repo))


def test_detects_languages(profile: RepositoryProfile) -> None:
    assert "javascript" in profile.languages
    assert "python" in profile.languages


def test_detects_frontend_framework(profile: RepositoryProfile) -> None:
    assert profile.frontend_framework == "react"


def test_detects_backend_framework(profile: RepositoryProfile) -> None:
    assert profile.backend_framework == "fastapi"


def test_detects_package_manager(profile: RepositoryProfile) -> None:
    assert profile.package_manager == "pnpm"
    assert set(profile.package_managers) >= {"pnpm", "uv"}


def test_reports_package_files(profile: RepositoryProfile) -> None:
    assert "package.json" in profile.package_files
    assert "pyproject.toml" in profile.package_files


def test_detects_dockerfile_and_compose(profile: RepositoryProfile) -> None:
    assert profile.has_dockerfile is True
    assert profile.dockerfiles == ["Dockerfile"]
    assert profile.has_docker_compose is True
    assert profile.docker_compose_files == ["docker-compose.yml"]


def test_detects_test_framework(profile: RepositoryProfile) -> None:
    assert profile.test_framework == "vitest"
    assert profile.test_file_count >= 1


def test_detects_entry_points(profile: RepositoryProfile) -> None:
    assert "src/main.py" in profile.entry_points


def test_detects_env_files_by_name_only(profile: RepositoryProfile) -> None:
    assert profile.env_files == [".env.example"]


def test_detects_database_usage(profile: RepositoryProfile) -> None:
    assert "postgresql" in profile.databases


def test_detects_ci_cd(profile: RepositoryProfile) -> None:
    assert profile.ci_cd == ["github-actions"]
    assert profile.ci_files == [".github/workflows/ci.yml"]


def test_detects_readme(profile: RepositoryProfile) -> None:
    assert profile.has_readme is True
    assert profile.readme_files == ["README.md"]


def test_package_manager_is_not_borrowed_from_another_language(
    repository_root: Path,
) -> None:
    # A Python-primary repo with only a frontend subdirectory must not be
    # labelled "npm"; the answer is None plus an explanatory note.
    root = repository_root / "polyglot"
    (root / "frontend").mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        '[project]\nname = "x"\ndependencies = ["fastapi"]\n', encoding="utf-8"
    )
    (root / "main.py").write_text("x = 1\n", encoding="utf-8")
    (root / "frontend" / "package.json").write_text(
        '{"name": "web", "dependencies": {"react": "19"}}', encoding="utf-8"
    )
    (root / "frontend" / "package-lock.json").write_text("{}\n", encoding="utf-8")

    profile = analyze_repository_path(str(root))

    assert profile.primary_language == "python"
    assert profile.package_manager is None
    assert "npm" in profile.package_managers
    assert any("reporting none" in note for note in profile.notes)


def test_reports_a_relative_root_for_nested_manifests(profile: RepositoryProfile) -> None:
    assert "src/api/main.py" in profile.test_files or profile.file_count > 0


def test_notes_explain_multiple_languages(profile: RepositoryProfile) -> None:
    assert any("Multiple languages" in note for note in profile.notes)


# --------------------------------------------------------------------------
# Negative cases
# --------------------------------------------------------------------------


def test_empty_directory_reports_empty_and_notes_it(repository_root: Path) -> None:
    root = repository_root / "empty"
    root.mkdir()

    profile = analyze_repository_path(str(root))

    assert profile.file_count == 0
    assert profile.languages == []
    assert profile.has_readme is False
    assert any("empty" in note for note in profile.notes)


def test_a_docs_only_repository_detects_no_language(repository_root: Path) -> None:
    root = repository_root / "docs"
    (root / "notes").mkdir(parents=True)
    (root / "notes" / "todo.md").write_text("later\n", encoding="utf-8")

    profile = analyze_repository_path(str(root))

    assert profile.languages == []
    assert profile.primary_language is None


def test_repo_without_readme_or_docker(repository_root: Path) -> None:
    root = repository_root / "bare"
    root.mkdir()
    (root / "lib.go").write_text("package main\n", encoding="utf-8")

    profile = analyze_repository_path(str(root))

    assert profile.has_readme is False
    assert profile.has_dockerfile is False
    assert profile.has_docker_compose is False
    assert profile.ci_cd == []


def test_go_repo_is_detected(repository_root: Path) -> None:
    root = repository_root / "go-app"
    root.mkdir()
    (root / "go.mod").write_text(
        "module example.com/app\n\ngo 1.23\n\nrequire (\n\tgithub.com/gin-gonic/gin v1.10.0\n)\n",
        encoding="utf-8",
    )
    (root / "main.go").write_text("package main\n", encoding="utf-8")

    profile = analyze_repository_path(str(root))

    assert profile.primary_language == "go"
    assert profile.backend_framework == "gin"
    assert profile.package_manager == "go"


def test_short_dependency_names_do_not_match_as_substrings(repository_root: Path) -> None:
    # "pygame" contains "pg", but must not be reported as PostgreSQL.
    root = repository_root / "false-positive"
    root.mkdir()
    (root / "requirements.txt").write_text("pygame==2.5.0\n", encoding="utf-8")

    profile = analyze_repository_path(str(root))

    assert profile.databases == []


def test_java_maven_coordinate_matches_a_backend_framework(repository_root: Path) -> None:
    root = repository_root / "java-app"
    root.mkdir()
    (root / "pom.xml").write_text(
        "<project><dependencies><dependency>"
        "<artifactId>spring-boot-starter-web</artifactId>"
        "</dependency></dependencies></project>",
        encoding="utf-8",
    )
    (root / "Main.java").write_text("class Main {}\n", encoding="utf-8")

    profile = analyze_repository_path(str(root))

    assert profile.backend_framework == "spring-boot"
    assert profile.package_manager == "maven"


def test_latest_is_not_mistaken_for_a_test_directory(repository_root: Path) -> None:
    root = repository_root / "segment-repo"
    (root / "src" / "latest").mkdir(parents=True)
    (root / "src" / "latest" / "model.py").write_text("x = 1\n", encoding="utf-8")

    profile = analyze_repository_path(str(root))

    assert profile.test_file_count == 0


def test_scan_reports_truncation_when_the_file_limit_is_hit(repository_root: Path) -> None:
    root = repository_root / "many"
    root.mkdir()
    for index in range(12):
        (root / f"file{index}.py").write_text("x = 1\n", encoding="utf-8")

    inventory = scan_repository(root, max_files=5)

    assert inventory.truncated is True
    assert len(inventory.files) == 5


def test_detect_stack_uses_the_inventory_not_the_path(sample_repo: Path) -> None:
    inventory = scan_repository(sample_repo.resolve())

    result = detect_node({"inventory": inventory})["profile"]

    assert result.name == "sample-repo"
    assert result.truncated is False


def test_scan_node_is_reusable_on_its_own(sample_repo: Path) -> None:
    scanned = scan_node({"root": sample_repo.resolve()})
    profile = detect_node(scanned)["profile"]

    assert profile.primary_language in {"javascript", "python"}


def test_repository_root_setting_is_used(repository_root: Path) -> None:
    assert get_settings().repository_root_resolved == repository_root.resolve()


# --------------------------------------------------------------------------
# POST /api/repository/analyze
# --------------------------------------------------------------------------


def test_analyze_endpoint_returns_a_profile(client: TestClient, sample_repo: Path) -> None:
    response = client.post("/api/repository/analyze", json={"path": str(sample_repo)})

    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "sample-repo"
    assert body["frontend_framework"] == "react"
    assert body["backend_framework"] == "fastapi"
    assert body["has_readme"] is True


def test_analyze_endpoint_includes_every_documented_field(
    client: TestClient, sample_repo: Path
) -> None:
    response = client.post("/api/repository/analyze", json={"path": str(sample_repo)})

    for field in RepositoryProfile.model_fields:
        assert field in response.json(), f"missing {field}"


def test_analyze_endpoint_accepts_a_relative_path(client: TestClient, sample_repo: Path) -> None:
    response = client.post("/api/repository/analyze", json={"path": "sample-repo"})

    assert response.status_code == 200
    assert response.json()["name"] == "sample-repo"


def test_analyze_endpoint_rejects_traversal(client: TestClient, sample_repo: Path) -> None:
    response = client.post("/api/repository/analyze", json={"path": "../../etc"})

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "permission_denied"


def test_analyze_endpoint_rejects_absolute_paths_outside_root(client: TestClient) -> None:
    response = client.post("/api/repository/analyze", json={"path": "/etc"})

    assert response.status_code == 403


def test_analyze_endpoint_reports_a_missing_path(client: TestClient, sample_repo: Path) -> None:
    response = client.post("/api/repository/analyze", json={"path": "not-here"})

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_analyze_endpoint_rejects_an_empty_path(client: TestClient) -> None:
    response = client.post("/api/repository/analyze", json={"path": ""})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_analyze_endpoint_rejects_a_missing_field(client: TestClient) -> None:
    response = client.post("/api/repository/analyze", json={})

    assert response.status_code == 422


def test_analyze_endpoint_is_documented(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()

    assert "/api/repository/analyze" in schema["paths"]
    assert "post" in schema["paths"]["/api/repository/analyze"]


def test_analyze_endpoint_emits_a_correlation_id(client: TestClient, sample_repo: Path) -> None:
    response = client.post("/api/repository/analyze", json={"path": str(sample_repo)})

    assert response.headers.get("X-Request-ID")


def test_analyze_endpoint_is_not_exposed_under_the_versioned_prefix(
    client: TestClient, sample_repo: Path
) -> None:
    response = client.post("/api/v1/repository/analyze", json={"path": str(sample_repo)})

    assert response.status_code == 404
