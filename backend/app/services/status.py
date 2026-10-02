"""Status aggregation service.

Owns the process start time and builds the health payload reported by the API.
Keeping this out of the route handlers leaves room to add real dependency
probes (database, cache, MCP servers) without touching the HTTP layer.
"""

from __future__ import annotations

import time

from app.core.config import Settings
from app.models.health import ComponentHealth, HealthResponse

# The development stage implemented by this codebase. Updated as features land.
CURRENT_STAGE = "stage-1-foundation"


class StatusService:
    """Reports process and dependency health."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._started_at = time.monotonic()

    @property
    def uptime_seconds(self) -> float:
        return round(time.monotonic() - self._started_at, 3)

    def _components(self) -> list[ComponentHealth]:
        """Describe each known subsystem.

        Subsystems that are not wired up yet report ``not_configured`` instead
        of failing, so the dashboard can show honest progress.
        """

        def configured(name: str, url: str | None) -> ComponentHealth:
            if url:
                return ComponentHealth(name=name, status="ok", detail="Configured")
            return ComponentHealth(name=name, status="not_configured", detail="Not required yet")

        return [
            ComponentHealth(name="api", status="ok", detail="Serving requests"),
            ComponentHealth(
                name="graph", status="not_configured", detail="LangGraph workflow pending"
            ),
            ComponentHealth(
                name="mcp", status="not_configured", detail="No MCP servers registered"
            ),
            configured("database", self._settings.database_url),
            configured("cache", self._settings.redis_url),
        ]

    def health(self) -> HealthResponse:
        components = self._components()
        overall = "error" if any(c.status == "error" for c in components) else "ok"
        return HealthResponse(
            status=overall,
            service=self._settings.app_name,
            version=self._settings.version,
            environment=self._settings.environment,
            uptime_seconds=self.uptime_seconds,
            stage=CURRENT_STAGE,
            components=components,
        )
