"""End-to-end integration tests for the PKM ingestion pipeline.

These tests verify the full pipeline flow with realistic fixture data,
including URL normalization, source folder organization, external article
extraction, and configuration from environment variables.
"""

import importlib
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from models import (
    Bookmark,
    EnrichedBookmark,
    Enrichment,
    ExtractedContent,
    Source,
)


def _make_bookmark(
    url: str = "https://example.com/article",
    source: Source = Source.X,
    title: str = "Test Article",
    post_text: str = "Some text",
    external_urls: list[str] | None = None,
) -> Bookmark:
    return Bookmark(
        source=source,
        url=url,
        title=title,
        post_text=post_text,
        external_urls=external_urls,
    )


def _make_enriched(bookmark: Bookmark, content: ExtractedContent | None = None) -> EnrichedBookmark:
    return EnrichedBookmark(
        bookmark=bookmark,
        content=content or ExtractedContent(
            url=bookmark.url,
            post_text=bookmark.post_text,
            extraction_method="post_only",
        ),
        enrichment=Enrichment(
            summary_bullets=["Bullet 1", "Bullet 2", "Bullet 3"],
            takeaway="Key takeaway",
            tags=["test", "e2e"],
            model_used="test-model",
            tokens=10,
        ),
    )


def _mock_auth():
    mock_auth = AsyncMock()
    mock_context = MagicMock()
    mock_context.new_page = AsyncMock(return_value=AsyncMock())
    mock_auth.context = mock_context
    mock_auth.ensure_browser = AsyncMock()
    mock_auth.close = AsyncMock()
    return mock_auth


def _mock_notifier():
    mock_notifier = AsyncMock()
    mock_notifier.send_summary = AsyncMock(return_value=True)
    mock_notifier.close = AsyncMock()
    return mock_notifier


@pytest.mark.asyncio
async def test_url_normalization_dedup_e2e(tmp_path: Path) -> None:
    """Bookmarks with tracking params are deduped via URL normalization."""
    state_dir = tmp_path / "state"
    output_dir = tmp_path / "output"
    user_data_dir = tmp_path / "user_data"
    state_dir.mkdir()

    processed_file = state_dir / "processed_urls.json"
    processed_file.write_text(json.dumps({
        "urls": {
            "https://example.com/article": {
                "first_seen": "2026-05-14T00:00:00Z",
                "source": "x",
            }
        }
    }))

    bm_variant = _make_bookmark(url="https://example.com/article?utm_source=twitter")

    with (
        patch("main.DEFAULT_STATE_DIR", str(state_dir)),
        patch("main.DEFAULT_OUTPUT_DIR", str(output_dir)),
        patch("main.DEFAULT_USER_DATA_DIR", str(user_data_dir)),
        patch("main.AuthManager") as MockAuth,
    ):
        MockAuth.return_value = _mock_auth()
        with (
            patch("main.ScraperX") as MockScraperX,
            patch("main.ScraperLinkedIn") as MockScraperLI,
            patch("main.LLMProcessor"),
            patch("main.web_extract", return_value=None),
            patch("main.Notifier") as MockNotifier,
        ):
            MockScraperX.return_value.scrape = AsyncMock(return_value=[bm_variant])
            MockScraperLI.return_value.scrape = AsyncMock(return_value=[])
            MockNotifier.return_value = _mock_notifier()

            from main import run_pipeline

            result = await run_pipeline()

    assert result.processed == 0
    assert result.enriched == 0


