"""Tests for the writer module."""

from datetime import UTC, datetime
from pathlib import Path

import yaml

from models import Bookmark, EnrichedBookmark, Enrichment, ExternalArticle, ExtractedContent, Source
from pipeline.writer import Writer, _build_body, _build_frontmatter, _sanitize_filename

# --- _sanitize_filename ---


def test_sanitize_basic_title() -> None:
    assert _sanitize_filename("Hello World") == "hello-world.md"


def test_sanitize_special_chars() -> None:
    assert _sanitize_filename("C++ Tips & Tricks!") == "c-tips-tricks.md"


def test_sanitize_multiple_spaces() -> None:
    assert _sanitize_filename("The   Quick   Brown") == "the-quick-brown.md"


def test_sanitize_underscores() -> None:
    assert _sanitize_filename("my_article_title") == "my-article-title.md"


def test_sanitize_unicode() -> None:
    # Unicode chars should be stripped
    result = _sanitize_filename("Art\u00edculo sobre Python")
    assert "rt" in result
    assert "culo" in result
    assert result.endswith(".md")


def test_sanitize_empty_title() -> None:
    assert _sanitize_filename("") == "untitled.md"
    assert _sanitize_filename("!!!") == "untitled.md"


def test_sanitize_leading_trailing_hyphens() -> None:
    assert _sanitize_filename("--- title ---") == "title.md"


def test_sanitize_already_valid() -> None:
    assert _sanitize_filename("clean-title") == "clean-title.md"


# --- _build_frontmatter ---


def test_frontmatter_with_enrichment() -> None:
    bookmark = Bookmark(
        source=Source.X,
        url="https://example.com/article",
        title="Test Article",
        saved_at=datetime(2026, 5, 13, 2, 0, 0, tzinfo=UTC),
    )
    content = ExtractedContent(
        url=bookmark.url,
        post_text="Post text",
        extraction_method="post_only",
    )
    enrichment = Enrichment(
        summary_bullets=["Bullet 1", "Bullet 2", "Bullet 3"],
        takeaway="Key insight",
        tags=["python", "architecture"],
        model_used="deepseek-v4-pro",
        tokens=500,
    )
    enriched = EnrichedBookmark(bookmark=bookmark, content=content, enrichment=enrichment)

    fm = _build_frontmatter(enriched)
    parsed = yaml.safe_load(fm)
    assert parsed["title"] == "Test Article"
    assert parsed["source"] == "x"
    assert parsed["url"] == "https://example.com/article"
    assert parsed["tags"] == ["python", "architecture"]
    assert parsed["model"] == "deepseek-v4-pro"


def test_frontmatter_without_enrichment() -> None:
    bookmark = Bookmark(
        source=Source.LINKEDIN,
        url="https://linkedin.com/post/123",
        title="LinkedIn Post",
    )
    content = ExtractedContent(
        url=bookmark.url,
        post_text="Some post",
        extraction_method="post_only",
    )
    enriched = EnrichedBookmark(bookmark=bookmark, content=content, enrichment=None)

    fm = _build_frontmatter(enriched)
    parsed = yaml.safe_load(fm)
    assert parsed["tags"] == []
    assert parsed["model"] == "none"


def test_frontmatter_no_saved_at() -> None:
    bookmark = Bookmark(
        source=Source.X,
        url="https://example.com",
        title="No Date",
    )
    content = ExtractedContent(
        url=bookmark.url,
        post_text="text",
        extraction_method="post_only",
    )
    enriched = EnrichedBookmark(bookmark=bookmark, content=content, enrichment=None)
    fm = _build_frontmatter(enriched)
    parsed = yaml.safe_load(fm)
    assert "saved" not in parsed


# --- _build_body ---


def test_body_with_enrichment() -> None:
    bookmark = Bookmark(
        source=Source.X,
        url="https://example.com",
        title="Test",
    )
    content = ExtractedContent(
        url=bookmark.url,
        post_text="Original post",
        extraction_method="post_only",
    )
    enrichment = Enrichment(
        summary_bullets=["A", "B", "C"],
        takeaway="Insight",
        tags=["tag1"],
        model_used="model",
        tokens=100,
    )
    enriched = EnrichedBookmark(bookmark=bookmark, content=content, enrichment=enrichment)

    body = _build_body(enriched)
    assert "## Summary" in body
    assert "- A" in body
    assert "## Takeaway" in body
    assert "Insight" in body
    assert "## Original" in body
    assert "Original post" in body


