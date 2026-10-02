"""Repository analysis.

Static inspection of a local repository. The repository is treated as untrusted
input throughout:

- nothing is executed, imported, or evaluated; only file *names* and small text
  manifests are read
- symlinks are skipped, so a crafted link cannot point the scan at ``/etc``
- every file is size-capped before it is read
- the resolved path must stay inside the configured repository root
- ``.env`` files are listed by name only, never opened, so secrets cannot reach
  the API response or the logs
- the repository is never written to; every open is read-only

Detection is heuristic by nature, so the returned profile carries a ``notes``
field describing anything ambiguous rather than presenting a false certainty.
"""

from __future__ import annotations

import json
import os
import re
import tomllib
from pathlib import Path

from app.models.repository import FileInventory, RepositoryProfile

# --- Scan limits ---------------------------------------------------------

MAX_SCAN_DEPTH = 12
MAX_SCANNED_FILES = 5000
#: Largest file read into memory. Manifests are tiny; anything bigger is skipped.
MAX_READ_BYTES = 512 * 1024
#: Caps on list fields, so one pathological repository cannot bloat a response.
MAX_LISTED_PATHS = 50
#: Shortest dependency needle allowed to match as a substring rather than a
#: whole token. Keeps short names like "pg" from matching "pygame".
_SUBSTRING_MATCH_MIN_LENGTH = 8

# --- Traversal ----------------------------------------------------------

#: Directories that never change the answer but are expensive to walk.
PRUNED_DIRECTORIES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".idea",
        ".vscode",
        ".tox",
        ".nox",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "__pycache__",
        "node_modules",
        "bower_components",
        "vendor",
        "venv",
        ".venv",
        "env",
        "site-packages",
        "dist",
        "build",
        "target",
        ".next",
        ".nuxt",
        ".svelte-kit",
        ".terraform",
        "coverage",
        "htmlcov",
        ".gradle",
        ".cache",
    }
)

#: Hidden directories that do matter, so they survive the hidden-name filter.
ALLOWED_HIDDEN_DIRECTORIES = frozenset({".github", ".circleci"})

# --- Signatures ----------------------------------------------------------

LANGUAGE_BY_SUFFIX: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".mts": "typescript",
    ".tsx": "typescript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".kt": "kotlin",
    ".rb": "ruby",
    ".php": "php",
    ".cs": "csharp",
    ".c": "c",
    ".h": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".hpp": "cpp",
    ".swift": "swift",
    ".scala": "scala",
    ".ex": "elixir",
    ".exs": "elixir",
    ".sh": "shell",
    ".bash": "shell",
    ".sql": "sql",
    ".html": "html",
    ".css": "css",
    ".scss": "css",
    ".vue": "vue",
    ".svelte": "svelte",
    ".dart": "dart",
}

#: Ordered so the first hit is the most specific answer: Next.js is reported as
#: ``nextjs``, not ``react``.
FRONTEND_FRAMEWORK_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("nextjs", ("next",)),
    ("nuxt", ("nuxt",)),
    ("remix", ("@remix-run/react", "@remix-run/node")),
    ("sveltekit", ("@sveltejs/kit",)),
    ("angular", ("@angular/core",)),
    ("vue", ("vue",)),
    ("svelte", ("svelte",)),
    ("react", ("react",)),
    ("astro", ("astro",)),
)

BACKEND_FRAMEWORK_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("fastapi", ("fastapi",)),
    ("django", ("django",)),
    ("flask", ("flask",)),
    ("nestjs", ("@nestjs/core",)),
    ("spring-boot", ("spring-boot-starter", "spring-boot")),
    ("rails", ("rails",)),
    ("gin", ("github.com/gin-gonic/gin", "gin-gonic")),
    ("echo", ("github.com/labstack/echo", "labstack/echo")),
    ("axum", ("axum",)),
    ("actix-web", ("actix-web",)),
    ("laravel", ("laravel/framework",)),
)

