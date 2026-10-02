"""Shared pytest fixtures for the backend test suite."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture
def client() -> Iterator[TestClient]:
    """Yield a test client with the full lifespan (startup/shutdown) exercised."""
    with TestClient(create_app()) as test_client:
        yield test_client
