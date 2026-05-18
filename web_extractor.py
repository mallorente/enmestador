"""Web article extractor for the PKM ingestion pipeline.

Fetches HTML from bookmark URLs and extracts clean article text using
trafilatura. Returns ExtractedContent on success or None on failure.
Includes retry with exponential backoff for transient failures.
"""

import asyncio
import logging
from datetime import UTC, datetime

import httpx
import trafilatura

from config import FETCH_BACKOFF_BASE, FETCH_MAX_RETRIES, FETCH_TIMEOUT, MAX_REDIRECTS
from models import ExtractedContent

logger = logging.getLogger(__name__)





async def _fetch_with_retry(url: str, max_retries: int = FETCH_MAX_RETRIES) -> str | None:
    """Fetch HTML with exponential-backoff retry on transient errors.

    Retries on: TimeoutException, HTTP 5xx, RequestError.
    Does NOT retry on: HTTP 4xx, TooManyRedirects.
    """
    for attempt in range(max_retries):
        try:
            return await _fetch_html(url)
        except _RetryableError:
            if attempt < max_retries - 1:
                delay = FETCH_BACKOFF_BASE * (2 ** attempt)
                logger.info("Retry %d/%d for %s in %.1fs", attempt + 1, max_retries, url, delay)
                await asyncio.sleep(delay)
                continue
            logger.warning("All %d retries exhausted for %s", max_retries, url)
            return None
        except _NonRetryableError:
            return None


async def extract(url: str, post_text: str | None = None) -> ExtractedContent | None:
    """Fetch and extract clean article text from a URL.

    Args:
        url: The article URL to fetch and extract.
        post_text: Original post text to include if trafilatura fails.

    Returns:
        ExtractedContent on success, None on failure (logged as WARNING).
    """
    logger.info("Extracting article: %s", url)

    html = await _fetch_with_retry(url)
    if html is None:
        logger.warning("Failed to fetch HTML for %s", url)
        return None

    extracted = _run_trafilatura(url, html, post_text)
    if extracted is None:
        logger.warning("trafilatura returned empty content for %s", url)
        return None

    return extracted


class _RetryableError(Exception):
    """Transient error worth retrying."""


class _NonRetryableError(Exception):
    """Permanent error — not worth retrying."""


async def _fetch_html(url: str) -> str:
    """Fetch raw HTML from a URL with timeout and redirect limits.

    Returns the HTML string on success.
    Raises _RetryableError on transient failures (timeout, 5xx, network).
    Raises _NonRetryableError on permanent failures (4xx, too many redirects).
    """
    try:
        async with httpx.AsyncClient(
            timeout=FETCH_TIMEOUT,
            follow_redirects=True,
            max_redirects=MAX_REDIRECTS,
        ) as client:
            response = await client.get(url)
            response.raise_for_status()
            return response.text
    except httpx.TooManyRedirects:
        logger.warning("Redirect chain exceeded %d for %s", MAX_REDIRECTS, url)
        raise _NonRetryableError(f"Too many redirects for {url}") from None
    except httpx.TimeoutException:
        logger.warning("Timeout fetching %s after %.1fs", url, FETCH_TIMEOUT)
        raise _RetryableError(f"Timeout fetching {url}") from None
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code >= 500:
            logger.warning("Server error %s for %s (retryable)", exc.response.status_code, url)
            raise _RetryableError(f"HTTP {exc.response.status_code} for {url}") from None
        logger.warning("Client error %s for %s (non-retryable)", exc.response.status_code, url)
        raise _NonRetryableError(f"HTTP {exc.response.status_code} for {url}") from None
    except httpx.RequestError as exc:
        logger.warning("Request error for %s: %s", url, exc)
        raise _RetryableError(f"Request error for {url}: {exc}") from None
    except Exception:
        logger.exception("Unexpected error fetching %s", url)
        raise _NonRetryableError(f"Unexpected error fetching {url}") from None


def _run_trafilatura(
    url: str,
    html: str,
    post_text: str | None = None,
) -> ExtractedContent | None:
    """Run trafilatura extraction on HTML content.

    Returns ExtractedContent or None if extraction yields no useful text.
    """
    try:
        full_text = trafilatura.extract(
            html,
            url=url,
            include_comments=False,
            include_tables=True,
            favor_precision=True,
        )
    except Exception:
        logger.exception("trafilatura extraction failed for %s", url)
        return None

    if not full_text or not full_text.strip():
        return None

    return ExtractedContent(
        url=url,
        full_text=full_text.strip(),
        post_text=post_text,
        extraction_method="trafilatura",
        extracted_at=datetime.now(UTC),
    )
