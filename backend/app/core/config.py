"""Application settings.

All configuration comes from environment variables, optionally via a ``.env``
file. Backend variables use the ``AEGIS_`` prefix so they can share an
environment with other tooling.

Variables are grouped into nested sections using a double underscore, for
example ``AEGIS_LLM__API_KEY``. Sections default to "not configured" and are
never validated at startup, so the service boots with no external services
available — see :func:`Settings.validate_configuration`.

Credentials are held as :class:`~pydantic.SecretStr`. That masks them in
``repr()``, in logs and in anything that serialises the settings object, so a
key cannot leak by accident. Nothing here performs I/O; the sections describe
configuration only, and no client is wired up yet.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

Environment = Literal["local", "dev", "staging", "prod"]

_LOG_LEVELS: dict[str, int] = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}


class _Section(BaseModel):
    """Base for configuration sections."""

    model_config = ConfigDict(extra="ignore", validate_assignment=True)


class LLMSettings(_Section):
    """LLM provider configuration. No client is implemented yet."""

    provider: str = "anthropic"
    model: str = "claude-sonnet-5"
    api_key: SecretStr | None = None
    base_url: str | None = None
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    max_tokens: int = Field(default=4096, gt=0)
    timeout_seconds: float = Field(default=60.0, gt=0)

    @property
    def is_configured(self) -> bool:
        return self.api_key is not None


class GitHubSettings(_Section):
    """GitHub API configuration. No client is implemented yet."""

    token: SecretStr | None = None
    org: str | None = None
    api_url: str = "https://api.github.com"
    timeout_seconds: float = Field(default=30.0, gt=0)

    @property
    def is_configured(self) -> bool:
        return self.token is not None


class MCPSettings(_Section):
    """MCP client configuration. No servers are registered yet."""

    enabled: bool = False
    request_timeout_seconds: float = Field(default=30.0, gt=0)
    # Always require approval for destructive tools once tools exist. Turning
    # this off is a deliberate, explicit choice rather than a default.
    destructive_tools_require_approval: bool = True
    # Map of server name -> launch command, written as ``name=command`` pairs:
    #   AEGIS_MCP__SERVERS=github=python -m mcp_servers.github,docker=docker-mcp
    servers: Annotated[dict[str, str], NoDecode] = Field(default_factory=dict)

    @field_validator("servers", mode="before")
    @classmethod
    def _parse_servers(cls, value: object) -> object:
        """Accept a comma-separated ``name=command`` string from the env.

        Without ``NoDecode`` pydantic-settings would require a JSON object here,
        which is unpleasant to write in a ``.env`` file.
        """
        if isinstance(value, str):
            servers: dict[str, str] = {}
            for pair in value.split(","):
                pair = pair.strip()
                if not pair:
                    continue
                if "=" not in pair:
                    raise ValueError(f"MCP server entry {pair!r} must use the form name=command")
                name, command = pair.split("=", 1)
                name, command = name.strip(), command.strip()
                if not name or not command:
                    raise ValueError(f"MCP server entry {pair!r} is missing a name or command")
                servers[name] = command
            return servers
        return value

    @property
    def is_configured(self) -> bool:
        return self.enabled and bool(self.servers)


class DatabaseSettings(_Section):
    """PostgreSQL-compatible database configuration. No engine is created yet."""

    # A URL may embed a password, so it is treated as a secret.
    url: SecretStr | None = None
    pool_size: int = Field(default=5, gt=0)
    max_overflow: int = Field(default=10, ge=0)
    echo: bool = False
    connect_timeout_seconds: float = Field(default=10.0, gt=0)

    @property
    def is_configured(self) -> bool:
        return self.url is not None


class RedisSettings(_Section):
    """Redis-compatible cache configuration. No client is created yet."""

    url: SecretStr | None = None
    max_connections: int = Field(default=10, gt=0)
    socket_timeout_seconds: float = Field(default=5.0, gt=0)

    @property
    def is_configured(self) -> bool:
        return self.url is not None


class AWSSettings(_Section):
    """AWS configuration. No boto3 client is created and no import exists yet."""

    region: str | None = None
    account_id: str | None = None
    # Prefer a named profile or an instance role over static keys.
    profile: str | None = None
    access_key_id: SecretStr | None = None
    secret_access_key: SecretStr | None = None

    @property
    def is_configured(self) -> bool:
        return bool(self.region) and bool(self.profile or self.access_key_id is not None)


class Settings(BaseSettings):
    """Runtime configuration for the Aegis backend."""

    model_config = SettingsConfigDict(
        env_prefix="AEGIS_",
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        extra="ignore",
    )

    # --- Service identity -------------------------------------------------
    app_name: str = "Aegis Backend"
    version: str = "0.1.0"
    environment: Environment = "local"
    debug: bool = True

    # --- HTTP server ------------------------------------------------------
    host: str = "0.0.0.0"
    port: int = Field(default=8000, gt=0, le=65535)
    api_prefix: str = "/api/v1"

    # Origins allowed to call the API from a browser. The Vite dev server and
    # the Docker frontend are listed by default so local development works
    # without extra configuration.
    #
    # NoDecode stops pydantic-settings from JSON-decoding this field. Without
    # it a list coming from a .env file must be written as JSON, which makes the
    # comma-separated form below impossible to use.
    cors_origins: Annotated[list[str], NoDecode] = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:4173",
        "http://localhost:8080",
    ]

    # --- Observability ----------------------------------------------------
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_format: Literal["json", "console"] = "console"
    # Trust X-Forwarded-* headers only behind a proxy you control.
    trust_forwarded_headers: bool = False

    # --- Repository analysis ----------------------------------------------
    # The only directory tree the repository analyzer may read. A request path is
    # resolved and then must still land inside this root, so ".." and symlinks
    # cannot walk the analyzer out of it.
    #
    # Defaults to the backend working directory, which is the safe default:
    # pointing AEGIS_REPOSITORY_ROOT at a checkout is what grants wider access.
    repository_root: Path = Field(default_factory=Path.cwd)

    @property
    def repository_root_resolved(self) -> Path:
        """Absolute, symlink-resolved form of :attr:`repository_root`."""
        return self.repository_root.expanduser().resolve()

    # --- Integrations (configured only; no clients implemented yet) -------
    llm: LLMSettings = Field(default_factory=LLMSettings)
    github: GitHubSettings = Field(default_factory=GitHubSettings)
    mcp: MCPSettings = Field(default_factory=MCPSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    redis: RedisSettings = Field(default_factory=RedisSettings)
    aws: AWSSettings = Field(default_factory=AWSSettings)

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

    @property
    def log_level_number(self) -> int:
        return _LOG_LEVELS[self.log_level]

    def configured_integrations(self) -> dict[str, bool]:
        """Report which integrations have configuration, for health output.

        Reports *configuration*, never connectivity: nothing here opens a
        connection.
        """
        return {
            "llm": self.llm.is_configured,
            "github": self.github.is_configured,
            "mcp": self.mcp.is_configured,
            "database": self.database.is_configured,
            "redis": self.redis.is_configured,
            "aws": self.aws.is_configured,
        }

    def safe_summary(self) -> dict[str, object]:
        """Non-secret summary suitable for logs and diagnostics.

        Secret values are reduced to a boolean so they can never be printed.
        """
        return {
            "app_name": self.app_name,
            "version": self.version,
            "environment": self.environment,
            "debug": self.debug,
            "api_prefix": self.api_prefix,
            "log_level": self.log_level,
            "log_format": self.log_format,
            # Not a secret, and the single most useful thing to know when
            # diagnosing an "outside the allowed root" rejection.
            "repository_root": str(self.repository_root_resolved),
            "configured_integrations": self.configured_integrations(),
        }


@lru_cache
def get_settings() -> Settings:
    """Return the cached settings instance.

    Cached so configuration is parsed once per process. Tests that need
    different values should call ``get_settings.cache_clear()``.
    """
    return Settings()
