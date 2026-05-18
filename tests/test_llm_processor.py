"""Tests for llm_processor.py."""

import time
from unittest.mock import AsyncMock, patch

import pytest

from pipeline.llm import EnrichmentError, LLMProcessor
from models import Bookmark, ExtractedContent, Source


def _make_response(text: str) -> object:
    """Create a mock OpenAI response object."""

    class Msg:
        content = text

    class Choice:
        message = Msg()

    class Resp:
        choices = [Choice()]

    return Resp()


def _make_bookmark(**kwargs) -> Bookmark:
    defaults = {
        "source": "x",
        "url": "https://example.com/article",
        "title": "Test Article",
        "post_text": "Great post about architecture",
    }
    defaults.update(kwargs)
    return Bookmark(**defaults)


def _make_content(**kwargs) -> ExtractedContent:
    defaults = {
        "url": "https://example.com/article",
        "full_text": "This is the full article text about architecture.",
        "extraction_method": "trafilatura",
    }
    defaults.update(kwargs)
    return ExtractedContent(**defaults)


SUCCESS_RESPONSE = """SUMMARY:
- Bullet point one about the topic
- Bullet point two with more detail
- Bullet point three for completeness

TAKEAWAY: This is the key takeaway sentence.

TAGS: python, architecture, testing"""


@pytest.fixture
def processor():
    with patch("pipeline.llm.LLM_BASE_URL", ""), \
         patch("pipeline.llm.LLM_API_KEY", ""):
        p = LLMProcessor()
    mock_client = AsyncMock()
    p._endpoints = [
        ("kimi-k2.6", mock_client),
        ("deepseek-v4-pro", mock_client),
        ("deepseek-v4-flash", mock_client),
    ]
    p.client = mock_client
    return p


class TestPromptBuilder:
    def test_prompt_with_article_and_post(self, processor):
        bookmark = _make_bookmark()
        content = _make_content()
        prompt = processor._build_prompt(bookmark, content)

        assert "Title: Test Article" in prompt
        assert "URL: https://example.com/article" in prompt
        assert "This is the full article text" in prompt

    def test_prompt_with_post_only(self, processor):
        bookmark = _make_bookmark()
        prompt = processor._build_prompt(bookmark, None)

        assert "Great post about architecture" in prompt
        assert "TWEET" in prompt or "tweet" in prompt

    def test_prompt_with_empty_full_text(self, processor):
        bookmark = _make_bookmark()
        content = _make_content(full_text="")
        prompt = processor._build_prompt(bookmark, content)

        assert "Great post about architecture" in prompt

    def test_prompt_format_sections(self, processor):
        bookmark = _make_bookmark()
        prompt = processor._build_prompt(bookmark, None)

        assert "SUMMARY:" in prompt
        assert "TAKEAWAY:" in prompt
        assert "TAGS:" in prompt


class TestResponseParser:
    def test_parse_standard_response(self, processor):
        result = processor._parse_response(SUCCESS_RESPONSE)

        assert len(result["summary_bullets"]) == 3
        assert "Bullet point one" in result["summary_bullets"][0]
        assert "key takeaway" in result["takeaway"]
        assert result["tags"] == ["python", "architecture", "testing"]

    def test_parse_extra_whitespace(self, processor):
        text = """
        SUMMARY:
          -  bullet one
        - bullet two
          -   bullet three

        TAKEAWAY:    spaced out

        TAGS:  tag1 ,  tag2  , tag3
        """
        result = processor._parse_response(text)

        assert len(result["summary_bullets"]) == 3
        assert result["takeaway"] == "spaced out"
        assert result["tags"] == ["tag1", "tag2", "tag3"]

    def test_parse_empty_sections(self, processor):
        text = "SUMMARY:\n\nTAKEAWAY:\n\nTAGS:"
        result = processor._parse_response(text)

        assert result["summary_bullets"] == []
        assert result["takeaway"] == ""
        assert result["tags"] == []

    def test_parse_lowercase_section_headers(self, processor):
        text = """summary:
- bullet one
- bullet two
- bullet three

takeaway: the takeaway

tags: one, two, three"""
        result = processor._parse_response(text)

        assert len(result["summary_bullets"]) == 3
        assert result["takeaway"] == "the takeaway"
        assert len(result["tags"]) == 3


