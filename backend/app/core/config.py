"""Application settings.

All configuration is loaded from environment variables (optionally via a
``.env`` file). Values are prefixed with ``AEGIS_`` to avoid collisions with
other tooling that may share the same environment.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

Environment = Literal["local", "dev", "staging", "prod"]


class Settings(BaseSettings):
    """Runtime configuration for the Aegis backend."""

    model_config = SettingsConfigDict(
        env_prefix="AEGIS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Service identity -------------------------------------------------
    app_name: str = "Aegis Backend"
    version: str = "0.1.0"
    environment: Environment = "local"
    debug: bool = True

    # --- HTTP server ------------------------------------------------------
    host: str = "0.0.0.0"
    port: int = 8000
    api_prefix: str = "/api/v1"

    # Origins allowed to call the API from a browser. The Vite dev server and
    # the Docker frontend are listed by default so local development works
    # without extra configuration.
    cors_origins: list[str] = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:4173",
        "http://localhost:8080",
    ]

    # --- Observability ----------------------------------------------------
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_format: Literal["json", "console"] = "console"

    # --- Storage ----------------------------------------------------------
    # Declared now so the dependency boundaries are explicit, but nothing
    # connects to them yet (see docs/roadmap.md).
    database_url: str | None = None
    redis_url: str | None = None

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        """Accept a comma-separated string, which is friendlier in ``.env``."""
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value

    @property
    def is_local(self) -> bool:
        return self.environment == "local"


@lru_cache
def get_settings() -> Settings:
    """Return the cached settings instance.

    Cached so that configuration is parsed once per process.
    """
    return Settings()
