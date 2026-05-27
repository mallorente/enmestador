"""Pydantic models for the PKM ingestion pipeline."""

from datetime import UTC, datetime
from enum import Enum

from pydantic import BaseModel, Field, HttpUrl


class Source(str, Enum):
    """Source platform for a bookmark."""

    X = "x"
    LINKEDIN = "linkedin"


class ScrapeMode(str, Enum):
    """Scrape execution mode."""

    BOOTSTRAP = "bootstrap"
    DELTA = "delta"


class Bookmark(BaseModel):
    """A bookmark extracted from X or LinkedIn."""

    source: Source
    url: HttpUrl
    title: str
    post_text: str | None = None
    external_urls: list[str] | None = None
    referenced_tweet_urls: list[str] | None = None
    image_urls: list[str] | None = None
    published_at: datetime | None = None
    saved_at: datetime | None = None
    retrieved_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ExternalArticle(BaseModel):
    """Content extracted from an external URL found in a tweet's links."""

    url: str
    text: str | None = None
    image_urls: list[str] | None = None
    extraction_method: str = "trafilatura"


class ExtractedContent(BaseModel):
    """Clean article text extracted via trafilatura."""

    url: HttpUrl
    full_text: str | None = None
    post_text: str | None = None
    extraction_method: str  # "trafilatura" | "post_only" | "tweet_text"
    external_urls: list[str] | None = None
    referenced_tweet_urls: list[str] | None = None
    external_articles: list[ExternalArticle] | None = None
    image_urls: list[str] | None = None
    extracted_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class Enrichment(BaseModel):
    """LLM-generated enrichment for a bookmark."""

    summary_bullets: list[str] = Field(..., min_length=3, max_length=3)
    takeaway: str
    tags: list[str]
    model_used: str
    tokens: int


class EnrichedBookmark(BaseModel):
    """A bookmark with extracted content and optional LLM enrichment."""

    bookmark: Bookmark
    content: ExtractedContent
    enrichment: Enrichment | None = None


class DeadLetter(BaseModel):
    """A failed item captured for later review."""

    bookmark: Bookmark
    error: str
    stage: str  # "extraction" | "llm" | "write"
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))


class PipelineResult(BaseModel):
    """Summary of a pipeline run."""

    processed: int
    enriched: int
    dead_letter: int
    new_cursor_x: str | None = None
    new_cursor_linkedin: str | None = None
