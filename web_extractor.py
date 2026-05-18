"""Web article extractor for the PKM ingestion pipeline.

Fetches HTML from bookmark URLs and extracts clean article text using
trafilatura. Retries with exponential backoff on transient failures.
"""

import asyncio
import logging
from datetime import UTC, datetime

import httpx
import trafilatura

from config import FETCH_BACKOFF_BASE, FETCH_MAX_RETRIES, FETCH_TIMEOUT, MAX_REDIRECTS
from models import ExtractedContent

logger = logging.getLogger(__name__)


async def extract(url: str, post_text: str | None = None) -> ExtractedContent | None:
    """Fetch and extract clean article text from a URL.

    Returns ExtractedContent on success, None on failure.
    """
    logger.info("Extracting article: %s", url)

    html = await _fetch_with_retry(url)
    if html is None:
        logger.warning("Failed to fetch HTML for %s", url)
        return None

    return _run_trafilatura(url, html, post_text)


async def _fetch_with_retry(url: str) -> str | None:
    """Fetch HTML with exponential-backoff retry on transient errors."""
    for attempt in range(FETCH_MAX_RETRIES):
        retryable, html = await _fetch_html(url)
        if html is not None:
            return html
        if not retryable:
            return None
        if attempt < FETCH_MAX_RETRIES - 1:
            delay = FETCH_BACKOFF_BASE * (2 ** attempt)
            logger.info("Retry %d/%d for %s in %.1fs", attempt + 1, FETCH_MAX_RETRIES, url, delay)
            await asyncio.sleep(delay)

    logger.warning("All %d retries exhausted for %s", FETCH_MAX_RETRIES, url)
    return None


async def _fetch_html(url: str) -> tuple[bool, str | None]:
    """Fetch raw HTML. Returns (retryable, html_or_None)."""
    try:
        async with httpx.AsyncClient(
            timeout=FETCH_TIMEOUT,
            follow_redirects=True,
            max_redirects=MAX_REDIRECTS,
        ) as client:
            response = await client.get(url)
            response.raise_for_status()
            return False, response.text
    except httpx.TooManyRedirects:
        logger.warning("Redirect chain exceeded %d for %s", MAX_REDIRECTS, url)
        return False, None
    except httpx.TimeoutException:
        logger.warning("Timeout fetching %s after %.1fs", url, FETCH_TIMEOUT)
        return True, None
    except httpx.HTTPStatusError as exc:
        retryable = exc.response.status_code >= 500
        logger.warning("HTTP %d for %s (%s)", exc.response.status_code, url,
                       "retryable" if retryable else "non-retryable")
        return retryable, None
    except httpx.RequestError as exc:
        logger.warning("Request error for %s: %s", url, exc)
        return True, None
    except Exception:
        logger.exception("Unexpected error fetching %s", url)
        return False, None


def _run_trafilatura(
    url: str,
    html: str,
    post_text: str | None = None,
) -> ExtractedContent | None:
    """Run trafilatura extraction on HTML. Returns None if no useful text found."""
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
        logger.warning("trafilatura returned empty content for %s", url)
        return None

    return ExtractedContent(
        url=url,
        full_text=full_text.strip(),
        post_text=post_text,
        extraction_method="trafilatura",
        extracted_at=datetime.now(UTC),
    )
