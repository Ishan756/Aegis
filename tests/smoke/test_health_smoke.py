"""Smoke tests against a running backend.

These verify the deployed surface rather than imported code, so they are skipped
when no server is listening. Run them with `make smoke`.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

import pytest

SMOKE_URL = os.environ.get("AEGIS_SMOKE_URL", "http://localhost:8000/api/v1").rstrip("/")


def _get(path: str, timeout: float = 5.0) -> dict:
    """GET ``path`` from the running API, skipping the test if unreachable."""
    url = f"{SMOKE_URL}{path}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as error:
        pytest.skip(f"Backend not reachable at {SMOKE_URL}: {error}")


def test_health_endpoint_is_ok() -> None:
    body = _get("/health")

    assert body["status"] == "ok"
    assert body["service"] == "Aegis Backend"
    assert "components" in body


def test_liveness_probe_is_ok() -> None:
    assert _get("/health/live") == {"alive": True}