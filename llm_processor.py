"""LLM processor for enriching bookmarks with AI-generated summaries."""

import asyncio
import logging
import re
import time

import httpx
from openai import AsyncOpenAI

from config import (
    LLM_API_KEY,
    LLM_BASE_URL,
    LLM_FALLBACK_3_API_KEY,
    LLM_FALLBACK_3_BASE_URL,
    LLM_FALLBACK_3_MODEL,
    LLM_FALLBACK_4_API_KEY,
    LLM_FALLBACK_4_BASE_URL,
    LLM_FALLBACK_4_MODEL,
    LLM_MAX_RETRIES,
    LLM_MODEL,
    LLM_MODEL_FALLBACK_1,
    LLM_MODEL_FALLBACK_2,
    LLM_RATE_LIMIT_SEC,
)
from models import Bookmark, EnrichedBookmark, Enrichment, ExtractedContent, Source

logger = logging.getLogger(__name__)


class EnrichmentError(Exception):
    """Raised when all LLM endpoints are exhausted."""


def _make_client(base_url: str, api_key: str) -> AsyncOpenAI:
    http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(120.0, connect=30.0),
        limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
    )
    return AsyncOpenAI(
        base_url=base_url,
        api_key=api_key,
        timeout=120.0,
        max_retries=2,
        http_client=http_client,
    )


