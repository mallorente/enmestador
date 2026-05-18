"""Shared test fixtures for the PKM ingestion pipeline."""

from pathlib import Path

import pytest


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