DATABASE_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("postgresql", ("psycopg", "psycopg2", "asyncpg", "pg", "postgres", "sqlalchemy")),
    ("mysql", ("pymysql", "mysqlclient", "mysql2", "mysql")),
    ("sqlite", ("sqlite3", "sqlite")),
    ("mongodb", ("pymongo", "mongoose", "mongodb")),
    ("redis", ("redis",)),
    ("prisma", ("prisma",)),
    ("sequelize", ("sequelize",)),
    ("typeorm", ("typeorm",)),
    ("drizzle", ("drizzle-orm",)),
    ("sqlmodel", ("sqlmodel",)),
)

TEST_FRAMEWORK_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("vitest", ("vitest",)),
    ("jest", ("jest",)),
    ("playwright", ("@playwright/test", "playwright")),
    ("cypress", ("cypress",)),
    ("pytest", ("pytest",)),
    ("mocha", ("mocha",)),
    ("junit", ("junit-jupiter", "junit")),
    ("rspec", ("rspec",)),
)

#: Directory and file names that imply a test framework even with no manifest.
TEST_CONFIG_FILES: dict[str, str] = {
    "pytest.ini": "pytest",
    "conftest.py": "pytest",
    "jest.config.js": "jest",
    "jest.config.ts": "jest",
    "vitest.config.ts": "vitest",
    "vitest.config.js": "vitest",
    "playwright.config.ts": "playwright",
    "cypress.json": "cypress",
    "karma.conf.js": "karma",
}

TEST_FILE_SUFFIXES = (".test.js", ".spec.ts", ".test.tsx", "_test.go", "test.java", "_spec.rb")
#: Suffixes a file must have to count as a test file.
_TEST_SUFFIXES = frozenset({".py", ".js", ".jsx", ".ts", ".tsx", ".rb", ".go", ".java"})
#: Directory names that indicate a test suite. Compared as whole path segments,
#: so "src/latest/" is not mistaken for a test directory.
TEST_DIRECTORY_NAMES = frozenset({"test", "tests", "__tests__", "spec", "specs"})

README_NAMES = ("readme.md", "readme", "readme.rst", "readme.txt", "readme.markdown")
COMPOSE_NAMES = ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml")
DOCKERFILE_PATTERN = "dockerfile"

ENV_FILE_NAMES = (
    ".env",
    ".env.example",
    ".env.sample",
    ".env.template",
    ".env.local",
    ".env.development",
    ".env.development.local",
    ".env.production",
    ".env.test",
    ".env.staging",
)

PACKAGE_MANAGER_FILES: tuple[tuple[str, str], ...] = (
    ("pnpm-lock.yaml", "pnpm"),
    ("yarn.lock", "yarn"),
    ("bun.lockb", "bun"),
    ("bun.lock", "bun"),
    ("package-lock.json", "npm"),
    ("uv.lock", "uv"),
    ("poetry.lock", "poetry"),
    ("pom.xml", "maven"),
    ("build.gradle", "gradle"),
    ("build.gradle.kts", "gradle"),
    ("go.mod", "go"),
    ("cargo.lock", "cargo"),
    ("cargo.toml", "cargo"),
    ("gemfile.lock", "bundler"),
    ("composer.lock", "composer"),
    ("composer.json", "composer"),
)

#: Manifest files that are read for dependency names.
MANIFEST_NAMES = (
    "package.json",
    "requirements.txt",
    "requirements-dev.txt",
    "pyproject.toml",
    "go.mod",
    "cargo.toml",
    "gemfile",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
)

#: Preference order for reporting the primary manager, by primary language.
LANGUAGE_MANAGER_PREFERENCE: dict[str, tuple[str, ...]] = {
    "javascript": ("pnpm", "yarn", "bun", "npm"),
    "typescript": ("pnpm", "yarn", "bun", "npm"),
    "python": ("uv", "poetry", "pip"),
    "go": ("go",),
    "rust": ("cargo",),
    "java": ("maven", "gradle"),
    "kotlin": ("maven", "gradle"),
    "ruby": ("bundler",),
    "php": ("composer",),
    "csharp": ("nuget",),
    "elixir": (),
}

