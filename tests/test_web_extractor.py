"""Tests for web_extractor.py — retry logic, mocked HTTP responses and trafilatura."""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from models import ExtractedContent
from web_extractor import _fetch_html, _fetch_with_retry, _run_trafilatura, _NonRetryableError, _RetryableError, extract


def _mock_client(return_value=None, side_effect=None):
    """Create a mock AsyncClient that can be used as context manager."""
    mock_client = AsyncMock()
    if side_effect is not None:
        mock_client.get = AsyncMock(side_effect=side_effect)
    else:
        mock_client.get = AsyncMock(return_value=return_value)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    return mock_client


class TestFetchHtml:
    """Unit tests for _fetch_html (now raises typed exceptions)."""

    @pytest.mark.asyncio
    async def test_successful_fetch(self) -> None:
        """Fetch returns HTML string on 200 response."""
        mock_response = MagicMock()
        mock_response.text = "<html><body><h1>Test Article</h1></body></html>"
        mock_response.raise_for_status = MagicMock()

        with patch("web_extractor.httpx.AsyncClient", return_value=_mock_client(return_value=mock_response)):
            result = await _fetch_html("https://example.com/article")
            assert result is not None
            assert "Test Article" in result

    @pytest.mark.asyncio
    async def test_timeout_raises_retryable(self) -> None:
        """Timeout raises _RetryableError."""
        with patch("web_extractor.httpx.AsyncClient", return_value=_mock_client(side_effect=httpx.TimeoutException("timed out"))):
            with pytest.raises(_RetryableError):
                await _fetch_html("https://slow-site.com/article")

    @pytest.mark.asyncio
    async def test_http_404_raises_non_retryable(self) -> None:
        """HTTP 404 raises _NonRetryableError."""
        mock_response = MagicMock()
        mock_response.status_code = 404
        error = httpx.HTTPStatusError("Not Found", request=MagicMock(), response=mock_response)

        with patch("web_extractor.httpx.AsyncClient", return_value=_mock_client(side_effect=error)):
            with pytest.raises(_NonRetryableError):
                await _fetch_html("https://example.com/missing")

    @pytest.mark.asyncio
    async def test_http_500_raises_retryable(self) -> None:
        """HTTP 500 raises _RetryableError."""
        mock_response = MagicMock()
        mock_response.status_code = 500
        error = httpx.HTTPStatusError("Internal Server Error", request=MagicMock(), response=mock_response)

        with patch("web_extractor.httpx.AsyncClient", return_value=_mock_client(side_effect=error)):
            with pytest.raises(_RetryableError):
                await _fetch_html("https://example.com/broken")

    @pytest.mark.asyncio
    async def test_http_502_raises_retryable(self) -> None:
        """HTTP 502 raises _RetryableError."""
        mock_response = MagicMock()
        mock_response.status_code = 502
        error = httpx.HTTPStatusError("Bad Gateway", request=MagicMock(), response=mock_response)

        with patch("web_extractor.httpx.AsyncClient", return_value=_mock_client(side_effect=error)):
            with pytest.raises(_RetryableError):
                await _fetch_html("https://example.com/down")

    @pytest.mark.asyncio
    async def test_too_many_redirects_raises_non_retryable(self) -> None:
        """TooManyRedirects raises _NonRetryableError."""
        with patch("web_extractor.httpx.AsyncClient", return_value=_mock_client(side_effect=httpx.TooManyRedirects("too many"))):
            with pytest.raises(_NonRetryableError):
                await _fetch_html("https://example.com/redirect-loop")

    @pytest.mark.asyncio
    async def test_request_error_raises_retryable(self) -> None:
        """Network error raises _RetryableError."""
        with patch("web_extractor.httpx.AsyncClient", return_value=_mock_client(side_effect=httpx.RequestError("DNS error", request=MagicMock()))):
            with pytest.raises(_RetryableError):
                await _fetch_html("https://nonexistent-domain.xyz")