@pytest.mark.asyncio
async def test_output_organized_by_source_e2e(tmp_path: Path) -> None:
    """X bookmarks go to x/ subfolder, LinkedIn bookmarks go to linkedin/ subfolder."""
    state_dir = tmp_path / "state"
    output_dir = tmp_path / "output"
    user_data_dir = tmp_path / "user_data"
    state_dir.mkdir()

    bm_x = _make_bookmark(
        url="https://x.com/user/status/100", source=Source.X, title="X Post Title"
    )
    bm_li = _make_bookmark(
        url="https://linkedin.com/posts/john-456",
        source=Source.LINKEDIN,
        title="LinkedIn Post Title",
    )

    def enrich_side_effect(bookmark, content=None):
        return _make_enriched(bookmark, content)

    with (
        patch("main.DEFAULT_STATE_DIR", str(state_dir)),
        patch("main.DEFAULT_OUTPUT_DIR", str(output_dir)),
        patch("main.DEFAULT_USER_DATA_DIR", str(user_data_dir)),
        patch("main.AuthManager") as MockAuth,
    ):
        MockAuth.return_value = _mock_auth()
        with (
            patch("main.ScraperX") as MockScraperX,
            patch("main.ScraperLinkedIn") as MockScraperLI,
            patch("main.LLMProcessor") as MockLLM,
            patch("main.web_extract", return_value=None),
            patch("main.Notifier") as MockNotifier,
        ):
            MockScraperX.return_value.scrape = AsyncMock(return_value=[bm_x])
            MockScraperLI.return_value.scrape = AsyncMock(return_value=[bm_li])
            mock_llm = AsyncMock()
            mock_llm.enrich = AsyncMock(side_effect=enrich_side_effect)
            MockLLM.return_value = mock_llm
            MockNotifier.return_value = _mock_notifier()

            from main import run_pipeline

            result = await run_pipeline()

    assert result.processed == 2
    assert result.enriched == 2

    x_files = list((output_dir / "x").glob("*.md"))
    li_files = list((output_dir / "linkedin").glob("*.md"))
    assert len(x_files) == 1
    assert len(li_files) == 1


@pytest.mark.asyncio
async def test_external_article_extraction_e2e(tmp_path: Path) -> None:
    """X bookmarks with external_urls produce Article sections; LinkedIn web_extract produces Article Text."""
    state_dir = tmp_path / "state"
    output_dir = tmp_path / "output"
    user_data_dir = tmp_path / "user_data"
    state_dir.mkdir()

    bm_x = Bookmark(
        source=Source.X,
        url="https://x.com/user/status/999",
        title="X Post with External Link",
        post_text="Read this!",
        external_urls=["https://example.com/external-article"],
    )
    bm_li = Bookmark(
        source=Source.LINKEDIN,
        url="https://linkedin.com/posts/jane-789",
        title="LinkedIn Article",
        post_text="Interesting take on AI",
    )

    # full_text must be ≥200 chars to pass the pipeline's external-article length filter
    _LONG_TEXT = "A" * 210
    ext_content_x = ExtractedContent(
        url="https://example.com/external-article",
        full_text=f"Full text of the external article about Python. {_LONG_TEXT}",
        extraction_method="trafilatura",
    )
    li_content = ExtractedContent(
        url="https://linkedin.com/posts/jane-789",
        full_text=f"Full article content about AI trends. {_LONG_TEXT}",
        post_text="Interesting take on AI",
        extraction_method="trafilatura",
    )

    def enrich_side_effect(bookmark, content=None):
        return _make_enriched(bookmark, content)

    with (
        patch("main.DEFAULT_STATE_DIR", str(state_dir)),
        patch("main.DEFAULT_OUTPUT_DIR", str(output_dir)),
        patch("main.DEFAULT_USER_DATA_DIR", str(user_data_dir)),
        patch("main.AuthManager") as MockAuth,
    ):
        MockAuth.return_value = _mock_auth()
        with (
            patch("main.ScraperX") as MockScraperX,
            patch("main.ScraperLinkedIn") as MockScraperLI,
            patch("main.LLMProcessor") as MockLLM,
            patch("main.web_extract", new_callable=AsyncMock) as mock_extract,
            patch("main.extract_with_playwright", new_callable=AsyncMock, return_value=None),
            patch("main.extract_linkedin_post", new_callable=AsyncMock, return_value=(None, [])),
            patch("main.extract_x_thread", new_callable=AsyncMock, return_value=None),
            patch("main.Notifier") as MockNotifier,
        ):
            MockScraperX.return_value.scrape = AsyncMock(return_value=[bm_x])
            MockScraperLI.return_value.scrape = AsyncMock(return_value=[bm_li])
            mock_llm = AsyncMock()
            mock_llm.enrich = AsyncMock(side_effect=enrich_side_effect)
            MockLLM.return_value = mock_llm

            def extract_lookup(url, post_text=None):
                if "external-article" in url:
                    return ext_content_x
                if "linkedin" in url:
                    return li_content
                return None
            mock_extract.side_effect = extract_lookup
            MockNotifier.return_value = _mock_notifier()

            from main import run_pipeline

            result = await run_pipeline()

    assert result.processed == 2
    assert result.enriched == 2

    x_files = list((output_dir / "x").glob("*.md"))
    li_files = list((output_dir / "linkedin").glob("*.md"))
    assert len(x_files) == 1
    assert len(li_files) == 1

    x_md = x_files[0].read_text(encoding="utf-8")
    assert "## Article: https://example.com/external-article" in x_md
    assert "Full text of the external article about Python" in x_md

    li_md = li_files[0].read_text(encoding="utf-8")
    assert "## Article Text" in li_md
    assert "Full article content about AI trends" in li_md


