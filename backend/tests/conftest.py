"""Shared pytest fixtures for the backend test suite."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import create_app


@pytest.fixture
def client() -> Iterator[TestClient]:
    """Yield a test client with the full lifespan (startup/shutdown) exercised."""
    with TestClient(create_app()) as test_client:
        yield test_client


@pytest.fixture
def repository_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point the analyzer's containment root at a temporary directory.

    Every repository-analysis test needs this: the root defaults to the process
    working directory, and without it a test could read the real source tree.
    """
    root = tmp_path / "allowed"
    root.mkdir()
    get_settings.cache_clear()
    monkeypatch.setenv("AEGIS_REPOSITORY_ROOT", str(root))
    yield root
    get_settings.cache_clear()


def _write(root: Path, relative: str, content: str = "") -> Path:
    """Write a fixture file, creating parent directories."""
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


@pytest.fixture
def sample_repo(repository_root: Path) -> Path:
    """A small but realistic full-stack repository.

    Deliberately mixes stacks so detection is exercised on a polyglot tree: a
    React frontend, a FastAPI backend, Vitest tests, Postgres via SQLAlchemy,
    Docker, and GitHub Actions.
    """
    root = repository_root / "sample-repo"

    _write(root, "README.md", "# Sample Repo\n\nA fixture.\n")
    _write(root, ".gitignore", "node_modules/\n__pycache__/\n.env\n")
    _write(root, ".env.example", "DATABASE_URL=postgres://localhost/app\n")
    _write(
        root,
        "package.json",
        """{
  "name": "sample-frontend",
  "version": "0.1.0",
  "main": "src/index.js",
  "scripts": { "dev": "vite", "test": "vitest" },
  "dependencies": { "react": "^19.0.0", "react-dom": "^19.0.0" },
  "devDependencies": { "vitest": "^2.1.0", "vite": "^6.0.0" }
}
""",
    )
    _write(root, "pnpm-lock.yaml", "lockfileVersion: '9.0'\n")
    _write(
        root,
        "pyproject.toml",
        """[project]
name = "sample-api"
version = "0.1.0"
dependencies = [
  "fastapi>=0.115.0",
  "uvicorn>=0.32.0",
  "sqlalchemy>=2.0.0",
  "psycopg2-binary>=2.9.0",
]
""",
    )
    _write(root, "uv.lock", "version = 1\n")
    _write(root, "Dockerfile", "FROM python:3.12-slim\n")
    _write(root, "docker-compose.yml", "services:\n  api:\n    build: .\n")
    _write(root, ".github/workflows/ci.yml", "name: CI\non: [push]\n")
    _write(root, "src/index.js", "import React from 'react';\n")
    _write(root, "src/App.jsx", "export default function App() { return null; }\n")
    _write(root, "src/main.py", "def main() -> None: ...\n")
    _write(root, "src/api/main.py", "from fastapi import FastAPI\n")
    _write(root, "tests/test_main.py", "def test_main(): ...\n")
    _write(root, "tests/App.test.js", "test('renders', () => {});\n")

    # Noise that must be pruned rather than counted.
    _write(root, "node_modules/left-pad/index.js", "module.exports = 1;\n")
    _write(root, ".venv/lib/fake.py", "x = 1\n")
    _write(root, "dist/bundle.js", "compiled\n")

    return root