def test_body_with_article_text() -> None:
    bookmark = Bookmark(
        source=Source.X,
        url="https://example.com",
        title="Test",
    )
    content = ExtractedContent(
        url=bookmark.url,
        full_text="Full article text here",
        post_text="Post text",
        extraction_method="trafilatura",
    )
    enriched = EnrichedBookmark(bookmark=bookmark, content=content, enrichment=None)

    body = _build_body(enriched)
    assert "## Original" in body
    assert "Post text" in body
    assert "## Article Text" in body
    assert "Full article text here" in body


def test_body_no_enrichment_no_article() -> None:
    bookmark = Bookmark(
        source=Source.X,
        url="https://example.com",
        title="Test",
    )
    content = ExtractedContent(
        url=bookmark.url,
        post_text="Just the post",
        extraction_method="post_only",
    )
    enriched = EnrichedBookmark(bookmark=bookmark, content=content, enrichment=None)

    body = _build_body(enriched)
    assert "## Summary" not in body
    assert "## Takeaway" not in body
    assert "## Original" in body


def test_body_with_external_articles() -> None:
    bookmark = Bookmark(
        source=Source.X,
        url="https://x.com/tweet/123",
        title="Tweet with link",
        external_urls=["https://example.com/article1", "https://example.com/article2"],
    )
    articles = [
        ExternalArticle(url="https://example.com/article1", text="Article 1 content here", extraction_method="trafilatura"),
        ExternalArticle(url="https://example.com/article2", text=None, extraction_method="failed"),
    ]
    content = ExtractedContent(
        url=bookmark.url,
        post_text="Check this out",
        extraction_method="tweet_text",
        external_urls=bookmark.external_urls,
        external_articles=articles,
    )
    enriched = EnrichedBookmark(bookmark=bookmark, content=content, enrichment=None)

    body = _build_body(enriched)
    assert "## Article: https://example.com/article1" in body
    assert "Article 1 content here" in body
    assert "## Article: https://example.com/article2" in body
    assert "(Extraction failed" in body
    assert "## Links" not in body


def test_body_with_external_urls_no_articles_backward_compat() -> None:
    bookmark = Bookmark(
        source=Source.X,
        url="https://x.com/tweet/456",
        title="Tweet with link but no articles",
        external_urls=["https://example.com/article1"],
    )
    content = ExtractedContent(
        url=bookmark.url,
        post_text="Read this",
        extraction_method="tweet_text",
        external_urls=bookmark.external_urls,
    )
    enriched = EnrichedBookmark(bookmark=bookmark, content=content, enrichment=None)

    body = _build_body(enriched)
    assert "## Links" in body
    assert "- https://example.com/article1" in body
    assert "## Article:" not in body


# --- Writer.write ---


def test_writer_creates_file(tmp_path: Path) -> None:
    output_dir = tmp_path / "output"
    writer = Writer(output_dir)
    bookmark = Bookmark(
        source=Source.X,
        url="https://example.com/article",
        title="My Article",
    )
    content = ExtractedContent(
        url=bookmark.url,
        post_text="Content",
        extraction_method="post_only",
    )
    enriched = EnrichedBookmark(bookmark=bookmark, content=content, enrichment=None)

    result = writer.write(enriched)
    assert result.exists()
    assert result.name == "my-article.md"
    assert result.parent.name == "x"


def test_writer_valid_yaml_frontmatter(tmp_path: Path) -> None:
    output_dir = tmp_path / "output"
    writer = Writer(output_dir)
    bookmark = Bookmark(
        source=Source.X,
        url="https://example.com/article",
        title="Test",
        saved_at=datetime(2026, 5, 13, 2, 0, 0, tzinfo=UTC),
    )
    content = ExtractedContent(
        url=bookmark.url,
        post_text="Content",
        extraction_method="post_only",
    )
    enrichment = Enrichment(
        summary_bullets=["A", "B", "C"],
        takeaway="T",
        tags=["t1"],
        model_used="m1",
        tokens=100,
    )
    enriched = EnrichedBookmark(bookmark=bookmark, content=content, enrichment=enrichment)

    path = writer.write(enriched)
    text = path.read_text()
    # Extract frontmatter between --- delimiters
    parts = text.split("---")
    assert len(parts) >= 3, "Should have YAML frontmatter delimited by ---"
    fm_text = parts[1].strip()
    fm = yaml.safe_load(fm_text)
    assert fm["title"] == "Test"
    assert fm["source"] == "x"