class TestFetchWithRetry:
    """Tests for _fetch_with_retry — backoff and retry logic."""

    @pytest.mark.asyncio
    async def test_success_on_first_try(self) -> None:
        """No retries needed when first fetch succeeds."""
        with patch("web_extractor._fetch_html", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = "<html>content</html>"

            result = await _fetch_with_retry("https://example.com/article")
            assert result == "<html>content</html>"
            assert mock_fetch.call_count == 1

    @pytest.mark.asyncio
    async def test_retry_on_timeout_then_success(self) -> None:
        """Retries on TimeoutException and succeeds on second attempt."""
        with patch("web_extractor._fetch_html", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.side_effect = [_RetryableError("timeout"), "<html>content</html>"]

            result = await _fetch_with_retry("https://example.com/article")
            assert result == "<html>content</html>"
            assert mock_fetch.call_count == 2

    @pytest.mark.asyncio
    async def test_retry_on_500_then_success(self) -> None:
        """Retries on HTTP 500 and succeeds on second attempt."""
        with patch("web_extractor._fetch_html", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.side_effect = [_RetryableError("HTTP 500"), "<html>content</html>"]

            result = await _fetch_with_retry("https://example.com/article")
            assert result == "<html>content</html>"
            assert mock_fetch.call_count == 2

    @pytest.mark.asyncio
    async def test_no_retry_on_404(self) -> None:
        """HTTP 404 raises _NonRetryableError — returns None immediately."""
        with patch("web_extractor._fetch_html", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.side_effect = _NonRetryableError("HTTP 404")

            result = await _fetch_with_retry("https://example.com/missing")
            assert result is None
            assert mock_fetch.call_count == 1

    @pytest.mark.asyncio
    async def test_no_retry_on_redirect_loop(self) -> None:
        """TooManyRedirects — returns None immediately, no retry."""
        with patch("web_extractor._fetch_html", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.side_effect = _NonRetryableError("Too many redirects")

            result = await _fetch_with_retry("https://example.com/loop")
            assert result is None
            assert mock_fetch.call_count == 1

    @pytest.mark.asyncio
    async def test_max_retries_exhausted(self) -> None:
        """All 3 retries fail with timeout — returns None."""
        with patch("web_extractor._fetch_html", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.side_effect = [
                _RetryableError("timeout"),
                _RetryableError("timeout"),
                _RetryableError("timeout"),
            ]

            result = await _fetch_with_retry("https://example.com/slow")
            assert result is None
            assert mock_fetch.call_count == 3

    @pytest.mark.asyncio
    async def test_backoff_delays(self) -> None:
        """Backoff delays follow exponential pattern: 1s, 2s, 4s."""
        call_count = 0

        async def fake_fetch(url: str) -> str:
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                raise _RetryableError("timeout")
            return "<html>ok</html>"

        with patch("web_extractor._fetch_html", side_effect=fake_fetch):
            with patch("web_extractor.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
                result = await _fetch_with_retry("https://example.com/article")

        assert result == "<html>ok</html>"
        assert mock_sleep.call_count == 2
        # First retry: 1.0s, second retry: 2.0s
        assert mock_sleep.call_args_list[0][0][0] == 1.0
        assert mock_sleep.call_args_list[1][0][0] == 2.0

    @pytest.mark.asyncio
    async def test_retry_on_request_error_then_success(self) -> None:
        """Retries on RequestError and succeeds on second attempt."""
        with patch("web_extractor._fetch_html", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.side_effect = [_RetryableError("DNS error"), "<html>content</html>"]

            result = await _fetch_with_retry("https://example.com/article")
            assert result == "<html>content</html>"
            assert mock_fetch.call_count == 2


class TestRunTrafilatura:
    """Unit tests for trafilatura extraction."""

    def test_successful_extraction(self) -> None:
        """trafilatura extracts clean text from HTML."""
        html = """
        <html>
            <head><title>Test Article</title></head>
            <body>
                <article>
                    <h1>Test Article</h1>
                    <p>This is a great article about software architecture.</p>
                    <p>It discusses clean code principles.</p>
                </article>
            </body>
        </html>
        """
        with patch("trafilatura.extract") as mock_extract:
            mock_extract.return_value = (
                "This is a great article about software architecture. "
                "It discusses clean code principles."
            )

            result = _run_trafilatura("https://example.com/article", html)
            assert result is not None
            assert result.extraction_method == "trafilatura"
            assert "software architecture" in result.full_text
            assert result.post_text is None

    def test_includes_post_text(self) -> None:
        """trafilatura result includes original post_text."""
        html = "<html><body><p>Article content</p></body></html>"
        with patch("trafilatura.extract") as mock_extract:
            mock_extract.return_value = "Article content extracted."

            result = _run_trafilatura(
                "https://example.com/article",
                html,
                post_text="Original post text here",
            )
            assert result is not None
            assert result.post_text == "Original post text here"

    def test_empty_extraction_returns_none(self) -> None:
        """Empty trafilatura output returns None."""
        html = "<html><body></body></html>"
        with patch("trafilatura.extract") as mock_extract:
            mock_extract.return_value = ""

            result = _run_trafilatura("https://example.com/empty", html)
            assert result is None

    def test_whitespace_only_returns_none(self) -> None:
        """Whitespace-only trafilatura output returns None."""
        html = "<html><body>   \n  </body></html>"
        with patch("trafilatura.extract") as mock_extract:
            mock_extract.return_value = "   \n  "

            result = _run_trafilatura("https://example.com/whitespace", html)
            assert result is None

    def test_trafilatura_exception_returns_none(self) -> None:
        """trafilatura exception returns None."""
        html = "<html>corrupted</html>"
        with patch("trafilatura.extract") as mock_extract:
            mock_extract.side_effect = Exception("trafilatura crashed")

            result = _run_trafilatura("https://example.com/bad", html)
            assert result is None

    def test_sets_extracted_at(self) -> None:
        """ExtractedContent has extracted_at timestamp."""
        html = "<html><body><p>Content</p></body></html>"
        with patch("trafilatura.extract") as mock_extract:
            mock_extract.return_value = "Content"

            result = _run_trafilatura("https://example.com/article", html)
            assert result is not None
            assert result.extracted_at is not None
            assert isinstance(result.extracted_at, datetime)


class TestExtract:
    """Integration tests for the main extract function."""

    @pytest.mark.asyncio
    async def test_full_extraction_pipeline(self) -> None:
        """extract: fetch + trafilatura = ExtractedContent."""
        with (
            patch("web_extractor._fetch_with_retry") as mock_fetch,
            patch("web_extractor._run_trafilatura") as mock_trafilatura,
        ):
            mock_fetch.return_value = "<html><body>Article</body></html>"
            mock_trafilatura.return_value = ExtractedContent(
                url="https://example.com/article",
                full_text="Article content",
                post_text="Post text",
                extraction_method="trafilatura",
            )

            result = await extract("https://example.com/article", post_text="Post text")
            assert result is not None
            assert result.full_text == "Article content"
            mock_fetch.assert_called_once_with("https://example.com/article")

    @pytest.mark.asyncio
    async def test_fetch_failure_returns_none(self) -> None:
        """extract: fetch failure returns None."""
        with patch("web_extractor._fetch_with_retry") as mock_fetch:
            mock_fetch.return_value = None

            result = await extract("https://broken-site.com")
            assert result is None

    @pytest.mark.asyncio
    async def test_trafilatura_failure_returns_none(self) -> None:
        """extract: trafilatura failure returns None."""
        with (
            patch("web_extractor._fetch_with_retry") as mock_fetch,
            patch("web_extractor._run_trafilatura") as mock_trafilatura,
        ):
            mock_fetch.return_value = "<html>content</html>"
            mock_trafilatura.return_value = None

            result = await extract("https://js-heavy-site.com")
            assert result is None