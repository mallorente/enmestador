"""Tests for Pydantic models — validation, JSON round-trip, invalid URL rejection."""

import pytest
from pydantic import ValidationError

from models import (
    Bookmark,
    DeadLetter,
    EnrichedBookmark,
    Enrichment,
    ExternalArticle,
    ExtractedContent,
    PipelineResult,
    ScrapeMode,
    Source,
)


class TestSource:
    def test_source_x(self):
        assert Source.X.value == "x"

    def test_source_linkedin(self):
        assert Source.LINKEDIN.value == "linkedin"


class TestScrapeMode:
    def test_bootstrap(self):
        assert ScrapeMode.BOOTSTRAP.value == "bootstrap"

    def test_delta(self):
        assert ScrapeMode.DELTA.value == "delta"


class TestBookmark:
    def test_valid_bookmark(self, sample_bookmark_data):
        bm = Bookmark(**sample_bookmark_data)
        assert bm.source == Source.X
        assert str(bm.url) == "https://example.com/article"
        assert bm.title == "Test Article"

    def test_bookmark_json_roundtrip(self, sample_bookmark_data):
        bm = Bookmark(**sample_bookmark_data)
        serialized = bm.model_dump_json()
        restored = Bookmark.model_validate_json(serialized)
        assert restored.source == bm.source
        assert restored.url == bm.url
        assert restored.title == bm.title

    def test_bookmark_minimal(self):
        bm = Bookmark(
            source=Source.LINKEDIN,
            url="https://linkedin.com/post/123",
            title="LinkedIn Post",
        )
        assert bm.post_text is None
        assert bm.saved_at is None

    def test_invalid_url_rejected(self, sample_bookmark_data):
        sample_bookmark_data["url"] = "not-a-url"
        with pytest.raises(ValidationError):
            Bookmark(**sample_bookmark_data)

    def test_invalid_source_rejected(self, sample_bookmark_data):
        sample_bookmark_data["source"] = "facebook"
        with pytest.raises(ValidationError):
            Bookmark(**sample_bookmark_data)


class TestExtractedContent:
    def test_valid_extraction(self):
        ec = ExtractedContent(
            url="https://example.com/article",
            full_text="The full article text here.",
            post_text="Shared post text",
            extraction_method="trafilatura",
        )
        assert ec.extraction_method == "trafilatura"
        assert ec.extracted_at is not None

    def test_post_only_extraction(self):
        ec = ExtractedContent(
            url="https://example.com/post",
            post_text="Post text only",
            extraction_method="post_only",
        )
        assert ec.full_text is None
        assert ec.extraction_method == "post_only"

    def test_json_roundtrip(self):
        ec = ExtractedContent(
            url="https://example.com/article",
            full_text="Content",
            extraction_method="trafilatura",
        )
        serialized = ec.model_dump_json()
        restored = ExtractedContent.model_validate_json(serialized)
        assert restored.url == ec.url
        assert restored.full_text == ec.full_text

    def test_with_external_articles(self):
        articles = [
            ExternalArticle(url="https://example.com/article1", text="Article 1 content", extraction_method="trafilatura"),
            ExternalArticle(url="https://example.com/article2", text=None, extraction_method="failed"),
        ]
        ec = ExtractedContent(
            url="https://x.com/tweet/123",
            full_text="Tweet text",
            extraction_method="tweet_text",
            external_articles=articles,
        )
        assert ec.external_articles is not None
        assert len(ec.external_articles) == 2
        assert ec.external_articles[0].text == "Article 1 content"
        assert ec.external_articles[1].text is None

    def test_external_articles_default_none(self):
        ec = ExtractedContent(
            url="https://example.com/article",
            extraction_method="post_only",
        )
        assert ec.external_articles is None


