"""Integration tests for the main orchestrator module.

All components are mocked to test the orchestration logic without
requiring real browsers, LLM APIs, or network calls.
"""

import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from models import Bookmark, EnrichedBookmark, Enrichment, ExternalArticle, ExtractedContent, PipelineResult, ScrapeMode, Source
from state import DeadLetter, LockFile


def _make_bookmark(url: str = "https://example.com/article", source: Source = Source.X) -> Bookmark:
    """Create a minimal valid Bookmark for tests."""
    return Bookmark(
        source=source,
        url=url,
        title="Test Article",
        post_text="Some post text",
    )


# --- Lock file prevents concurrent run ---


@pytest.mark.asyncio
async def test_lock_prevents_concurrent_run(tmp_path: Path) -> None:
    """When lock file exists and is fresh, pipeline exits immediately."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    lock = LockFile(state_dir)
    assert lock.acquire()  # First acquire succeeds

    with patch("main.DEFAULT_STATE_DIR", str(state_dir)):
        with patch("main.DEFAULT_OUTPUT_DIR", str(tmp_path / "output")):
            with patch("main.DEFAULT_USER_DATA_DIR", str(tmp_path / "user_data")):
                from main import run_pipeline

                result = await run_pipeline()

    assert result.processed == 0
    assert result.enriched == 0
    assert result.dead_letter == 0


@pytest.mark.asyncio
async def test_stale_lock_is_overwritten(tmp_path: Path) -> None:
    """When lock file is older than 4 hours, it is treated as stale."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    lock_file = state_dir / "pipeline.lock"
    lock_file.write_text(json.dumps({"pid": 999}))
    # Make the file look old by modifying its mtime
    old_time = time.time() - (5 * 60 * 60)  # 5 hours ago
    import os
    os.utime(lock_file, (old_time, old_time))

    with patch("main.DEFAULT_STATE_DIR", str(state_dir)):
        with patch("main.DEFAULT_OUTPUT_DIR", str(tmp_path / "output")):
            with patch("main.DEFAULT_USER_DATA_DIR", str(tmp_path / "user_data")):
                with patch("main.AuthManager") as MockAuth:
                    mock_auth = AsyncMock()
                    mock_context = MagicMock()
                    mock_context.new_page = AsyncMock(return_value=MagicMock())
                    mock_auth.context = mock_context
                    mock_auth.ensure_browser = AsyncMock()
                    mock_auth.close = AsyncMock()
                    MockAuth.return_value = mock_auth

                    with patch("main.ScraperX") as MockScraperX:
                        MockScraperX.return_value.scrape = AsyncMock(return_value=[])
                        with patch("main.ScraperLinkedIn") as MockScraperLI:
                            MockScraperLI.return_value.scrape = AsyncMock(return_value=[])
                            with patch("main.LLMProcessor"):
                                with patch("main.Notifier") as MockNotifier:
                                    mock_notifier = AsyncMock()
                                    mock_notifier.send_summary = AsyncMock(return_value=True)
                                    mock_notifier.close = AsyncMock()
                                    MockNotifier.return_value = mock_notifier

                                    from main import run_pipeline

                                    result = await run_pipeline()

    # Should have proceeded past the stale lock
    mock_auth.ensure_browser.assert_awaited_once()
    # Lock file should be gone after release
    assert not lock_file.exists()


# --- Full pipeline with mocks ---


@pytest.mark.asyncio
async def test_full_pipeline_with_mocks(tmp_path: Path) -> None:
    """End-to-end pipeline with all components mocked."""
    state_dir = tmp_path / "state"
    output_dir = tmp_path / "output"
    user_data_dir = tmp_path / "user_data"
    state_dir.mkdir()

    bm = _make_bookmark()

    with patch("main.DEFAULT_STATE_DIR", str(state_dir)):
        with patch("main.DEFAULT_OUTPUT_DIR", str(output_dir)):
            with patch("main.DEFAULT_USER_DATA_DIR", str(user_data_dir)):
                with patch("main.AuthManager") as MockAuth:
                    mock_auth = AsyncMock()
                    mock_context = MagicMock()
                    mock_context.new_page = AsyncMock(return_value=MagicMock())
                    mock_auth.context = mock_context
                    mock_auth.ensure_browser = AsyncMock()
                    mock_auth.close = AsyncMock()
                    MockAuth.return_value = mock_auth

                    with patch("main.ScraperX") as MockScraperX:
                        MockScraperX.return_value.scrape = AsyncMock(return_value=[bm])
                        with patch("main.ScraperLinkedIn") as MockScraperLI:
                            MockScraperLI.return_value.scrape = AsyncMock(return_value=[])
                            with patch("main.LLMProcessor") as MockLLM:
                                mock_llm = AsyncMock()
                                mock_llm.enrich = AsyncMock()
                                # Return a valid enriched bookmark
                                from models import EnrichedBookmark, Enrichment, ExtractedContent
                                from datetime import UTC, datetime
                                mock_llm.enrich.return_value = EnrichedBookmark(
                                    bookmark=bm,
                                    content=ExtractedContent(
                                        url=bm.url,
                                        post_text=bm.post_text,
                                        extraction_method="post_only",
                                    ),
                                    enrichment=Enrichment(
                                        summary_bullets=["bullet 1", "bullet 2", "bullet 3"],
                                        takeaway="Key takeaway",
                                        tags=["test", "pipeline"],
                                        model_used="test-model",
                                        tokens=10,
                                    ),
                                )
                                MockLLM.return_value = mock_llm

                                with patch("main.web_extract") as mock_extract:
                                    mock_extract.return_value = None

                                    with patch("main.Notifier") as MockNotifier:
                                        mock_notifier = AsyncMock()
                                        mock_notifier.send_summary = AsyncMock(return_value=True)
                                        mock_notifier.close = AsyncMock()
                                        MockNotifier.return_value = mock_notifier

                                        from main import run_pipeline

                                        result = await run_pipeline()

    assert result.processed == 1
    assert result.enriched == 1
    assert result.dead_letter == 0

    # Verify auth lifecycle
    mock_auth.ensure_browser.assert_awaited_once()
    mock_auth.close.assert_awaited_once()

    # Verify scrapers were called
    MockScraperX.return_value.scrape.assert_awaited_once()
    MockScraperLI.return_value.scrape.assert_awaited_once()

    # Verify summary was sent
    mock_notifier.send_summary.assert_awaited_once()

    # Verify processed URL was recorded
    processed_file = state_dir / "processed_urls.json"
    assert processed_file.exists()
    data = json.loads(processed_file.read_text())
    assert str(bm.url) in data.get("urls", {})