def test_config_env_vars_e2e():
    """Config module reads values from environment variables correctly."""
    import config

    with patch.dict("os.environ", {
        "MAX_BOOKMARKS": "100",
        "FETCH_TIMEOUT": "15.0",
        "LLM_RATE_LIMIT_SEC": "2.0",
    }):
        importlib.reload(config)
        assert config.MAX_BOOKMARKS_PER_SOURCE == 100
        assert config.FETCH_TIMEOUT == 15.0
        assert config.LLM_RATE_LIMIT_SEC == 2.0

    importlib.reload(config)


def test_healthcheck_healthy(monkeypatch, tmp_path):
    """Healthcheck exits 0 when all dirs exist and no lock file."""
    import healthcheck

    state_dir = tmp_path / "state"
    output_dir = tmp_path / "output"
    user_data_dir = tmp_path / "user_data"
    for d in [state_dir, output_dir, user_data_dir]:
        d.mkdir()

    path_map = {
        "/app/volumes/state": state_dir,
        "/app/volumes/llm_wiki_seed/Bookmarks/bookmarks": output_dir,
        "/app/volumes/user_data": user_data_dir,
    }

    def fake_path(path_str):
        return path_map.get(path_str, Path(path_str))

    monkeypatch.setattr(healthcheck, "Path", fake_path)

    with pytest.raises(SystemExit) as exc_info:
        healthcheck.healthcheck()
    assert exc_info.value.code == 0


def test_healthcheck_missing_dir(monkeypatch, tmp_path):
    """Healthcheck exits 1 when a required directory is missing."""
    import healthcheck

    state_dir = tmp_path / "state"
    output_dir = tmp_path / "output"
    state_dir.mkdir()
    output_dir.mkdir()

    path_map = {
        "/app/volumes/state": state_dir,
        "/app/volumes/llm_wiki_seed/Bookmarks/bookmarks": output_dir,
        "/app/volumes/user_data": tmp_path / "user_data",
    }

    def fake_path(path_str):
        return path_map.get(path_str, Path(path_str))

    monkeypatch.setattr(healthcheck, "Path", fake_path)

    with pytest.raises(SystemExit) as exc_info:
        healthcheck.healthcheck()
    assert exc_info.value.code == 1


def test_healthcheck_locked(monkeypatch, tmp_path):
    """Healthcheck exits 1 when lock file exists."""
    import healthcheck

    state_dir = tmp_path / "state"
    output_dir = tmp_path / "output"
    user_data_dir = tmp_path / "user_data"
    for d in [state_dir, output_dir, user_data_dir]:
        d.mkdir()

    lock_file = state_dir / "pipeline.lock"
    lock_file.write_text(json.dumps({"pid": 999}))

    path_map = {
        "/app/volumes/state": state_dir,
        "/app/volumes/llm_wiki_seed/Bookmarks/bookmarks": output_dir,
        "/app/volumes/user_data": user_data_dir,
    }

    def fake_path(path_str):
        return path_map.get(path_str, Path(path_str))

    monkeypatch.setattr(healthcheck, "Path", fake_path)

    with pytest.raises(SystemExit) as exc_info:
        healthcheck.healthcheck()
    assert exc_info.value.code == 1