#: Filenames that are plausible program entry points in most ecosystems.
ENTRY_POINT_FILENAMES = frozenset(
    {
        "main.py",
        "app.py",
        "manage.py",
        "wsgi.py",
        "asgi.py",
        "__main__.py",
        "server.py",
        "run.py",
        "index.js",
        "server.js",
        "app.js",
        "main.js",
        "index.mjs",
        "server.mjs",
        "index.ts",
        "main.ts",
        "main.go",
        "main.rs",
        "program.cs",
        "index.php",
        "artisan",
        "config.ru",
    }
)
#: Entry points live near the top of a tree; deeper matches are library code.
MAX_ENTRY_POINT_DEPTH = 3


# --- Safe filesystem helpers --------------------------------------------


def _ignore_os_error(_: OSError) -> None:
    """Swallow unreadable paths; a permission error must not abort the scan."""


def _read_text(path: Path, *, max_bytes: int = MAX_READ_BYTES) -> str | None:
    """Read a small text file, or return ``None`` if that is not safe.

    Symlinks, non-files, oversized files and unreadable files all return
    ``None``. Decoding is lossy so binary or odd encodings never raise.
    """
    try:
        if path.is_symlink() or not path.is_file():
            return None
        if path.stat().st_size > max_bytes:
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _keep_directory(name: str) -> bool:
    """Decide whether to descend into a directory."""
    if name in PRUNED_DIRECTORIES:
        return False
    # Hidden directories are skipped by default; .github carries CI signals.
    return not name.startswith(".") or name in ALLOWED_HIDDEN_DIRECTORIES


# --- Dependency extraction ----------------------------------------------


def _package_json_dependencies(text: str) -> set[str]:
    """Extract dependency names from package.json.

    ``json.loads`` cannot execute anything; a malformed file is simply ignored.
    """
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return set()
    if not isinstance(data, dict):
        return set()

    names: set[str] = set()
    for key in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        block = data.get(key)
        if isinstance(block, dict):
            names.update(str(name).lower() for name in block)
    return names


def _requirements_dependencies(text: str) -> set[str]:
    """Extract names from a pip requirements file, ignoring version specifiers."""
    names: set[str] = set()
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        # "django>=4", "fastapi[all]==0.1", "pkg @ https://..."
        name = line.split("[", 1)[0].split("@", 1)[0]
        for separator in ("==", ">=", "<=", "~=", "!=", ">", "<"):
            name = name.split(separator, 1)[0]
        name = name.strip().lower()
        if name:
            names.add(name)
    return names


def _pyproject_dependencies(text: str) -> set[str]:
    """Extract dependency names from pyproject.toml using the stdlib TOML parser."""
    try:
        data = tomllib.loads(text)
    except (tomllib.TOMLDecodeError, ValueError):
        return set()

    names: set[str] = set()

    def add_from_requirements(requirements: object) -> None:
        if isinstance(requirements, list):
            for item in requirements:
                if not isinstance(item, str):
                    continue
                head = item.split("[", 1)[0].split("@", 1)[0]
                head = head.split(";", 1)[0].strip()
                for separator in ("==", ">=", "<=", "~=", "!=", ">", "<", " "):
                    head = head.split(separator, 1)[0]
                if head:
                    names.add(head.lower())

    project = data.get("project")
    if isinstance(project, dict):
        add_from_requirements(project.get("dependencies"))
        optional = project.get("optional-dependencies")
        if isinstance(optional, dict):
            for group in optional.values():
                add_from_requirements(group)

    tool_poetry = data.get("tool", {})
    if isinstance(tool_poetry, dict):
        poetry = tool_poetry.get("poetry")
        if isinstance(poetry, dict):
            add_from_requirements(poetry.get("dependencies"))

    return names


