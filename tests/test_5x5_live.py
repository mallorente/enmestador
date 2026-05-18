"""Integration test: 5 X bookmarks + 5 LinkedIn bookmarks through the full pipeline.

All external services (browser, LLM, web extraction) are mocked.
The pipeline runs from end to end and writes real Markdown files
into a temp directory so we can inspect the output.
"""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from models import (
    Bookmark,
    EnrichedBookmark,
    Enrichment,
    ExtractedContent,
    ExternalArticle,
    PipelineResult,
    ScrapeMode,
    Source,
)


# --- Sample data ---

X_BOOKMARKS = [
    Bookmark(
        source=Source.X,
        url=f"https://x.com/user/status/{i}",
        title=f"X Post {i}: Topic on {t}",
        post_text=f"Interesting thoughts on {t} from my perspective. Thread",
        external_urls=[f"https://example.com/article/x-{i}"] if i % 2 == 0 else None,
        saved_at=None,
    )
    for i, t in enumerate(
        ["software architecture", "Python async", "system design", "testing patterns", "DevOps culture"],
        start=1,
    )
]

LI_BOOKMARKS = [
    Bookmark(
        source=Source.LINKEDIN,
        url=f"https://linkedin.com/posts/user-{i}",
        title=f"LI Post {i}: {t}",
        post_text=f"Great insights about {t} worth saving.",
        saved_at=None,
    )
    for i, t in enumerate(
        ["leadership lessons", "hiring tips", "startup growth", "remote work", "AI in finance"],
        start=1,
    )
]


def _make_enriched(bookmark: Bookmark, content: ExtractedContent) -> EnrichedBookmark:
    return EnrichedBookmark(
        bookmark=bookmark,
        content=content,
        enrichment=Enrichment(
            summary_bullets=["Key point 1", "Key point 2", "Key point 3"],
            takeaway=f"Main takeaway from {bookmark.title}",
            tags=["saved", bookmark.source.value, "insights"],
            model_used="test-mock-llm",
            tokens=42,
        ),
    )


def _make_x_content(bookmark: Bookmark) -> ExtractedContent:
    articles = []
    if bookmark.external_urls:
        for url in bookmark.external_urls:
            articles.append(ExternalArticle(
                url=url,
                text=f"Extracted article content for {url}. It discusses various technical topics in depth.",
                extraction_method="trafilatura",
            ))
    return ExtractedContent(
        url=bookmark.url,
        full_text=f"Full extracted text for {bookmark.title}" if bookmark.external_urls else None,
        post_text=bookmark.post_text,
        extraction_method="tweet_text",
        external_urls=bookmark.external_urls,
        external_articles=articles or None,
    )


def _make_li_content(bookmark: Bookmark) -> ExtractedContent:
    return ExtractedContent(
        url=bookmark.url,
        full_text=f"Full article text extracted from LinkedIn post: {bookmark.title}. "
        f"This article covers {bookmark.post_text} and provides actionable advice.",
        post_text=bookmark.post_text,
        extraction_method="trafilatura",
    )


OUTPUT_DIR = Path(__file__).resolve().parent.parent / "test_output"