class TestEnrichSuccess:
    @pytest.mark.asyncio
    async def test_single_model_success(self, processor):
        processor.client.chat.completions.create.side_effect = lambda *a, **k: _make_response(
            SUCCESS_RESPONSE
        )

        bookmark = _make_bookmark()
        result = await processor.enrich(bookmark)

        assert result.bookmark == bookmark
        assert result.enrichment is not None
        assert len(result.enrichment.summary_bullets) == 3
        assert result.enrichment.model_used == "kimi-k2.6"

    @pytest.mark.asyncio
    async def test_enrich_with_extracted_content(self, processor):
        processor.client.chat.completions.create.side_effect = lambda *a, **k: _make_response(
            SUCCESS_RESPONSE
        )

        bookmark = _make_bookmark()
        content = _make_content()
        result = await processor.enrich(bookmark, content)

        assert result.content == content
        assert result.enrichment is not None

    @pytest.mark.asyncio
    async def test_enrich_no_content_fallback(self, processor):
        processor.client.chat.completions.create.side_effect = lambda *a, **k: _make_response(
            SUCCESS_RESPONSE
        )

        bookmark = _make_bookmark()
        result = await processor.enrich(bookmark, None)

        assert result.content.extraction_method == "post_only"


class TestFallbackChain:
    @pytest.mark.asyncio
    async def test_fallback_on_429(self, processor):
        """First model gets 429 (3 retries), second model succeeds."""
        call_count = 0

        async def side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count <= 3:
                raise Exception("429 Rate limit exceeded")
            return _make_response(SUCCESS_RESPONSE)

        processor.client.chat.completions.create.side_effect = side_effect

        bookmark = _make_bookmark()
        result = await processor.enrich(bookmark)

        assert result.enrichment is not None
        assert result.enrichment.model_used == "deepseek-v4-pro"
        assert call_count == 4  # 3 failures on model 1 + 1 success on model 2

    @pytest.mark.asyncio
    async def test_fallback_to_third_model(self, processor):
        """First two models fail (3 retries each), third model succeeds."""
        call_count = 0

        async def side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count <= 6:
                raise Exception("Server error")
            return _make_response(SUCCESS_RESPONSE)

        processor.client.chat.completions.create.side_effect = side_effect

        bookmark = _make_bookmark()
        result = await processor.enrich(bookmark)

        assert result.enrichment is not None
        assert result.enrichment.model_used == "deepseek-v4-flash"
        assert call_count == 7  # 3+3 failures + 1 success

    @pytest.mark.asyncio
    async def test_all_models_fail_raises(self, processor):
        """All models exhausted raises EnrichmentError."""
        processor.client.chat.completions.create.side_effect = Exception(
            "Connection refused"
        )

        bookmark = _make_bookmark()

        with pytest.raises(EnrichmentError, match="All models exhausted"):
            await processor.enrich(bookmark)


class TestBackoffTiming:
    @pytest.mark.asyncio
    async def test_backoff_delays(self, processor):
        """Verify exponential backoff: 1s, 2s, 4s between retries."""
        call_times = []

        async def side_effect(*args, **kwargs):
            call_times.append(time.monotonic())
            raise Exception("fail")

        processor.client.chat.completions.create.side_effect = side_effect

        with pytest.raises(EnrichmentError):
            await processor._try_model("test-model", processor.client, "prompt")

        assert len(call_times) == 3
        delay_1 = call_times[1] - call_times[0]
        delay_2 = call_times[2] - call_times[1]
        assert delay_1 >= 0.9, f"Delay 1 too small: {delay_1}"
        assert delay_2 >= 1.9, f"Delay 2 too small: {delay_2}"