class TestExternalArticle:
    def test_valid_article(self):
        ea = ExternalArticle(url="https://example.com/article", text="Full article text", extraction_method="trafilatura")
        assert ea.url == "https://example.com/article"
        assert ea.text == "Full article text"
        assert ea.extraction_method == "trafilatura"

    def test_failed_extraction(self):
        ea = ExternalArticle(url="https://broken.com", text=None, extraction_method="failed")
        assert ea.text is None
        assert ea.extraction_method == "failed"

    def test_default_extraction_method(self):
        ea = ExternalArticle(url="https://example.com/article", text="Content")
        assert ea.extraction_method == "trafilatura"

    def test_json_roundtrip(self):
        ea = ExternalArticle(url="https://example.com/article", text="Content", extraction_method="trafilatura")
        serialized = ea.model_dump_json()
        restored = ExternalArticle.model_validate_json(serialized)
        assert restored.url == ea.url
        assert restored.text == ea.text


class TestEnrichment:
    def test_valid_enrichment(self):
        e = Enrichment(
            summary_bullets=["Bullet 1", "Bullet 2", "Bullet 3"],
            takeaway="Key takeaway sentence.",
            tags=["python", "architecture"],
            model_used="deepseek-v4-pro",
            tokens=1500,
        )
        assert len(e.summary_bullets) == 3
        assert e.model_used == "deepseek-v4-pro"

    def test_enrichment_wrong_bullet_count(self):
        with pytest.raises(ValidationError):
            Enrichment(
                summary_bullets=["Only one"],
                takeaway="Takeaway",
                tags=["tag"],
                model_used="test",
                tokens=100,
            )

    def test_enrichment_too_many_bullets(self):
        with pytest.raises(ValidationError):
            Enrichment(
                summary_bullets=["A", "B", "C", "D"],
                takeaway="Takeaway",
                tags=["tag"],
                model_used="test",
                tokens=100,
            )


class TestEnrichedBookmark:
    def test_with_enrichment(self, sample_bookmark_data):
        bm = Bookmark(**sample_bookmark_data)
        ec = ExtractedContent(
            url=bm.url,
            full_text="Article content",
            post_text=bm.post_text,
            extraction_method="trafilatura",
        )
        e = Enrichment(
            summary_bullets=["B1", "B2", "B3"],
            takeaway="Takeaway",
            tags=["tag1"],
            model_used="deepseek-v4-pro",
            tokens=500,
        )
        enriched = EnrichedBookmark(bookmark=bm, content=ec, enrichment=e)
        assert enriched.enrichment is not None
        assert enriched.enrichment.model_used == "deepseek-v4-pro"

    def test_without_enrichment(self, sample_bookmark_data):
        bm = Bookmark(**sample_bookmark_data)
        ec = ExtractedContent(
            url=bm.url,
            post_text=bm.post_text,
            extraction_method="post_only",
        )
        enriched = EnrichedBookmark(bookmark=bm, content=ec)
        assert enriched.enrichment is None


class TestDeadLetter:
    def test_dead_letter_creation(self, sample_bookmark_data):
        bm = Bookmark(**sample_bookmark_data)
        dl = DeadLetter(bookmark=bm, error="Timeout", stage="extraction")
        assert dl.error == "Timeout"
        assert dl.stage == "extraction"
        assert dl.timestamp is not None

    def test_dead_letter_json_roundtrip(self, sample_bookmark_data):
        bm = Bookmark(**sample_bookmark_data)
        dl = DeadLetter(bookmark=bm, error="Schema change", stage="llm")
        serialized = dl.model_dump_json()
        restored = DeadLetter.model_validate_json(serialized)
        assert restored.error == dl.error
        assert restored.stage == dl.stage


class TestPipelineResult:
    def test_pipeline_result(self):
        result = PipelineResult(
            processed=10,
            enriched=8,
            dead_letter=2,
            new_cursor_x="cursor123",
        )
        assert result.processed == 10
        assert result.enriched == 8
        assert result.dead_letter == 2
        assert result.new_cursor_x == "cursor123"
        assert result.new_cursor_linkedin is None

    def test_pipeline_result_json_roundtrip(self):
        result = PipelineResult(
            processed=5,
            enriched=5,
            dead_letter=0,
        )
        serialized = result.model_dump_json()
        restored = PipelineResult.model_validate_json(serialized)
        assert restored.processed == result.processed