class LLMProcessor:
    """Enriches bookmarks via LLM with multi-provider fallback chain."""

    MAX_RETRIES = LLM_MAX_RETRIES
    RATE_LIMIT_SEC = LLM_RATE_LIMIT_SEC

    def __init__(self) -> None:
        self._endpoints = self._build_endpoints()
        self._last_request_time: dict[str, float] = {}
        logger.info(
            "LLM endpoints: %s",
            [(m, u) for u, m in ((e[1].base_url, e[0]) for e in self._endpoints)],
        )

    def _build_endpoints(self) -> list[tuple[str, AsyncOpenAI]]:
        """Return ordered list of (model, client) across all configured providers."""
        endpoints: list[tuple[str, AsyncOpenAI]] = []

        # Primary provider — opencode.ai (shared client for all its models)
        if LLM_BASE_URL and LLM_API_KEY:
            primary_client = _make_client(LLM_BASE_URL, LLM_API_KEY)
            for model in [LLM_MODEL, LLM_MODEL_FALLBACK_1, LLM_MODEL_FALLBACK_2]:
                if model:
                    endpoints.append((model, primary_client))

        # Extra providers — each gets its own client
        extras = [
            (LLM_FALLBACK_3_BASE_URL, LLM_FALLBACK_3_API_KEY, LLM_FALLBACK_3_MODEL),
            (LLM_FALLBACK_4_BASE_URL, LLM_FALLBACK_4_API_KEY, LLM_FALLBACK_4_MODEL),
        ]
        for base_url, api_key, model in extras:
            if base_url and api_key and model:
                endpoints.append((model, _make_client(base_url, api_key)))

        return endpoints

    async def _rate_limit(self, source: str = "default") -> None:
        elapsed = time.monotonic() - self._last_request_time.get(source, 0.0)
        if elapsed < self.RATE_LIMIT_SEC:
            await asyncio.sleep(self.RATE_LIMIT_SEC - elapsed)
        self._last_request_time[source] = time.monotonic()

    async def _try_model(self, model: str, client: AsyncOpenAI, prompt: str) -> str:
        for attempt in range(self.MAX_RETRIES):
            try:
                response = await client.chat.completions.create(
                    model=model,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.3,
                    max_tokens=4000,
                )
                msg = response.choices[0].message
                text = msg.content or ""
                if not text.strip() and hasattr(msg, "reasoning_content") and msg.reasoning_content:
                    text = msg.reasoning_content
                if text and text.strip():
                    return text.strip()
                logger.warning(
                    "Model %s attempt %d/%d returned empty content",
                    model, attempt + 1, self.MAX_RETRIES,
                )
            except Exception as exc:
                cause = exc.__cause__ or exc.__context__
                cause_info = f" (caused by {type(cause).__name__}: {cause})" if cause else ""
                logger.warning(
                    "Model %s attempt %d/%d failed: %s: %s%s",
                    model, attempt + 1, self.MAX_RETRIES,
                    type(exc).__name__, exc, cause_info,
                )
                if attempt < self.MAX_RETRIES - 1:
                    await asyncio.sleep(2**attempt)
        raise EnrichmentError(f"Model {model!r} exhausted after {self.MAX_RETRIES} retries")

    def _build_prompt(
        self, bookmark: Bookmark, content: ExtractedContent | None
    ) -> str:
        source_label = "tweet" if bookmark.source == Source.X else "post"
        is_tweet = bookmark.source == Source.X

        parts = [
            "You are a knowledge curator. Summarize the following content in exactly 3",
            "insightful bullets that capture the key ideas, followed by 1 takeaway sentence,",
            "and 3-5 lowercase tags.",
            "",
        ]

        if is_tweet:
            parts.extend([
                "This is a TWEET (possibly a thread). Focus on the actual ideas,",
                " NOT on any error messages or UI text. Ignore phrases like",
                ' "Something went wrong" or "privacy related extensions".',
                " Summarize the tweet content, not any error pages.",
                " If the tweet references an external article or link, summarize",
                " what the tweet SAYS about it — do not fabricate article content.",
                "",
            ])
        else:
            parts.extend([
                "This is a bookmarked article/post. Summarize the core ideas.",
                "",
            ])

        parts.extend([
            f"Title: {bookmark.title}",
            f"URL: {bookmark.url}",
        ])

        if content and content.full_text and content.extraction_method == "trafilatura":
            parts.append(f"\nContent:\n{content.full_text}")
        elif bookmark.post_text:
            parts.append(f"\n{source_label.capitalize()} text: {bookmark.post_text}")
        else:
            parts.append("\n(No additional content available)")

        if bookmark.external_urls:
            parts.append("\nExternal links referenced:")
            for url in bookmark.external_urls:
                parts.append(f"- {url}")

        parts.extend([
            "",
            "Respond in this EXACT format:",
            "SUMMARY:",
            "- bullet 1",
            "- bullet 2",
            "- bullet 3",
            "TAKEAWAY: one sentence",
            "TAGS: tag1, tag2, tag3",
        ])
        return "\n".join(parts)

    def _parse_response(self, text: str) -> dict:
        summary_match = re.search(
            r"SUMMARY:\s*\n((?:\s*-.*\n?)+)", text, re.IGNORECASE
        )
        takeaway_match = re.search(
            r"TAKEAWAY:[ \t]*([^\n]+)", text, re.IGNORECASE
        )
        tags_match = re.search(r"TAGS:\s*(.+)", text, re.IGNORECASE)

        summary_bullets = []
        if summary_match:
            summary_bullets = [
                line.lstrip("- ").strip()
                for line in summary_match.group(1).strip().splitlines()
                if line.strip().startswith("-")
            ]

        takeaway = takeaway_match.group(1).strip() if takeaway_match else ""

        tags = []
        if tags_match:
            tags = [
                t.strip().lower()
                for t in tags_match.group(1).split(",")
                if t.strip()
            ]

        return {
            "summary_bullets": summary_bullets,
            "takeaway": takeaway,
            "tags": tags,
        }

    async def enrich(
        self, bookmark: Bookmark, content: ExtractedContent | None = None
    ) -> EnrichedBookmark:
        source = bookmark.source.value
        prompt = self._build_prompt(bookmark, content)
        last_error: EnrichmentError | None = None

        for model, client in self._endpoints:
            await self._rate_limit(source)
            try:
                raw = await self._try_model(model, client, prompt)
                parsed = self._parse_response(raw)

                bullets = parsed["summary_bullets"]
                if len(bullets) < 3:
                    while len(bullets) < 3:
                        bullets.append(f"Key point from {bookmark.source.value}: {bookmark.title[:80]}")

                takeaway = parsed["takeaway"]
                if not takeaway and raw.strip():
                    first_sentence = raw.split(".")[0].strip()
                    if first_sentence:
                        takeaway = first_sentence[:200]

                enrichment = Enrichment(
                    summary_bullets=bullets[:3],
                    takeaway=takeaway,
                    tags=parsed["tags"],
                    model_used=model,
                    tokens=len(raw.split()),
                )
                return EnrichedBookmark(
                    bookmark=bookmark,
                    content=content or ExtractedContent(
                        url=bookmark.url,
                        post_text=bookmark.post_text,
                        extraction_method="post_only",
                    ),
                    enrichment=enrichment,
                )
            except EnrichmentError as exc:
                last_error = exc
                continue

        raise EnrichmentError(
            f"All models exhausted. Last error: {last_error}"
        )