# --- Single bookmark failure doesn't abort pipeline ---


@pytest.mark.asyncio
async def test_single_failure_survives(tmp_path: Path) -> None:
    """When one bookmark fails, the pipeline continues with the rest."""
    state_dir = tmp_path / "state"
    output_dir = tmp_path / "output"
    user_data_dir = tmp_path / "user_data"
    state_dir.mkdir()

    bm_good = _make_bookmark(url="https://example.com/good")
    bm_bad = _make_bookmark(url="https://example.com/bad")

    with patch("main.DEFAULT_STATE_DIR", str(state_dir)):
        with patch("main.DEFAULT_OUTPUT_DIR", str(output_dir)):
            with patch("main.DEFAULT_USER_DATA_DIR", str(user_data_dir)):
                with patch("main.AuthManager") as MockAuth:
                    mock_auth = AsyncMock()
                    mock_context = MagicMock()
                    mock_context.new_page = AsyncMock(return_value=MagicMock())
                    mock_auth.context = mock_context
                    mock_auth.ensure_browser = AsyncMock()
                    mock_auth.close = AsyncMock()
                    MockAuth.return_value = mock_auth

                    with patch("main.ScraperX") as MockScraperX:
                        MockScraperX.return_value.scrape = AsyncMock(return_value=[bm_good, bm_bad])
                        with patch("main.ScraperLinkedIn") as MockScraperLI:
                            MockScraperLI.return_value.scrape = AsyncMock(return_value=[])
                            with patch("main.LLMProcessor") as MockLLM:
                                mock_llm = AsyncMock()

                                from models import EnrichedBookmark, Enrichment, ExtractedContent
                                from llm_processor import EnrichmentError

                                def enrich_side_effect(bookmark, content=None):
                                    if "bad" in str(bookmark.url):
                                        raise EnrichmentError("LLM failed for bad bookmark")
                                    return EnrichedBookmark(
                                        bookmark=bookmark,
                                        content=ExtractedContent(
                                            url=bookmark.url,
                                            post_text=bookmark.post_text,
                                            extraction_method="post_only",
                                        ),
                                        enrichment=Enrichment(
                                            summary_bullets=["a", "b", "c"],
                                            takeaway="Takeaway",
                                            tags=["tag"],
                                            model_used="test",
                                            tokens=5,
                                        ),
                                    )

                                mock_llm.enrich = AsyncMock(side_effect=enrich_side_effect)
                                MockLLM.return_value = mock_llm

                                with patch("main.web_extract", return_value=None):
                                    with patch("main.Notifier") as MockNotifier:
                                        mock_notifier = AsyncMock()
                                        mock_notifier.send_summary = AsyncMock(return_value=True)
                                        mock_notifier.close = AsyncMock()
                                        MockNotifier.return_value = mock_notifier

                                        from main import run_pipeline

                                        result = await run_pipeline()

    # Both bookmarks should have been attempted
    assert result.processed == 2
    # The good one was enriched, the bad one was dead-lettered but still "processed"
    assert result.enriched == 1
    # The bad one should be in dead letter
    assert result.dead_letter >= 1

    # Verify dead letter file was written
    dead_letter_file = state_dir / "dead_letter.jsonl"
    assert dead_letter_file.exists()
    lines = dead_letter_file.read_text().strip().splitlines()
    assert len(lines) >= 1
    dl = json.loads(lines[0])
    assert dl["error"] == "LLM failed for bad bookmark"
    assert dl["stage"] == "llm"


