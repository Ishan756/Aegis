"""Tests for settings parsing."""

from __future__ import annotations

from app.core.config import Settings


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

    assert settings.database_url is None
    assert settings.redis_url is None
