"""X.com bookmark scraper for the PKM ingestion pipeline.

Intercepts GraphQL Bookmarks responses via Playwright route interception,
parses raw JSON into Bookmark objects, and supports cursor-based pagination.
"""

import json
import logging
from pathlib import Path

from playwright.async_api import Page, Response

from models import Bookmark, ScrapeMode, Source
from state import CursorsStore, ProcessedUrlStore

logger = logging.getLogger(__name__)

# GraphQL route pattern for X.com Bookmarks
GRAPHQL_PATTERN = "**/graphql*"
BOOKMARKS_QUERY_NAME = "Bookmarks"

# Default max bookmarks per run
DEFAULT_MAX_BOOKMARKS = 500


def _parse_graphql_response(body: str) -> tuple[list[dict], str | None]:
    """Parse a GraphQL Bookmarks response into bookmark dicts and next cursor.

    Returns (bookmarks, next_cursor).
    Handles the v2 nested structure: data.bookmark_timeline_v2.timeline.instructions
    """
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return [], None

    # Navigate the nested GraphQL response structure
    # X API v2: data.bookmark_timeline_v2.timeline.instructions
    # Fallback (older): data.user.bookmark_timeline.timeline.instructions
    top_level = data.get("data", {})
    timeline_data = (
        top_level.get("bookmark_timeline_v2", {})
        or top_level.get("user", {}).get("bookmark_timeline", {})
    )
    instructions = (
        timeline_data.get("timeline", {})
        .get("instructions", [])
    )

    if not instructions:
        return [], None

    entries: list[dict] = []
    cursor: str | None = None

    # Instructions can be a list of objects with "type" + "entries",
    # OR a single flat list of entries (v2 format wraps entries directly)
    for instruction in instructions:
        if isinstance(instruction, dict):
            if instruction.get("type") == "TimelineAddEntries":
                entries.extend(instruction.get("entries", []))
            elif instruction.get("type") == "TimelineTerminateTimeline":
                direction = instruction.get("direction", "")
                if direction == "Bottom":
                    cursor = None
            else:
                # v2 format: entries may be nested under instruction directly
                instruction_entries = instruction.get("entries", [])
                if instruction_entries:
                    entries.extend(instruction_entries)

    bookmarks: list[dict] = []
    for entry in entries:
        entry_id = entry.get("entryId", "")
        if entry_id.startswith("tweet-"):
            # v2 format: entry.content.itemContent.tweet_results.result
            content = entry.get("content", {})
            item_content = content.get("itemContent", {})
            # Some entries wrap result under content -> itemContent -> tweet_results
            result = item_content.get("tweet_results", {}).get("result", {})
            # Also handle content.result directly (some v2 responses)
            if not result:
                result = content.get("result", {})
            if result:
                bookmarks.append(result)
        elif entry_id.startswith("cursor-bottom-"):
            cursor = entry.get("content", {}).get("value")
            if not cursor:
                # v2 format: cursor can be in itemContent
                cursor = entry.get("content", {}).get("itemContent", {}).get("value")

    return bookmarks, cursor


