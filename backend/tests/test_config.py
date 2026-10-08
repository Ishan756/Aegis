"""Tests for settings parsing."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.config import Settings

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_EXAMPLE = REPO_ROOT / ".env.example"


def test_defaults_are_local_development() -> None:
    settings = Settings(_env_file=None)

    assert settings.environment == "local"
    assert settings.api_prefix == "/api/v1"
    assert "http://localhost:5173" in settings.cors_origins


def test_cors_origins_accept_comma_separated_string() -> None:
    settings = Settings(_env_file=None, cors_origins="http://a.test, http://b.test")

    assert settings.cors_origins == ["http://a.test", "http://b.test"]


def test_optional_dependencies_default_to_unset() -> None:
    settings = Settings(_env_file=None)

    assert settings.database.url is None
    assert settings.redis.url is None
    assert settings.database.is_configured is False
    assert settings.redis.is_configured is False


def test_comma_separated_origins_load_from_env_file(tmp_path: Path) -> None:
    """A comma-separated list must survive the trip through a .env file.

    pydantic-settings JSON-decodes list fields by default, which turns this
    into a startup crash unless the field opts out with NoDecode.
    """
    env_file = tmp_path / ".env"
    env_file.write_text(
        "AEGIS_ENVIRONMENT=dev\nAEGIS_CORS_ORIGINS=http://a.test, http://b.test\n",
        encoding="utf-8",
    )

    settings = Settings(_env_file=env_file)

    assert settings.environment == "dev"
    assert settings.cors_origins == ["http://a.test", "http://b.test"]


def test_project_root_env_loads_when_started_from_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    """The repository .env is found when the process cwd is ``backend/``."""
    backend = REPO_ROOT / "backend"
    monkeypatch.chdir(backend)

    settings = Settings()

    assert settings.mcp.enabled is True
    assert settings.mcp.servers == {
        "aws": "python ../mcp_servers/aws/server.py",
        "docker": "python ../mcp_servers/docker/server.py",
    }
    assert "AEGIS_AWS__REGION" in settings.mcp.forward_environment


def test_shipped_env_example_is_valid() -> None:
    """The template users copy must load without error.

    Guards against documenting configuration the app cannot actually parse.
    """
    assert ENV_EXAMPLE.is_file(), "missing .env.example at repo root"

    settings = Settings(_env_file=ENV_EXAMPLE)

    assert settings.environment in {"local", "dev", "staging", "prod"}
    assert settings.api_prefix.startswith("/")
    assert settings.cors_origins, "at least one origin must be configured"
