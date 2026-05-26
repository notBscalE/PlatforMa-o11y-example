"""Shared fixtures for all agent tests."""

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

# Set required env vars before any module-level imports touch them
os.environ.setdefault("ANTHROPIC_API_KEY", "test-api-key")
os.environ.setdefault("GITHUB_TOKEN", "test-github-token")
os.environ.setdefault("GITHUB_REPO", "test-owner/test-repo")
os.environ.setdefault("PROMETHEUS_URL", "http://prometheus-test:9090")
os.environ.setdefault("ARGOCD_URL", "http://argocd-test:80")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("CLUSTER_NAME", "test-cluster")


async def _noop_poll():
    """Coroutine that sleeps forever — stands in for poll_issue_comments in tests."""
    while True:
        await asyncio.sleep(3600)


@pytest.fixture()
def mock_orchestrator():
    """Return a fully mocked Orchestrator instance."""
    mock = MagicMock()
    mock.handle_trigger = AsyncMock()
    mock.run_periodic_check = AsyncMock()
    mock.startup_recovery = AsyncMock()
    mock.poll_issue_comments = _noop_poll  # must return an actual coroutine
    mock._health_checker = MagicMock()
    mock._health_checker.check_all = AsyncMock(
        return_value=MagicMock(
            healthy=True,
            summary="All healthy",
            dict=lambda: {"healthy": True, "summary": "All healthy", "components": {}},
        )
    )
    return mock


@pytest.fixture()
def client(mock_orchestrator):
    """TestClient with the Orchestrator class patched before lifespan runs."""
    import main

    # Patch the class so lifespan's `_orchestrator = Orchestrator()` returns our mock
    with patch("main.Orchestrator", return_value=mock_orchestrator):
        with TestClient(main.app, raise_server_exceptions=True) as c:
            yield c