@pytest.mark.asyncio
async def test_5x5_bookmarks_pipeline(tmp_path: Path) -> None:
    """Run the full pipeline with 5 X + 5 LinkedIn bookmarks and inspect output."""
    state_dir = tmp_path / "state"
    output_dir = OUTPUT_DIR
    user_data_dir = tmp_path / "user_data"
    state_dir.mkdir()
    output_dir.mkdir(parents=True, exist_ok=True)

    all_bookmarks = X_BOOKMARKS + LI_BOOKMARKS

    # Build side_effect for LLM enrich — returns proper EnrichedBookmark per source
    async def enrich_side_effect(bookmark: Bookmark, content: ExtractedContent = None):
        if bookmark.source == Source.X:
            cont = content or _make_x_content(bookmark)
        else:
            cont = content or _make_li_content(bookmark)
        return _make_enriched(bookmark, cont)

    # Build side_effect for web_extract — returns content for external URLs
    call_count = 0

    async def extract_side_effect(url: str, post_text: str = None):
        nonlocal call_count
        call_count += 1
        return ExtractedContent(
            url=url,
            full_text=f"Web-clipped content from {url}. This is an in-depth article.",
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
                        MockScraperX.return_value.scrape = AsyncMock(return_value=X_BOOKMARKS)
                        with patch("main.ScraperLinkedIn") as MockScraperLI:
                            MockScraperLI.return_value.scrape = AsyncMock(return_value=LI_BOOKMARKS)
                            with patch("main.LLMProcessor") as MockLLM:
                                mock_llm = AsyncMock()
                                mock_llm.enrich = AsyncMock(side_effect=enrich_side_effect)
                                MockLLM.return_value = mock_llm

                                with patch("main.web_extract", new_callable=AsyncMock) as mock_extract:
                                    mock_extract.side_effect = extract_side_effect

                                    with patch("main.Notifier") as MockNotifier:
                                        mock_notifier = AsyncMock()
                                        mock_notifier.send_summary = AsyncMock(return_value=True)
                                        mock_notifier.close = AsyncMock()
                                        MockNotifier.return_value = mock_notifier

                                        from main import run_pipeline

                                        result = await run_pipeline()

    # --- Assertions ---
    assert result.processed == 10, f"Expected 10 processed, got {result.processed}"
    assert result.enriched == 10, f"Expected 10 enriched, got {result.enriched}"
    assert result.dead_letter == 0, f"Expected 0 dead letters, got {result.dead_letter}"

    # Verify output directory structure
    x_dir = output_dir / "x"
    li_dir = output_dir / "linkedin"
    assert x_dir.exists(), "X output directory should exist"
    assert li_dir.exists(), "LinkedIn output directory should exist"

    x_files = list(x_dir.glob("*.md"))
    li_files = list(li_dir.glob("*.md"))
    print(f"\n=== X bookmarks: {len(x_files)} files ===")
    print(f"=== LinkedIn bookmarks: {len(li_files)} files ===")

    assert len(x_files) == 5, f"Expected 5 X markdown files, got {len(x_files)}"
    assert len(li_files) == 5, f"Expected 5 LinkedIn markdown files, got {len(li_files)}"

    # Print all generated files for inspection
    print("\n" + "=" * 60)
    print("GENERATED MARKDOWN FILES")
    print("=" * 60)

    for f in sorted(x_files):
        print(f"\n--- {f.name} (X) ---")
        content = f.read_text(encoding="utf-8")
        print(content[:800])
        if len(content) > 800:
            print(f"... [{len(content)} chars total]")

    for f in sorted(li_files):
        print(f"\n--- {f.name} (LinkedIn) ---")
        content = f.read_text(encoding="utf-8")
        print(content[:800])
        if len(content) > 800:
            print(f"... [{len(content)} chars total]")

    # Verify frontmatter structure
    for f in x_files:
        content = f.read_text(encoding="utf-8")
        assert content.startswith("---"), f"Missing frontmatter in {f.name}"
        assert "source: x" in content, f"Missing source in {f.name}"
        assert "## Summary" in content, f"Missing Summary section in {f.name}"
        assert "## Takeaway" in content, f"Missing Takeaway section in {f.name}"

    for f in li_files:
        content = f.read_text(encoding="utf-8")
        assert content.startswith("---"), f"Missing frontmatter in {f.name}"
        assert "source: linkedin" in content, f"Missing source in {f.name}"
        assert "## Summary" in content, f"Missing Summary section in {f.name}"

    # Verify processed URLs were recorded
    processed_file = state_dir / "processed_urls.json"
    assert processed_file.exists()
    urls_data = json.loads(processed_file.read_text())
    assert len(urls_data["urls"]) == 10, f"Expected 10 processed URLs, got {len(urls_data['urls'])}"

    # Verify cursors were updated
    cursors_file = state_dir / "cursors.json"
    assert cursors_file.exists()

    # Verify web_extract was called for X bookmarks with external_urls (posts 2, 4)
    # (posts at index 1 and 3 have external_urls because i%2==0 for i=2,4)
    assert call_count >= 2, f"Expected at least 2 web_extract calls for external URLs, got {call_count}"

    print("\n" + "=" * 60)
    print("PIPELINE RESULT")
    print("=" * 60)
    print(f"  Processed:  {result.processed}")
    print(f"  Enriched:   {result.enriched}")
    print(f"  Dead letter:{result.dead_letter}")
    print(f"  X files:    {len(x_files)}")
    print(f"  LI files:   {len(li_files)}")