def _go_mod_dependencies(text: str) -> set[str]:
    """Extract module paths from go.mod.

    Covers both single-line ``require x v1`` directives and block form. Module
    paths are matched by shape rather than by parsing the whole grammar, which is
    all that detection needs.
    """
    names: set[str] = set()

    # Block form: one module path per line inside require ( ... ).
    block_form = r"^\s+([a-z0-9.\-]+\.[a-z]{2,}/[^\s()]+)\s+v"
    names.update(
        match.lower() for match in re.findall(block_form, text, re.IGNORECASE | re.MULTILINE)
    )
    # Single-line form: require example.com/pkg v1.2.3
    single_form = r"^\s*require\s+([a-z0-9.\-]+\.[a-z]{2,}/\S+)"
    names.update(
        match.lower() for match in re.findall(single_form, text, re.IGNORECASE | re.MULTILINE)
    )
    return names


def _cargo_dependencies(text: str) -> set[str]:
    try:
        data = tomllib.loads(text)
    except (tomllib.TOMLDecodeError, ValueError):
        return set()
    names: set[str] = set()
    for block in ("dependencies", "dev-dependencies", "build-dependencies"):
        section = data.get(block)
        if isinstance(section, dict):
            names.update(str(name).lower() for name in section)
    return names


def _text_dependencies(text: str) -> set[str]:
    """Fallback for manifests without a structured parser (Gemfile, Gradle, pom)."""
    return {token.lower() for token in text.replace('"', " ").replace("'", " ").split()}


def extract_dependencies(filename: str, text: str) -> set[str]:
    """Dispatch to the right extractor for a manifest.

    Public because remote analysis feeds it manifest text fetched over MCP, so
    local and GitHub repositories are parsed by the same code.
    """
    lowered = filename.lower()
    if lowered == "package.json":
        return _package_json_dependencies(text)
    if lowered.startswith("requirements"):
        return _requirements_dependencies(text)
    if lowered == "pyproject.toml":
        return _pyproject_dependencies(text)
    if lowered == "go.mod":
        return _go_mod_dependencies(text)
    if lowered == "cargo.toml":
        return _cargo_dependencies(text)
    return _text_dependencies(text)


# --- Scan ---------------------------------------------------------------


def scan_repository(
    root: Path,
    *,
    max_depth: int = MAX_SCAN_DEPTH,
    max_files: int = MAX_SCANNED_FILES,
) -> FileInventory:
    """Walk ``root`` and collect a bounded, symlink-free inventory.

    Reads nothing but manifest contents, and never writes.
    """
    files: list[Path] = []
    directories: set[str] = set()
    seen = 0
    truncated = False

    for dirpath, dirnames, filenames in os.walk(root, followlinks=False, onerror=_ignore_os_error):
        current = Path(dirpath)
        try:
            depth = len(current.relative_to(root).parts)
        except ValueError:  # pragma: no cover - defensive
            continue

        dirnames[:] = sorted(
            name for name in dirnames if _keep_directory(name) and not (current / name).is_symlink()
        )
        if depth >= max_depth:
            dirnames.clear()
        directories.update(dirnames)

        for filename in sorted(filenames):
            path = current / filename
            if path.is_symlink():
                continue
            seen += 1
            if len(files) >= max_files:
                truncated = True
                break
            files.append(path)

        if truncated:
            break

    dependencies: dict[str, frozenset[str]] = {}
    for path in files:
        if path.name.lower() in MANIFEST_NAMES:
            text = _read_text(path)
            if text is not None:
                dependencies[path.relative_to(root).as_posix()] = frozenset(
                    extract_dependencies(path.name, text)
                )

    return FileInventory(
        root=root,
        files=tuple(files),
        directories=frozenset(directories),
        file_count=seen,
        truncated=truncated,
        dependencies=dependencies,
    )


# --- Detection ----------------------------------------------------------