def _extract_author_handle(tweet: dict) -> str:
    """Extract the author's screen_name from a tweet result object.

    X API v2 structure: tweet.core.user_results.result.core.screen_name
    """
    core = tweet.get("core", {})
    user_results = core.get("user_results", {})
    result = user_results.get("result", {})

    # Primary path: result.core.screen_name (confirmed X API v2 structure)
    result_core = result.get("core", {})
    if isinstance(result_core, dict):
        sn = result_core.get("screen_name")
        if sn and isinstance(sn, str) and sn.strip():
            return sn.strip()

    # Fallback paths for other possible API versions
    # result.screen_name (top-level)
    sn = result.get("screen_name")
    if sn and isinstance(sn, str) and sn.strip():
        return sn.strip()

    # result.legacy.screen_name
    user_legacy = result.get("legacy", {})
    sn = user_legacy.get("screen_name")
    if sn and isinstance(sn, str) and sn.strip():
        return sn.strip()

    # legacy.user.screen_name (older API format)
    tweet_legacy = tweet.get("legacy", {})
    user_obj = tweet_legacy.get("user", {})
    if isinstance(user_obj, dict):
        sn = user_obj.get("screen_name")
        if sn and isinstance(sn, str) and sn.strip():
            return sn.strip()

    # Direct user_results at tweet level (some API response shapes skip core wrapper)
    user_results_direct = tweet.get("user_results", {})
    if user_results_direct:
        result_direct = user_results_direct.get("result", {})
        sn = (
            result_direct.get("core", {}).get("screen_name")
            or result_direct.get("legacy", {}).get("screen_name")
        )
        if sn and isinstance(sn, str) and sn.strip():
            return sn.strip()

    logger.warning(
        "screen_name extraction failed for tweet. result_core_keys=%s, result_keys=%s",
        list(result_core.keys()) if isinstance(result_core, dict) else type(result_core).__name__,
        list(result.keys()) if isinstance(result, dict) else type(result).__name__,
    )
    return "unknown"


def _extract_external_urls(tweet: dict) -> list[str]:
    """Extract external (non-t.co/X-internal) URLs from a tweet.

    Resolves t.co shortlinks to their full URLs using the entities data.
    Returns a list of resolved external URLs.
    """
    legacy = tweet.get("legacy", {})
    entities = legacy.get("entities", {})
    urls = entities.get("urls", [])

    external = []
    for url_obj in urls:
        expanded = url_obj.get("expanded_url", "")
        # Skip X-internal URLs (photos, videos, quotes of same tweet)
        if not expanded:
            continue
        if "/status/" in expanded and "x.com" in expanded:
            continue
        if "pic.x.com" in expanded or "pbs.twimg.com" in expanded:
            continue
        external.append(expanded)

    # Also extract URLs from full_text that weren't in entities
    # (t.co links that appear as raw text)
    import re as _re
    full_text = legacy.get("full_text", "")
    tco_pattern = _re.compile(r"https?://t\.co/\w+")
    text_tcos = set(tco_pattern.findall(full_text))
    entity_tcos = {u.get("url", "") for u in urls}
    unresolved = text_tcos - entity_tcos
    # We can't resolve these without an HTTP call, store t.co as fallback
    for tco in unresolved:
        external.append(tco)

    return external


def _bookmark_from_graphql(raw: dict) -> Bookmark | None:
    """Convert a raw GraphQL tweet result into a Bookmark.

    Handles both direct result and nested tweet wrapper (retweet/quote scenarios).
    """
    try:
        tweet = raw.get("tweet", raw)
        legacy = tweet.get("legacy", {})
        author_handle = _extract_author_handle(tweet)

        tweet_id = tweet.get("rest_id", raw.get("rest_id", ""))
        post_text = legacy.get("full_text", "")
        external_urls = _extract_external_urls(tweet)
        url = f"https://x.com/{author_handle}/status/{tweet_id}"

        return Bookmark(
            source=Source.X,
            url=url,
            title=post_text[:120] if post_text else f"Post by @{author_handle}",
            post_text=post_text or None,
            external_urls=external_urls if external_urls else None,
            saved_at=None,
        )
    except Exception:
        logger.exception("Failed to parse bookmark from GraphQL tweet")
        return None