@pytest.mark.asyncio
async def test_single_failure_sends_bookmark_error_notification(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    output_dir = tmp_path / "output"
    user_data_dir = tmp_path / "user_data"
    state_dir.mkdir()

    bm_bad = _make_bookmark(url="https://example.com/bad")

    with patch("main.DEFAULT_STATE_DIR", str(state_dir)):
        with patch("main.DEFAULT_OUTPUT_DIR", str(output_dir)):
            with patch("main.DEFAULT_USER_DATA_DIR", str(user_data_dir)):
                with patch("main.AuthManager") as MockAuth:
                    mock_auth = AsyncMock()
                    mock_context = MagicMock()
                    mock_context.new_page = AsyncMock(return_value=MagicMock())
                    mock_auth.context = mock_context
                    mock_auth.ensure_browser = AsyncMock()
                    mock_auth.close = AsyncMock()
                    MockAuth.return_value = mock_auth

                    with patch("main.ScraperX") as MockScraperX:
                        MockScraperX.return_value.scrape = AsyncMock(return_value=[bm_bad])
                        with patch("main.ScraperLinkedIn") as MockScraperLI:
                            MockScraperLI.return_value.scrape = AsyncMock(return_value=[])
                            with patch("main.LLMProcessor") as MockLLM:
                                from llm_processor import EnrichmentError

                                mock_llm = AsyncMock()
                                mock_llm.enrich = AsyncMock(
                                    side_effect=EnrichmentError("LLM boom")
                                )
                                MockLLM.return_value = mock_llm

                                with patch("main.web_extract", return_value=None):
                                    with patch("main.Notifier") as MockNotifier:
                                        mock_notifier = AsyncMock()
                                        mock_notifier.send_summary = AsyncMock(return_value=True)
                                        mock_notifier.send_bookmark_error = AsyncMock(return_value=True)
                                        mock_notifier.close = AsyncMock()
                                        MockNotifier.return_value = mock_notifier

                                        from main import run_pipeline

                                        await run_pipeline()

    mock_notifier.send_bookmark_error.assert_awaited()
    call_args = mock_notifier.send_bookmark_error.call_args
    assert call_args[0][0] == "x"
    assert call_args[0][1] == "https://example.com/bad"
    assert call_args[0][2] == "llm"
    assert call_args[0][3] == "LLM boom"


@pytest.mark.asyncio
async def test_bootstrap_mode_when_no_cursor(tmp_path: Path) -> None:
    """When no cursor exists, scrapers run in bootstrap mode."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()

    with patch("main.DEFAULT_STATE_DIR", str(state_dir)):
        with patch("main.DEFAULT_OUTPUT_DIR", str(tmp_path / "output")):
            with patch("main.DEFAULT_USER_DATA_DIR", str(tmp_path / "user_data")):
                with patch("main.AuthManager") as MockAuth:
                    mock_auth = AsyncMock()
                    mock_context = MagicMock()
                    mock_context.new_page = AsyncMock(return_value=MagicMock())
                    mock_auth.context = mock_context
                    mock_auth.ensure_browser = AsyncMock()
                    mock_auth.close = AsyncMock()
                    MockAuth.return_value = mock_auth

                    with patch("main.ScraperX") as MockScraperX:
                        MockScraperX.return_value.scrape = AsyncMock(return_value=[])
                        with patch("main.ScraperLinkedIn") as MockScraperLI:
                            MockScraperLI.return_value.scrape = AsyncMock(return_value=[])
                            with patch("main.LLMProcessor"):
                                with patch("main.web_extract", return_value=None):
                                    with patch("main.Notifier") as MockNotifier:
                                        mock_notifier = AsyncMock()
                                        mock_notifier.send_summary = AsyncMock(return_value=True)
                                        mock_notifier.close = AsyncMock()
                                        MockNotifier.return_value = mock_notifier

                                        from main import run_pipeline

                                        await run_pipeline()

    # Verify scrapers were called with BOOTSTRAP mode
    call_args_x = MockScraperX.return_value.scrape.call_args
    assert call_args_x[0][0] == ScrapeMode.BOOTSTRAP

    call_args_li = MockScraperLI.return_value.scrape.call_args
    assert call_args_li[0][0] == ScrapeMode.BOOTSTRAP


@pytest.mark.asyncio
async def test_delta_mode_when_cursor_exists(tmp_path: Path) -> None:
    """When cursor file exists with a cursor, scrapers run in delta mode."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    cursors_file = state_dir / "cursors.json"
    cursors_file.write_text(json.dumps({
        "x": {"cursor": "abc123==", "mode": "delta", "last_run": "2026-05-13T02:00:00Z"},
        "linkedin": {"cursor": "xyz789==", "mode": "delta", "last_run": "2026-05-13T02:00:00Z"},
    }))

    with patch("main.DEFAULT_STATE_DIR", str(state_dir)):
        with patch("main.DEFAULT_OUTPUT_DIR", str(tmp_path / "output")):
            with patch("main.DEFAULT_USER_DATA_DIR", str(tmp_path / "user_data")):
                with patch("main.AuthManager") as MockAuth:
                    mock_auth = AsyncMock()
                    mock_context = MagicMock()
                    mock_context.new_page = AsyncMock(return_value=MagicMock())
                    mock_auth.context = mock_context
                    mock_auth.ensure_browser = AsyncMock()
                    mock_auth.close = AsyncMock()
                    MockAuth.return_value = mock_auth

                    with patch("main.ScraperX") as MockScraperX:
                        MockScraperX.return_value.scrape = AsyncMock(return_value=[])
                        with patch("main.ScraperLinkedIn") as MockScraperLI:
                            MockScraperLI.return_value.scrape = AsyncMock(return_value=[])
                            with patch("main.LLMProcessor"):
                                with patch("main.web_extract", return_value=None):
                                    with patch("main.Notifier") as MockNotifier:
                                        mock_notifier = AsyncMock()
                                        mock_notifier.send_summary = AsyncMock(return_value=True)
                                        mock_notifier.close = AsyncMock()
                                        MockNotifier.return_value = mock_notifier

                                        from main import run_pipeline

                                        await run_pipeline()

    # Verify scrapers were called with DELTA mode
    call_args_x = MockScraperX.return_value.scrape.call_args
    assert call_args_x[0][0] == ScrapeMode.DELTA

    call_args_li = MockScraperLI.return_value.scrape.call_args
    assert call_args_li[0][0] == ScrapeMode.DELTA


# --- Empty bookmarks (no-op) ---


@pytest.mark.asyncio
async def test_empty_bookmarks_noop(tmp_path: Path) -> None:
    """When both scrapers return empty lists, pipeline completes without errors."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()

    with patch("main.DEFAULT_STATE_DIR", str(state_dir)):
        with patch("main.DEFAULT_OUTPUT_DIR", str(tmp_path / "output")):
            with patch("main.DEFAULT_USER_DATA_DIR", str(tmp_path / "user_data")):
                with patch("main.AuthManager") as MockAuth:
                    mock_auth = AsyncMock()
                    mock_context = MagicMock()
                    mock_context.new_page = AsyncMock(return_value=MagicMock())
                    mock_auth.context = mock_context
                    mock_auth.ensure_browser = AsyncMock()
                    mock_auth.close = AsyncMock()
                    MockAuth.return_value = mock_auth

                    with patch("main.ScraperX") as MockScraperX:
                        MockScraperX.return_value.scrape = AsyncMock(return_value=[])
                        with patch("main.ScraperLinkedIn") as MockScraperLI:
                            MockScraperLI.return_value.scrape = AsyncMock(return_value=[])
                            with patch("main.LLMProcessor"):
                                with patch("main.web_extract", return_value=None):
                                    with patch("main.Notifier") as MockNotifier:
                                        mock_notifier = AsyncMock()
                                        mock_notifier.send_summary = AsyncMock(return_value=True)
                                        mock_notifier.close = AsyncMock()
                                        MockNotifier.return_value = mock_notifier

                                        from main import run_pipeline

                                        result = await run_pipeline()

    assert result.processed == 0
    assert result.enriched == 0
    assert result.dead_letter == 0
    mock_auth.close.assert_awaited_once()


# --- Dead letter accumulation ---


@pytest.mark.asyncio
async def test_dead_letter_accumulation(tmp_path: Path) -> None:
    """Multiple failures accumulate in dead letter file."""
    state_dir = tmp_path / "state"
    output_dir = tmp_path / "output"
    user_data_dir = tmp_path / "user_data"
    state_dir.mkdir()

    bms = [
        _make_bookmark(url=f"https://example.com/bad-{i}")
        for i in range(3)
    ]

    with patch("main.DEFAULT_STATE_DIR", str(state_dir)):
        with patch("main.DEFAULT_OUTPUT_DIR", str(output_dir)):
            with patch("main.DEFAULT_USER_DATA_DIR", str(user_data_dir)):
                with patch("main.AuthManager") as MockAuth:
                    mock_auth = AsyncMock()
                    mock_context = MagicMock()
                    mock_context.new_page = AsyncMock(return_value=MagicMock())
                    mock_auth.context = mock_context
                    mock_auth.ensure_browser = AsyncMock()
                    mock_auth.close = AsyncMock()
                    MockAuth.return_value = mock_auth

                    with patch("main.ScraperX") as MockScraperX:
                        MockScraperX.return_value.scrape = AsyncMock(return_value=bms)
                        with patch("main.ScraperLinkedIn") as MockScraperLI:
                            MockScraperLI.return_value.scrape = AsyncMock(return_value=[])
                            with patch("main.LLMProcessor") as MockLLM:
                                from llm_processor import EnrichmentError
                                mock_llm = AsyncMock()
                                mock_llm.enrich = AsyncMock(
                                    side_effect=EnrichmentError("All models failed")
                                )
                                MockLLM.return_value = mock_llm

                                with patch("main.web_extract", return_value=None):
                                    with patch("main.Notifier") as MockNotifier:
                                        mock_notifier = AsyncMock()
                                        mock_notifier.send_summary = AsyncMock(return_value=True)
                                        mock_notifier.close = AsyncMock()
                                        MockNotifier.return_value = mock_notifier

                                        from main import run_pipeline

                                        result = await run_pipeline()

    assert result.processed == 3
    assert result.enriched == 0
    assert result.dead_letter == 3

    # Verify all 3 dead letters are in the file
    dead_letter_file = state_dir / "dead_letter.jsonl"
    lines = dead_letter_file.read_text().strip().splitlines()
    assert len(lines) == 3


# --- Summary notification content ---


@pytest.mark.asyncio
async def test_summary_notification_content(tmp_path: Path) -> None:
    """Verify the summary notification includes correct stats."""
    state_dir = tmp_path / "state"
    output_dir = tmp_path / "output"
    user_data_dir = tmp_path / "user_data"
    state_dir.mkdir()

    bm = _make_bookmark()

    with patch("main.DEFAULT_STATE_DIR", str(state_dir)):
        with patch("main.DEFAULT_OUTPUT_DIR", str(output_dir)):
            with patch("main.DEFAULT_USER_DATA_DIR", str(user_data_dir)):
                with patch("main.AuthManager") as MockAuth:
                    mock_auth = AsyncMock()
                    mock_context = MagicMock()
                    mock_context.new_page = AsyncMock(return_value=MagicMock())
                    mock_auth.context = mock_context
                    mock_auth.ensure_browser = AsyncMock()
                    mock_auth.close = AsyncMock()
                    MockAuth.return_value = mock_auth

                    with patch("main.ScraperX") as MockScraperX:
                        MockScraperX.return_value.scrape = AsyncMock(return_value=[bm])
                        with patch("main.ScraperLinkedIn") as MockScraperLI:
                            MockScraperLI.return_value.scrape = AsyncMock(return_value=[])
                            with patch("main.LLMProcessor") as MockLLM:
                                mock_llm = AsyncMock()
                                from models import EnrichedBookmark, Enrichment, ExtractedContent
                                mock_llm.enrich = AsyncMock(return_value=EnrichedBookmark(
                                    bookmark=bm,
                                    content=ExtractedContent(
                                        url=bm.url,
                                        post_text=bm.post_text,
                                        extraction_method="post_only",
                                    ),
                                    enrichment=Enrichment(
                                        summary_bullets=["a", "b", "c"],
                                        takeaway="t",
                                        tags=["tag"],
                                        model_used="test",
                                        tokens=5,
                                    ),
                                ))
                                MockLLM.return_value = mock_llm

                                with patch("main.web_extract", return_value=None):
                                    with patch("main.Notifier") as MockNotifier:
                                        mock_notifier = AsyncMock()
                                        mock_notifier.send_summary = AsyncMock(return_value=True)
                                        mock_notifier.close = AsyncMock()
                                        MockNotifier.return_value = mock_notifier

                                        from main import run_pipeline

                                        await run_pipeline()

    # Verify send_summary was called with a PipelineResult
    call_args = mock_notifier.send_summary.call_args
    assert call_args is not None
    result_arg = call_args[0][0]
    assert isinstance(result_arg, PipelineResult)
    assert result_arg.processed == 1
    assert result_arg.enriched == 1


# --- Lock is released on crash ---


@pytest.mark.asyncio
async def test_lock_released_on_crash(tmp_path: Path) -> None:
    """When the pipeline crashes, the lock file is still released."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()

    with patch("main.DEFAULT_STATE_DIR", str(state_dir)):
        with patch("main.DEFAULT_OUTPUT_DIR", str(tmp_path / "output")):
            with patch("main.DEFAULT_USER_DATA_DIR", str(tmp_path / "user_data")):
                with patch("main.AuthManager") as MockAuth:
                    mock_auth = AsyncMock()
                    mock_auth.ensure_browser = AsyncMock(
                        side_effect=RuntimeError("Browser crashed")
                    )
                    mock_auth.close = AsyncMock()
                    MockAuth.return_value = mock_auth

                    with patch("main.Notifier") as MockNotifier:
                        mock_notifier = AsyncMock()
                        mock_notifier.send = AsyncMock(return_value=True)
                        mock_notifier.send_summary = AsyncMock(return_value=True)
                        mock_notifier.close = AsyncMock()
                        MockNotifier.return_value = mock_notifier

                        from main import run_pipeline

                        with pytest.raises(RuntimeError, match="Browser crashed"):
                            await run_pipeline()

    # Lock file should be released even after crash
    lock_file = state_dir / "pipeline.lock"
    assert not lock_file.exists()

    # Auth close should still be called
    mock_auth.close.assert_awaited_once()


# --- Deduplication skips already-processed URLs ---


@pytest.mark.asyncio
async def test_dedup_skips_processed_urls(tmp_path: Path) -> None:
    """Bookmarks whose URLs are already in processed_urls.json are skipped."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    processed_file = state_dir / "processed_urls.json"
    processed_file.write_text(json.dumps({
        "urls": {
            "https://example.com/article": {
                "first_seen": "2026-05-10T00:00:00Z",
                "source": "x",
            }
        }
    }))

    bm = _make_bookmark()  # Same URL as the processed one

    with patch("main.DEFAULT_STATE_DIR", str(state_dir)):
        with patch("main.DEFAULT_OUTPUT_DIR", str(tmp_path / "output")):
            with patch("main.DEFAULT_USER_DATA_DIR", str(tmp_path / "user_data")):
                with patch("main.AuthManager") as MockAuth:
                    mock_auth = AsyncMock()
                    mock_context = MagicMock()
                    mock_context.new_page = AsyncMock(return_value=MagicMock())
                    mock_auth.context = mock_context
                    mock_auth.ensure_browser = AsyncMock()
                    mock_auth.close = AsyncMock()
                    MockAuth.return_value = mock_auth

                    with patch("main.ScraperX") as MockScraperX:
                        MockScraperX.return_value.scrape = AsyncMock(return_value=[bm])
                        with patch("main.ScraperLinkedIn") as MockScraperLI:
                            MockScraperLI.return_value.scrape = AsyncMock(return_value=[])
                            with patch("main.LLMProcessor"):
                                with patch("main.web_extract", return_value=None):
                                    with patch("main.Notifier") as MockNotifier:
                                        mock_notifier = AsyncMock()
                                        mock_notifier.send_summary = AsyncMock(return_value=True)
                                        mock_notifier.close = AsyncMock()
                                        MockNotifier.return_value = mock_notifier

                                        from main import run_pipeline

                                        result = await run_pipeline()

    # The bookmark should have been skipped (deduplicated)
    assert result.processed == 0
    assert result.enriched == 0


# --- External article extraction (web clipper for X bookmarks) ---


@pytest.mark.asyncio
async def test_x_bookmark_with_external_urls_extracts_articles(tmp_path: Path) -> None:
    """X bookmarks with external_urls call web_extract for each URL."""
    state_dir = tmp_path / "state"
    output_dir = tmp_path / "output"
    user_data_dir = tmp_path / "user_data"
    state_dir.mkdir()

    bm = Bookmark(
        source=Source.X,
        url="https://x.com/user/status/123",
        title="Great article about Python",
        post_text="Check out this article!",
        external_urls=["https://example.com/article1", "https://example.com/article2"],
    )

    extracted_content_1 = ExtractedContent(
        url="https://example.com/article1",
        full_text="Article 1 full text content about Python.",
        extraction_method="trafilatura",
    )
    extracted_content_2 = ExtractedContent(
        url="https://example.com/article2",
        full_text="Article 2 full text content about testing.",
        extraction_method="trafilatura",
    )

    with patch("main.DEFAULT_STATE_DIR", str(state_dir)):
        with patch("main.DEFAULT_OUTPUT_DIR", str(output_dir)):
            with patch("main.DEFAULT_USER_DATA_DIR", str(user_data_dir)):
                with patch("main.AuthManager") as MockAuth:
                    mock_auth = AsyncMock()
                    mock_context = MagicMock()
                    mock_context.new_page = AsyncMock(return_value=MagicMock())
                    mock_auth.context = mock_context
                    mock_auth.ensure_browser = AsyncMock()
                    mock_auth.close = AsyncMock()
                    MockAuth.return_value = mock_auth

                    with patch("main.ScraperX") as MockScraperX:
                        MockScraperX.return_value.scrape = AsyncMock(return_value=[bm])
                        with patch("main.ScraperLinkedIn") as MockScraperLI:
                            MockScraperLI.return_value.scrape = AsyncMock(return_value=[])
                            with patch("main.LLMProcessor") as MockLLM:
                                mock_llm = AsyncMock()

                                def enrich_side_effect(bookmark, content=None):
                                    return EnrichedBookmark(
                                        bookmark=bookmark,
                                        content=content or ExtractedContent(
                                            url=bookmark.url,
                                            post_text=bookmark.post_text,
                                            extraction_method="post_only",
                                        ),
                                        enrichment=Enrichment(
                                            summary_bullets=["a", "b", "c"],
                                            takeaway="t",
                                            tags=["python"],
                                            model_used="test",
                                            tokens=5,
                                        ),
                                    )

                                mock_llm.enrich = AsyncMock(side_effect=enrich_side_effect)
                                MockLLM.return_value = mock_llm

                                with patch("main.web_extract", new_callable=AsyncMock) as mock_extract:
                                    mock_extract.side_effect = [extracted_content_1, extracted_content_2]

                                    with patch("main.Notifier") as MockNotifier:
                                        mock_notifier = AsyncMock()
                                        mock_notifier.send_summary = AsyncMock(return_value=True)
                                        mock_notifier.close = AsyncMock()
                                        MockNotifier.return_value = mock_notifier

                                        from main import run_pipeline

                                        result = await run_pipeline()

    assert result.processed == 1
    assert result.enriched == 1
    assert mock_extract.call_count == 2


@pytest.mark.asyncio
async def test_x_bookmark_caps_external_articles(tmp_path: Path) -> None:
    """X bookmarks with >3 external_urls only extract first 3."""
    state_dir = tmp_path / "state"
    output_dir = tmp_path / "output"
    user_data_dir = tmp_path / "user_data"
    state_dir.mkdir()

    bm = Bookmark(
        source=Source.X,
        url="https://x.com/user/status/456",
        title="Many links",
        post_text="Check these out!",
        external_urls=[
            "https://example.com/1",
            "https://example.com/2",
            "https://example.com/3",
            "https://example.com/4",
            "https://example.com/5",
        ],
    )

    with patch("main.DEFAULT_STATE_DIR", str(state_dir)):
        with patch("main.DEFAULT_OUTPUT_DIR", str(output_dir)):
            with patch("main.DEFAULT_USER_DATA_DIR", str(user_data_dir)):
                with patch("main.AuthManager") as MockAuth:
                    mock_auth = AsyncMock()
                    mock_context = MagicMock()
                    mock_context.new_page = AsyncMock(return_value=MagicMock())
                    mock_auth.context = mock_context
                    mock_auth.ensure_browser = AsyncMock()
                    mock_auth.close = AsyncMock()
                    MockAuth.return_value = mock_auth

                    with patch("main.ScraperX") as MockScraperX:
                        MockScraperX.return_value.scrape = AsyncMock(return_value=[bm])
                        with patch("main.ScraperLinkedIn") as MockScraperLI:
                            MockScraperLI.return_value.scrape = AsyncMock(return_value=[])
                            with patch("main.LLMProcessor") as MockLLM:
                                mock_llm = AsyncMock()
                                mock_llm.enrich = AsyncMock(return_value=EnrichedBookmark(
                                    bookmark=bm,
                                    content=ExtractedContent(url=bm.url, post_text=bm.post_text, extraction_method="tweet_text"),
                                    enrichment=Enrichment(
                                        summary_bullets=["a", "b", "c"],
                                        takeaway="t",
                                        tags=["tag"],
                                        model_used="test",
                                        tokens=5,
                                    ),
                                ))
                                MockLLM.return_value = mock_llm

                                with patch("main.web_extract", new_callable=AsyncMock) as mock_extract:
                                    mock_extract.return_value = ExtractedContent(
                                        url="https://example.com/1",
                                        full_text="Content",
                                        extraction_method="trafilatura",
                                    )

                                    with patch("main.Notifier") as MockNotifier:
                                        mock_notifier = AsyncMock()
                                        mock_notifier.send_summary = AsyncMock(return_value=True)
                                        mock_notifier.close = AsyncMock()
                                        MockNotifier.return_value = mock_notifier

                                        from main import run_pipeline

                                        result = await run_pipeline()

    assert result.processed == 1
    assert mock_extract.call_count == 3  # MAX_EXTERNAL_ARTICLES=3


@pytest.mark.asyncio
async def test_x_bookmark_no_external_urls_skips_web_extract(tmp_path: Path) -> None:
    """X bookmarks without external_urls do not call web_extract."""
    state_dir = tmp_path / "state"
    output_dir = tmp_path / "output"
    user_data_dir = tmp_path / "user_data"
    state_dir.mkdir()

    bm = Bookmark(
        source=Source.X,
        url="https://x.com/user/status/789",
        title="Just a tweet",
        post_text="No links here!",
    )

    with patch("main.DEFAULT_STATE_DIR", str(state_dir)):
        with patch("main.DEFAULT_OUTPUT_DIR", str(output_dir)):
            with patch("main.DEFAULT_USER_DATA_DIR", str(user_data_dir)):
                with patch("main.AuthManager") as MockAuth:
                    mock_auth = AsyncMock()
                    mock_context = MagicMock()
                    mock_context.new_page = AsyncMock(return_value=MagicMock())
                    mock_auth.context = mock_context
                    mock_auth.ensure_browser = AsyncMock()
                    mock_auth.close = AsyncMock()
                    MockAuth.return_value = mock_auth

                    with patch("main.ScraperX") as MockScraperX:
                        MockScraperX.return_value.scrape = AsyncMock(return_value=[bm])
                        with patch("main.ScraperLinkedIn") as MockScraperLI:
                            MockScraperLI.return_value.scrape = AsyncMock(return_value=[])
                            with patch("main.LLMProcessor") as MockLLM:
                                mock_llm = AsyncMock()
                                mock_llm.enrich = AsyncMock(return_value=EnrichedBookmark(
                                    bookmark=bm,
                                    content=ExtractedContent(url=bm.url, post_text=bm.post_text, extraction_method="tweet_text"),
                                    enrichment=Enrichment(
                                        summary_bullets=["a", "b", "c"],
                                        takeaway="t",
                                        tags=["tag"],
                                        model_used="test",
                                        tokens=5,
                                    ),
                                ))
                                MockLLM.return_value = mock_llm

                                with patch("main.web_extract", new_callable=AsyncMock) as mock_extract:
                                    with patch("main.Notifier") as MockNotifier:
                                        mock_notifier = AsyncMock()
                                        mock_notifier.send_summary = AsyncMock(return_value=True)
                                        mock_notifier.close = AsyncMock()
                                        MockNotifier.return_value = mock_notifier

                                        from main import run_pipeline

                                        result = await run_pipeline()

    assert result.processed == 1
    assert mock_extract.call_count == 0  # No external URLs → no web_extract calls


@pytest.mark.asyncio
async def test_external_url_extraction_failure_still_writes_note(tmp_path: Path) -> None:
    """When web_extract fails for an external URL, note is still written with links."""
    state_dir = tmp_path / "state"
    output_dir = tmp_path / "output"
    user_data_dir = tmp_path / "user_data"
    state_dir.mkdir()

    bm = Bookmark(
        source=Source.X,
        url="https://x.com/user/status/999",
        title="Tweet with broken link",
        post_text="Read this!",
        external_urls=["https://broken-site.com/article"],
    )

    with patch("main.DEFAULT_STATE_DIR", str(state_dir)):
        with patch("main.DEFAULT_OUTPUT_DIR", str(output_dir)):
            with patch("main.DEFAULT_USER_DATA_DIR", str(user_data_dir)):
                with patch("main.AuthManager") as MockAuth:
                    mock_auth = AsyncMock()
                    mock_context = MagicMock()
                    mock_context.new_page = AsyncMock(return_value=MagicMock())
                    mock_auth.context = mock_context
                    mock_auth.ensure_browser = AsyncMock()
                    mock_auth.close = AsyncMock()
                    MockAuth.return_value = mock_auth

                    with patch("main.ScraperX") as MockScraperX:
                        MockScraperX.return_value.scrape = AsyncMock(return_value=[bm])
                        with patch("main.ScraperLinkedIn") as MockScraperLI:
                            MockScraperLI.return_value.scrape = AsyncMock(return_value=[])
                            with patch("main.LLMProcessor") as MockLLM:
                                mock_llm = AsyncMock()
                                mock_llm.enrich = AsyncMock(return_value=EnrichedBookmark(
                                    bookmark=bm,
                                    content=ExtractedContent(
                                        url=bm.url,
                                        post_text=bm.post_text,
                                        extraction_method="tweet_text",
                                        external_urls=bm.external_urls,
                                        external_articles=[
                                            ExternalArticle(url="https://broken-site.com/article", text=None, extraction_method="error"),
                                        ],
                                    ),
                                    enrichment=Enrichment(
                                        summary_bullets=["a", "b", "c"],
                                        takeaway="t",
                                        tags=["tag"],
                                        model_used="test",
                                        tokens=5,
                                    ),
                                ))
                                MockLLM.return_value = mock_llm

                                with patch("main.web_extract", new_callable=AsyncMock) as mock_extract:
                                    mock_extract.return_value = None  # Extraction fails

                                    with patch("main.Notifier") as MockNotifier:
                                        mock_notifier = AsyncMock()
                                        mock_notifier.send_summary = AsyncMock(return_value=True)
                                        mock_notifier.close = AsyncMock()
                                        MockNotifier.return_value = mock_notifier

                                        from main import run_pipeline

                                        result = await run_pipeline()

    assert result.processed == 1
    assert result.enriched == 1


@pytest.mark.asyncio
async def test_linkedin_bookmark_with_external_urls_extracts_articles(tmp_path: Path) -> None:
    """LinkedIn bookmarks with external_urls also get web extraction."""
    state_dir = tmp_path / "state"
    output_dir = tmp_path / "output"
    user_data_dir = tmp_path / "user_data"
    state_dir.mkdir()

    bm = Bookmark(
        source=Source.LINKEDIN,
        url="https://linkedin.com/posts/user-123",
        title="LinkedIn Post with link",
        post_text="Check this article!",
        external_urls=["https://example.com/article1"],
    )

    post_content = ExtractedContent(
        url="https://linkedin.com/posts/user-123",
        full_text="LinkedIn post content about something interesting.",
        post_text="Check this article!",
        extraction_method="trafilatura",
    )

    article_content = ExtractedContent(
        url="https://example.com/article1",
        full_text="Article 1 extracted content.",
        extraction_method="trafilatura",
    )

    with patch("main.DEFAULT_STATE_DIR", str(state_dir)):
        with patch("main.DEFAULT_OUTPUT_DIR", str(output_dir)):
            with patch("main.DEFAULT_USER_DATA_DIR", str(user_data_dir)):
                with patch("main.AuthManager") as MockAuth:
                    mock_auth = AsyncMock()
                    mock_context = MagicMock()
                    mock_context.new_page = AsyncMock(return_value=MagicMock())
                    mock_auth.context = mock_context
                    mock_auth.ensure_browser = AsyncMock()
                    mock_auth.close = AsyncMock()
                    MockAuth.return_value = mock_auth

                    with patch("main.ScraperX") as MockScraperX:
                        MockScraperX.return_value.scrape = AsyncMock(return_value=[])
                        with patch("main.ScraperLinkedIn") as MockScraperLI:
                            MockScraperLI.return_value.scrape = AsyncMock(return_value=[bm])
                            with patch("main.LLMProcessor") as MockLLM:
                                mock_llm = AsyncMock()

                                def enrich_side_effect(bookmark, content=None):
                                    return EnrichedBookmark(
                                        bookmark=bookmark,
                                        content=content or ExtractedContent(
                                            url=bookmark.url,
                                            post_text=bookmark.post_text,
                                            extraction_method="post_only",
                                        ),
                                        enrichment=Enrichment(
                                            summary_bullets=["a", "b", "c"],
                                            takeaway="t",
                                            tags=["linkedin"],
                                            model_used="test",
                                            tokens=5,
                                        ),
                                    )

                                mock_llm.enrich = AsyncMock(side_effect=enrich_side_effect)
                                MockLLM.return_value = mock_llm

                                with patch("main.web_extract", new_callable=AsyncMock) as mock_extract:
                                    mock_extract.side_effect = [post_content, article_content]

                                    with patch("main.Notifier") as MockNotifier:
                                        mock_notifier = AsyncMock()
                                        mock_notifier.send_summary = AsyncMock(return_value=True)
                                        mock_notifier.close = AsyncMock()
                                        MockNotifier.return_value = mock_notifier

                                        from main import run_pipeline

                                        result = await run_pipeline()

    assert result.processed == 1
    assert result.enriched == 1
    assert mock_extract.call_count == 2


# --- Parallel processing ---


@pytest.mark.asyncio
async def test_parallel_processes_all_bookmarks(tmp_path: Path) -> None:
    """Multiple bookmarks processed in parallel with gather."""
    state_dir = tmp_path / "state"
    output_dir = tmp_path / "output"
    user_data_dir = tmp_path / "user_data"
    state_dir.mkdir()

    bms = [
        _make_bookmark(url=f"https://example.com/article-{i}")
        for i in range(5)
    ]

    with patch("main.DEFAULT_STATE_DIR", str(state_dir)):
        with patch("main.DEFAULT_OUTPUT_DIR", str(output_dir)):
            with patch("main.DEFAULT_USER_DATA_DIR", str(user_data_dir)):
                with patch("main.AuthManager") as MockAuth:
                    mock_auth = AsyncMock()
                    mock_context = MagicMock()
                    mock_context.new_page = AsyncMock(return_value=MagicMock())
                    mock_auth.context = mock_context
                    mock_auth.ensure_browser = AsyncMock()
                    mock_auth.close = AsyncMock()
                    MockAuth.return_value = mock_auth

                    with patch("main.ScraperX") as MockScraperX:
                        MockScraperX.return_value.scrape = AsyncMock(return_value=bms)
                        with patch("main.ScraperLinkedIn") as MockScraperLI:
                            MockScraperLI.return_value.scrape = AsyncMock(return_value=[])
                            with patch("main.LLMProcessor") as MockLLM:
                                mock_llm = AsyncMock()
                                mock_llm.enrich = AsyncMock(return_value=EnrichedBookmark(
                                    bookmark=bms[0],
                                    content=ExtractedContent(url=bms[0].url, post_text=bms[0].post_text, extraction_method="post_only"),
                                    enrichment=Enrichment(
                                        summary_bullets=["a", "b", "c"],
                                        takeaway="t",
                                        tags=["tag"],
                                        model_used="test",
                                        tokens=5,
                                    ),
                                ))
                                MockLLM.return_value = mock_llm

                                with patch("main.web_extract", return_value=None):
                                    with patch("main.Notifier") as MockNotifier:
                                        mock_notifier = AsyncMock()
                                        mock_notifier.send_summary = AsyncMock(return_value=True)
                                        mock_notifier.close = AsyncMock()
                                        MockNotifier.return_value = mock_notifier

                                        from main import run_pipeline

                                        result = await run_pipeline()

    assert result.processed == 5


@pytest.mark.asyncio
async def test_parallel_single_failure_others_succeed(tmp_path: Path) -> None:
    """When one bookmark fails in parallel, others still succeed."""
    state_dir = tmp_path / "state"
    output_dir = tmp_path / "output"
    user_data_dir = tmp_path / "user_data"
    state_dir.mkdir()

    bm_good = _make_bookmark(url="https://example.com/good")
    bm_bad = _make_bookmark(url="https://example.com/bad")

    with patch("main.DEFAULT_STATE_DIR", str(state_dir)):
        with patch("main.DEFAULT_OUTPUT_DIR", str(output_dir)):
            with patch("main.DEFAULT_USER_DATA_DIR", str(user_data_dir)):
                with patch("main.AuthManager") as MockAuth:
                    mock_auth = AsyncMock()
                    mock_context = MagicMock()
                    mock_context.new_page = AsyncMock(return_value=MagicMock())
                    mock_auth.context = mock_context
                    mock_auth.ensure_browser = AsyncMock()
                    mock_auth.close = AsyncMock()
                    MockAuth.return_value = mock_auth

                    with patch("main.ScraperX") as MockScraperX:
                        MockScraperX.return_value.scrape = AsyncMock(return_value=[bm_good, bm_bad])
                        with patch("main.ScraperLinkedIn") as MockScraperLI:
                            MockScraperLI.return_value.scrape = AsyncMock(return_value=[])
                            with patch("main.LLMProcessor") as MockLLM:
                                mock_llm = AsyncMock()
                                from llm_processor import EnrichmentError

                                def enrich_side_effect(bookmark, content=None):
                                    if "bad" in str(bookmark.url):
                                        raise EnrichmentError("LLM failed for bad bookmark")
                                    return EnrichedBookmark(
                                        bookmark=bookmark,
                                        content=ExtractedContent(
                                            url=bookmark.url,
                                            post_text=bookmark.post_text,
                                            extraction_method="post_only",
                                        ),
                                        enrichment=Enrichment(
                                            summary_bullets=["a", "b", "c"],
                                            takeaway="Takeaway",
                                            tags=["tag"],
                                            model_used="test",
                                            tokens=5,
                                        ),
                                    )

                                mock_llm.enrich = AsyncMock(side_effect=enrich_side_effect)
                                MockLLM.return_value = mock_llm

                                with patch("main.web_extract", return_value=None):
                                    with patch("main.Notifier") as MockNotifier:
                                        mock_notifier = AsyncMock()
                                        mock_notifier.send_summary = AsyncMock(return_value=True)
                                        mock_notifier.close = AsyncMock()
                                        MockNotifier.return_value = mock_notifier

                                        from main import run_pipeline

                                        result = await run_pipeline()

    assert result.processed == 2
    assert result.enriched == 1
    assert result.dead_letter >= 1


@pytest.mark.asyncio
async def test_max_concurrent_env_var(tmp_path: Path) -> None:
    """MAX_CONCURRENT=1 processes sequentially (equivalent to old for-loop)."""
    state_dir = tmp_path / "state"
    output_dir = tmp_path / "output"
    user_data_dir = tmp_path / "user_data"
    state_dir.mkdir()

    bms = [
        _make_bookmark(url=f"https://example.com/art-{i}")
        for i in range(3)
    ]

    with patch("main.MAX_CONCURRENT", 1):
        with patch("main.DEFAULT_STATE_DIR", str(state_dir)):
            with patch("main.DEFAULT_OUTPUT_DIR", str(output_dir)):
                with patch("main.DEFAULT_USER_DATA_DIR", str(user_data_dir)):
                    with patch("main.AuthManager") as MockAuth:
                        mock_auth = AsyncMock()
                        mock_context = MagicMock()
                        mock_context.new_page = AsyncMock(return_value=MagicMock())
                        mock_auth.context = mock_context
                        mock_auth.ensure_browser = AsyncMock()
                        mock_auth.close = AsyncMock()
                        MockAuth.return_value = mock_auth

                        with patch("main.ScraperX") as MockScraperX:
                            MockScraperX.return_value.scrape = AsyncMock(return_value=bms)
                            with patch("main.ScraperLinkedIn") as MockScraperLI:
                                MockScraperLI.return_value.scrape = AsyncMock(return_value=[])
                                with patch("main.LLMProcessor") as MockLLM:
                                    mock_llm = AsyncMock()
                                    mock_llm.enrich = AsyncMock(return_value=EnrichedBookmark(
                                        bookmark=bms[0],
                                        content=ExtractedContent(url=bms[0].url, post_text=bms[0].post_text, extraction_method="post_only"),
                                        enrichment=Enrichment(
                                            summary_bullets=["a", "b", "c"],
                                            takeaway="t",
                                            tags=["tag"],
                                            model_used="test",
                                            tokens=5,
                                        ),
                                    ))
                                    MockLLM.return_value = mock_llm

                                    with patch("main.web_extract", return_value=None):
                                        with patch("main.Notifier") as MockNotifier:
                                            mock_notifier = AsyncMock()
                                            mock_notifier.send_summary = AsyncMock(return_value=True)
                                            mock_notifier.close = AsyncMock()
                                            MockNotifier.return_value = mock_notifier

                                            from main import run_pipeline

                                            result = await run_pipeline()

    assert result.processed == 3