class TestRateLimiter:
    @pytest.mark.asyncio
    async def test_rate_limiter_spacing(self, processor):
        """Verify requests are spaced >= 1 second apart."""
        call_times = []

        async def side_effect(*args, **kwargs):
            call_times.append(time.monotonic())
            return _make_response(SUCCESS_RESPONSE)

        processor.client.chat.completions.create.side_effect = side_effect

        bookmarks = [_make_bookmark() for _ in range(3)]
        for bm in bookmarks:
            await processor.enrich(bm)

        assert len(call_times) == 3
        gap_1 = call_times[1] - call_times[0]
        gap_2 = call_times[2] - call_times[1]
        assert gap_1 >= 0.9, f"Gap 1 too small: {gap_1}"
        assert gap_2 >= 0.9, f"Gap 2 too small: {gap_2}"


class TestPerSourceRateLimiting:
    def test_rate_limit_dict_initialized(self):
        with patch("pipeline.llm.LLM_BASE_URL", ""), \
             patch("pipeline.llm.LLM_API_KEY", ""):
            p = LLMProcessor()
            assert isinstance(p._last_request_time, dict)
            assert len(p._last_request_time) == 0

    @pytest.mark.asyncio
    async def test_different_sources_no_cross_wait(self, processor):
        call_times = []

        async def side_effect(*args, **kwargs):
            call_times.append(time.monotonic())
            return _make_response(SUCCESS_RESPONSE)

        processor.client.chat.completions.create.side_effect = side_effect

        bm_x = _make_bookmark(source=Source.X)
        bm_li = _make_bookmark(source=Source.LINKEDIN, url="https://linkedin.com/post/1")

        result_x = await processor.enrich(bm_x)
        result_li = await processor.enrich(bm_li)

        assert result_x.enrichment is not None
        assert result_li.enrichment is not None
        assert "x" in processor._last_request_time
        assert "linkedin" in processor._last_request_time

    @pytest.mark.asyncio
    async def test_same_source_rate_limited(self, processor):
        call_times = []

        async def side_effect(*args, **kwargs):
            call_times.append(time.monotonic())
            return _make_response(SUCCESS_RESPONSE)

        processor.client.chat.completions.create.side_effect = side_effect

        bms = [_make_bookmark() for _ in range(3)]
        for bm in bms:
            await processor.enrich(bm)

        assert len(call_times) == 3
        assert call_times[1] - call_times[0] >= 0.9
        assert call_times[2] - call_times[1] >= 0.9


class TestModelResolution:
    def test_no_endpoints_when_creds_missing(self):
        with patch("pipeline.llm.LLM_BASE_URL", ""), \
             patch("pipeline.llm.LLM_API_KEY", ""):
            p = LLMProcessor()
            assert p._endpoints == []

    def test_env_overrides_models(self):
        with patch("pipeline.llm.LLM_MODEL", "gpt-4"), \
             patch("pipeline.llm.LLM_MODEL_FALLBACK_1", "gpt-3.5"), \
             patch("pipeline.llm.LLM_MODEL_FALLBACK_2", "claude"), \
             patch("pipeline.llm.LLM_BASE_URL", "https://api.test"), \
             patch("pipeline.llm.LLM_API_KEY", "key"), \
             patch("pipeline.llm._make_client", return_value=AsyncMock()):
            p = LLMProcessor()
            assert [m for m, _ in p._endpoints] == ["gpt-4", "gpt-3.5", "claude"]

    def test_partial_env_overrides(self):
        with patch("pipeline.llm.LLM_MODEL", "gpt-4"), \
             patch("pipeline.llm.LLM_MODEL_FALLBACK_1", ""), \
             patch("pipeline.llm.LLM_MODEL_FALLBACK_2", ""), \
             patch("pipeline.llm.LLM_BASE_URL", "https://api.test"), \
             patch("pipeline.llm.LLM_API_KEY", "key"), \
             patch("pipeline.llm._make_client", return_value=AsyncMock()):
            p = LLMProcessor()
            assert [m for m, _ in p._endpoints] == ["gpt-4"]