class ScraperX:
    """Scrape bookmarks from X.com via GraphQL interception.

    Uses Playwright response interception to capture Bookmarks query results,
    supporting both bootstrap (full pagination) and delta (resume from cursor)
    modes.
    """

    def __init__(
        self,
        page: Page,
        state_dir: Path,
        max_bookmarks: int = DEFAULT_MAX_BOOKMARKS,
    ) -> None:
        self.page = page
        self.max_bookmarks = max_bookmarks
        self.cursors = CursorsStore(state_dir)
        self.processed = ProcessedUrlStore(state_dir)

    async def _process_captured(
        self,
        responses: list[Response],
        results: list[Bookmark],
        seen_urls: set[str],
        processed_urls: dict[str, object],
    ) -> str | None:
        """Process captured GraphQL responses into Bookmark objects.

        Returns the next cursor, or None if no more pages or no bookmarks found.
        """
        next_cursor: str | None = None

        for response in responses:
            try:
                body = await response.text()
            except Exception:
                logger.debug("Failed to read response body from %s", response.url)
                continue

            bms, cursor = _parse_graphql_response(body)
            if bms:
                for raw in bms:
                    if len(results) >= self.max_bookmarks:
                        break
                    bm = _bookmark_from_graphql(raw)
                    if bm and str(bm.url) not in processed_urls and str(bm.url) not in seen_urls:
                        results.append(bm)
                        seen_urls.add(str(bm.url))
                if cursor:
                    next_cursor = cursor

        return next_cursor

    async def scrape(self, mode: ScrapeMode) -> list[Bookmark]:
        """Scrape bookmarks from X.com.

        Args:
            mode: BOOTSTRAP to paginate from start, DELTA to resume from cursor.

        Returns:
            List of Bookmark objects, skipping already-processed URLs.
        """
        results: list[Bookmark] = []
        seen_urls: set[str] = set()
        processed_urls = self.processed.load()
        cursor: str | None = None

        if mode == ScrapeMode.DELTA:
            cursor_data = self.cursors.get(Source.X)
            cursor = cursor_data.get("cursor")
            if not cursor:
                logger.info("Delta mode requested but no cursor found, falling back to bootstrap")
                mode = ScrapeMode.BOOTSTRAP

        logger.info("Starting X scraper: mode=%s, cursor=%s", mode.value, cursor)

        # Set up response interception — store Response objects for later
        # async processing (response.body() is async and can't be called
        # from a synchronous event handler).
        captured_responses: list[Response] = []

        def _on_response(response: Response) -> None:
            url = response.url
            # Log ALL graphql URLs regardless of match
            if "graphql" in url.lower():
                logger.info("GRAPHQL URL: %s", url[:200])
            # Case-insensitive: X.com may use "Bookmarks" or "bookmarks"
            if "graphql" in url.lower() and BOOKMARKS_QUERY_NAME.lower() in url.lower():
                logger.info("CAPTURED Bookmarks: %s", url[:200])
                captured_responses.append(response)

        self.page.on("response", _on_response)

        try:
            # Navigate to bookmarks page (X.com serves both /bookmarks and /i/bookmarks)
            # domcontentloaded: networkidle never fires on X.com (endless analytics)
            await self.page.goto(
                "https://x.com/i/bookmarks",
                wait_until="domcontentloaded",
                timeout=60000,
            )
            # Wait for GraphQL requests to fire and complete
            await self.page.wait_for_timeout(8000)

            # Process captured responses (in async context where we can await)
            cursor = await self._process_captured(captured_responses, results, seen_urls, processed_urls)
            logger.debug("After initial load: %d bookmarks, cursor=%s", len(results), cursor)
            captured_responses.clear()

            # Pagination loop
            while cursor and len(results) < self.max_bookmarks:
                # Scroll to trigger next page load
                await self.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await self.page.wait_for_timeout(3000)

                cursor = await self._process_captured(captured_responses, results, seen_urls, processed_urls)
                if not cursor:
                    break  # No more data
                captured_responses.clear()

        except Exception:
            logger.exception("X scraper encountered an error")

        # Save cursor
        if cursor:
            self.cursors.save(Source.X, cursor, ScrapeMode.DELTA.value)
            logger.info("Saved X cursor: %s", cursor)

        logger.info("X scraper complete: %d bookmarks", len(results))
        return results