def test_writer_collision_appends(tmp_path: Path) -> None:
    output_dir = tmp_path / "output"
    writer = Writer(output_dir)
    bookmark1 = Bookmark(
        source=Source.X,
        url="https://example.com/1",
        title="Same Title",
    )
    bookmark2 = Bookmark(
        source=Source.X,
        url="https://example.com/2",
        title="Same Title",
    )
    content1 = ExtractedContent(url=bookmark1.url, post_text="First", extraction_method="post_only")
    content2 = ExtractedContent(url=bookmark2.url, post_text="Second", extraction_method="post_only")
    enriched1 = EnrichedBookmark(bookmark=bookmark1, content=content1, enrichment=None)
    enriched2 = EnrichedBookmark(bookmark=bookmark2, content=content2, enrichment=None)

    writer.write(enriched1)
    writer.write(enriched2)

    filepath = output_dir / "x" / "same-title.md"
    text = filepath.read_text()
    assert "First" in text
    assert "Second" in text
    assert text.count("---") >= 3


def test_writer_creates_output_dir(tmp_path: Path) -> None:
    output_dir = tmp_path / "nested" / "output"
    writer = Writer(output_dir)
    bookmark = Bookmark(
        source=Source.X,
        url="https://example.com",
        title="Test",
    )
    content = ExtractedContent(
        url=bookmark.url,
        post_text="Content",
        extraction_method="post_only",
    )
    enriched = EnrichedBookmark(bookmark=bookmark, content=content, enrichment=None)

    writer.write(enriched)
    assert output_dir.exists()
    assert (output_dir / "x").exists()


def test_writer_organizes_by_source_x(tmp_path: Path) -> None:
    output_dir = tmp_path / "output"
    writer = Writer(output_dir)
    bookmark = Bookmark(source=Source.X, url="https://x.com/tweet/1", title="Test Tweet")
    content = ExtractedContent(url=bookmark.url, post_text="text", extraction_method="post_only")
    enriched = EnrichedBookmark(bookmark=bookmark, content=content, enrichment=None)

    result = writer.write(enriched)
    assert result.exists()
    assert result.parent.name == "x"
    assert result.name == "test-tweet.md"


def test_writer_organizes_by_source_linkedin(tmp_path: Path) -> None:
    output_dir = tmp_path / "output"
    writer = Writer(output_dir)
    bookmark = Bookmark(source=Source.LINKEDIN, url="https://linkedin.com/post/1", title="Test Post")
    content = ExtractedContent(url=bookmark.url, post_text="text", extraction_method="post_only")
    enriched = EnrichedBookmark(bookmark=bookmark, content=content, enrichment=None)

    result = writer.write(enriched)
    assert result.exists()
    assert result.parent.name == "linkedin"
    assert result.name == "test-post.md"


def test_writer_collision_across_sources(tmp_path: Path) -> None:
    output_dir = tmp_path / "output"
    writer = Writer(output_dir)
    bm_x = Bookmark(source=Source.X, url="https://x.com/1", title="Same Title")
    bm_li = Bookmark(source=Source.LINKEDIN, url="https://linkedin.com/1", title="Same Title")
    content = ExtractedContent(url="https://example.com", post_text="text", extraction_method="post_only")

    enriched_x = EnrichedBookmark(bookmark=bm_x, content=content, enrichment=None)
    enriched_li = EnrichedBookmark(bookmark=bm_li, content=content, enrichment=None)

    path_x = writer.write(enriched_x)
    path_li = writer.write(enriched_li)

    assert path_x != path_li
    assert path_x.parent.name == "x"
    assert path_li.parent.name == "linkedin"
