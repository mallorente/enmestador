"""Shared test fixtures for the PKM ingestion pipeline."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def mock_x_auth_manager():
    """Keep orchestrator tests off the real X browser manager by default."""
    with patch("main.XAuthManager") as MockXAuth:
        mock_x_auth = AsyncMock()
        mock_context = MagicMock()
        mock_context.new_page = AsyncMock(return_value=MagicMock())
        mock_x_auth.context = mock_context
        mock_x_auth.ensure_browser = AsyncMock()
        mock_x_auth.close = AsyncMock()
        MockXAuth.return_value = mock_x_auth
        yield MockXAuth


@pytest.fixture
def tmp_state_dir(tmp_path: Path) -> Path:
    """Provide a temporary directory for state files."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    return state_dir


@pytest.fixture
def sample_bookmark_data() -> dict:
    """Return a valid bookmark payload for testing."""
    return {
        "source": "x",
        "url": "https://example.com/article",
        "title": "Test Article",
        "post_text": "Great insights on architecture",
        "saved_at": "2026-05-13T02:00:00Z",
    }