def _detect_languages(files: tuple[Path, ...]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for path in files:
        language = LANGUAGE_BY_SUFFIX.get(path.suffix.lower())
        if language:
            counts[language] = counts.get(language, 0) + 1
    return counts


def _match_markers(
    markers: tuple[tuple[str, tuple[str, ...]], ...],
    haystacks: list[str],
) -> str | None:
    """Return the first marker name present in any haystack.

    Two passes. First exact token comparison, which is what package registries use
    and what stops ``react`` matching ``react-native`` or ``pg`` matching
    ``pygame``. If that finds nothing, a substring pass runs, but only for needles
    of at least eight characters: long enough that a partial match is meaningful.
    That second pass is what catches Maven coordinates such as
    ``org.springframework.boot:spring-boot-starter-web`` and Go module paths,
    which never match a token exactly.
    """
    for name, needles in markers:
        if any(needle in haystack for needle in needles for haystack in haystacks):
            return name

    for name, needles in markers:
        if any(
            len(needle) >= _SUBSTRING_MATCH_MIN_LENGTH and needle in haystack
            for needle in needles
            for haystack in haystacks
        ):
            return name

    return None


def detect_stack(inventory: FileInventory) -> RepositoryProfile:
    """Derive a profile from an inventory.

    Every answer is a heuristic; anything ambiguous is recorded in ``notes``.
    """
    root = inventory.root
    names = [path.name for path in inventory.files]
    lowered_names = [name.lower() for name in names]
    relative = [inventory.relative(path) for path in inventory.files]
    lowered_relative = [item.lower() for item in relative]

    # Languages, primary = most files.
    counts = _detect_languages(inventory.files)
    languages = sorted(counts, key=lambda item: (-counts[item], item))
    primary_language = languages[0] if languages else None

    # Every dependency token seen, so one lookup answers several questions.
    all_dependencies = [dep for deps in inventory.dependencies.values() for dep in deps]

    frontend_framework = _match_markers(FRONTEND_FRAMEWORK_MARKERS, all_dependencies)
    backend_framework = _match_markers(BACKEND_FRAMEWORK_MARKERS, all_dependencies)

    # Package managers.
    managers: list[str] = []
    for filename, manager in PACKAGE_MANAGER_FILES:
        if manager not in managers and filename in lowered_names:
            managers.append(manager)
    if "requirements.txt" in lowered_names and "pip" not in managers:
        managers.append("pip")

    # The reported manager must belong to the primary language. Falling back to
    # any manager found would label a Python repository "npm" because it has a
    # frontend subdirectory; reporting None with a note is the honest answer.
    package_manager: str | None = None
    preference = LANGUAGE_MANAGER_PREFERENCE.get(primary_language or "", ())
    for candidate in preference:
        if candidate in managers:
            package_manager = candidate
            break

    # Containerisation.
    dockerfiles = [
        inventory.relative(path)
        for path in inventory.files
        if DOCKERFILE_PATTERN in path.name.lower()
    ]
    compose_files = [
        inventory.relative(path) for path in inventory.files if path.name.lower() in COMPOSE_NAMES
    ]

    # Tests.
    test_files = [
        inventory.relative(path)
        for path in inventory.files
        if (
            path.name.lower().endswith(TEST_FILE_SUFFIXES)
            or path.name.startswith("test_")
            or set(Path(inventory.relative(path)).parts[:-1]) & TEST_DIRECTORY_NAMES
        )
        and path.suffix.lower() in _TEST_SUFFIXES
    ]
    test_framework = _match_markers(TEST_FRAMEWORK_MARKERS, all_dependencies)
    if test_framework is None:
        for filename, framework in TEST_CONFIG_FILES.items():
            if filename in lowered_names:
                test_framework = framework
                break
    if test_framework is None and test_files:
        test_framework = "unknown"

    # Entrypoints: well-known filenames that are not buried deep in a tree, so
    # both "main.py" and "src/api/main.py" count but library modules do not.
    entry_points = []
    for path in inventory.files:
        relative_path = inventory.relative(path)
        parts = Path(relative_path).parts
        if path.name.lower() in ENTRY_POINT_FILENAMES and len(parts) <= MAX_ENTRY_POINT_DEPTH:
            entry_points.append(relative_path)
    if not entry_points:
        # Fall back to shallow top-level program files.
        entry_points = [
            inventory.relative(path)
            for path in inventory.files
            if len(Path(inventory.relative(path)).parts) == 1
            and path.suffix.lower() in {".py", ".js", ".ts", ".go", ".rs", ".java", ".php"}
        ]

    # Environment files by name only; contents are never opened.
    env_files = [
        inventory.relative(path) for path in inventory.files if path.name.lower() in ENV_FILE_NAMES
    ]

    databases = [
        name
        for name, needles in DATABASE_MARKERS
        if any(needle in dep for needle in needles for dep in all_dependencies)
    ]

    # CI/CD.
    ci_files: list[str] = []
    ci_cd: list[str] = []
    if ".github" in inventory.directories:
        workflows = [item for item in relative if item.startswith(".github/workflows/")]
        if workflows:
            ci_files.extend(workflows)
            ci_cd.append("github-actions")
    for filename, provider in (
        (".gitlab-ci.yml", "gitlab-ci"),
        ("jenkinsfile", "jenkins"),
        ("circleci/config.yml", "circleci"),
        ("azure-pipelines.yml", "azure-pipelines"),
        (".travis.yml", "travis"),
    ):
        if filename in lowered_relative or filename in lowered_names:
            ci_cd.append(provider)
            ci_files.extend(item for item in relative if item.lower().endswith(filename))

    readme_files = [
        inventory.relative(path) for path in inventory.files if path.name.lower() in README_NAMES
    ]

    notes: list[str] = []
    if inventory.truncated:
        notes.append(
            f"Scan stopped at {MAX_SCANNED_FILES} files or depth {MAX_SCAN_DEPTH}; "
            "results are partial."
        )
    if primary_language is None:
        notes.append("No recognised source file extensions were found.")
    elif len(languages) > 1:
        notes.append(f"Multiple languages present; primary chosen as {primary_language}.")
    if len(managers) > 1:
        notes.append(f"Multiple package managers found: {', '.join(sorted(managers))}.")
    elif managers and package_manager is None:
        notes.append(
            f"Found {managers[0]} but no {primary_language or 'matching'} package manager; "
            "reporting none."
        )
    if test_framework == "unknown":
        notes.append("Test files were found but no test framework could be identified.")
    if not entry_points:
        notes.append("No likely entry point could be identified.")

    return RepositoryProfile(
        name=root.name,
        root=str(root),
        file_count=inventory.file_count,
        scanned_file_count=len(inventory.files),
        truncated=inventory.truncated,
        languages=languages,
        primary_language=primary_language,
        frontend_framework=frontend_framework,
        backend_framework=backend_framework,
        package_manager=package_manager,  # type: ignore[arg-type]
        package_managers=sorted(managers),  # type: ignore[arg-type]
        package_files=sorted(inventory.dependencies),
        entry_points=sorted(entry_points)[:MAX_LISTED_PATHS],
        has_dockerfile=bool(dockerfiles),
        dockerfiles=sorted(dockerfiles)[:MAX_LISTED_PATHS],
        has_docker_compose=bool(compose_files),
        docker_compose_files=sorted(compose_files)[:MAX_LISTED_PATHS],
        test_framework=test_framework,
        test_file_count=len(test_files),
        test_files=sorted(test_files)[:MAX_LISTED_PATHS],
        env_files=sorted(env_files)[:MAX_LISTED_PATHS],
        databases=sorted(databases),
        ci_cd=sorted(set(ci_cd)),
        ci_files=sorted(set(ci_files))[:MAX_LISTED_PATHS],
        has_readme=bool(readme_files),
        readme_files=sorted(readme_files)[:MAX_LISTED_PATHS],
        notes=notes,
    )
