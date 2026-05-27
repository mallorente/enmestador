"""X.com bookmark scraper for the PKM ingestion pipeline.

Intercepts GraphQL Bookmarks responses via Playwright route interception,
parses raw JSON into Bookmark objects, and supports cursor-based pagination.
"""

import json
import logging
import os
from datetime import datetime
from pathlib import Path

from patchright.async_api import Page, Response

from models import Bookmark, ScrapeMode, Source
from pipeline.frontier import KnownSequenceBoundary
from pipeline.state import CursorsStore, ProcessedUrlStore, normalize_url

logger = logging.getLogger(__name__)

# GraphQL route pattern for X.com Bookmarks
GRAPHQL_PATTERN = "**/graphql*"
BOOKMARKS_QUERY_NAME = "Bookmarks"

# Default max bookmarks per run
DEFAULT_MAX_BOOKMARKS = 500
API_TIMEOUT_SECONDS = int(os.getenv("X_API_TIMEOUT", "20"))
DOM_FALLBACK_TIMEOUT_SECONDS = int(os.getenv("X_DOM_TIMEOUT", "60"))


def _parse_x_datetime(value: object) -> datetime | None:
    """Parse X timestamp values into aware datetimes."""
    if not value or not isinstance(value, str):
        return None
    try:
        if value.endswith("Z"):
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        return datetime.strptime(value, "%a %b %d %H:%M:%S %z %Y")
    except ValueError:
        logger.debug("Could not parse X timestamp: %s", value)
        return None


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
        if "/status/" in expanded and ("x.com" in expanded or "twitter.com" in expanded):
            continue
        if "pic.x.com" in expanded or "pbs.twimg.com" in expanded:
            continue
        external.append(expanded)

    return external


def _normalize_tweet_url(url: str) -> str | None:
    """Return canonical x.com tweet URL for a status URL, else None."""
    import re as _re

    match = _re.search(r"https?://(?:www\.)?(?:x|twitter)\.com/([^/?#]+)/status/(\d+)", url)
    if not match:
        return None
    handle, status_id = match.groups()
    return f"https://x.com/{handle}/status/{status_id}"


def _extract_referenced_tweet_urls(tweet: dict, current_url: str | None = None) -> list[str]:
    """Extract linked/quoted tweet URLs separately from external web URLs."""
    legacy = tweet.get("legacy", {})
    entities = legacy.get("entities", {})
    urls = entities.get("urls", [])

    referenced: list[str] = []
    seen: set[str] = set()

    def _add(url: str | None) -> None:
        normalized = _normalize_tweet_url(url or "")
        if not normalized:
            return
        if current_url and normalized == current_url:
            return
        if normalized not in seen:
            seen.add(normalized)
            referenced.append(normalized)

    for url_obj in urls:
        if isinstance(url_obj, dict):
            _add(url_obj.get("expanded_url") or url_obj.get("url"))

    quoted = (
        tweet.get("quoted_status_result", {})
        .get("result", {})
    )
    if isinstance(quoted, dict) and quoted:
        quoted_id = quoted.get("rest_id")
        quoted_handle = _extract_author_handle(quoted)
        if quoted_id and quoted_handle != "unknown":
            _add(f"https://x.com/{quoted_handle}/status/{quoted_id}")

    return referenced


def _extract_image_urls(tweet: dict) -> list[str]:
    """Extract image URLs from X GraphQL tweet media entities."""
    legacy = tweet.get("legacy", {})
    media_items = []
    for key in ("extended_entities", "entities"):
        items = legacy.get(key, {}).get("media", [])
        if isinstance(items, list):
            media_items.extend(items)

    images: list[str] = []
    seen: set[str] = set()
    for media in media_items:
        if not isinstance(media, dict):
            continue
        media_url = media.get("media_url_https") or media.get("media_url")
        media_type = media.get("type")
        # Preserve photo URLs and video/GIF preview images when X exposes them.
        if media_url and media_type in {"photo", "animated_gif", "video", None}:
            url = str(media_url)
            if url not in seen:
                seen.add(url)
                images.append(url)
    return images


def _bookmark_from_graphql(raw: dict) -> Bookmark | None:
    """Convert a raw GraphQL tweet result into a Bookmark.

    Handles both direct result and nested tweet wrapper (retweet/quote scenarios).
    """
    try:
        tweet = raw.get("tweet", raw)
        legacy = tweet.get("legacy", {})
        author_handle = _extract_author_handle(tweet)

        tweet_id = tweet.get("rest_id", raw.get("rest_id", ""))
        note_tweet_text = (
            tweet.get("note_tweet", {})
            .get("note_tweet_results", {})
            .get("result", {})
            .get("text")
        )
        post_text = note_tweet_text or legacy.get("full_text", "")
        external_urls = _extract_external_urls(tweet)
        image_urls = _extract_image_urls(tweet)
        url = f"https://x.com/{author_handle}/status/{tweet_id}"
        referenced_tweet_urls = _extract_referenced_tweet_urls(tweet, url)
        published_at = _parse_x_datetime(legacy.get("created_at"))

        return Bookmark(
            source=Source.X,
            url=url,
            title=post_text[:120] if post_text else f"Post by @{author_handle}",
            post_text=post_text or None,
            external_urls=external_urls if external_urls else None,
            referenced_tweet_urls=referenced_tweet_urls if referenced_tweet_urls else None,
            image_urls=image_urls if image_urls else None,
            published_at=published_at,
            saved_at=None,
        )
    except Exception:
        logger.exception("Failed to parse bookmark from GraphQL tweet")
        return None


def _bookmark_from_dom_post(raw: dict) -> Bookmark | None:
    """Convert a DOM-extracted tweet dict into a Bookmark."""
    try:
        url = raw.get("url", "")
        text = raw.get("text", "")
        author = raw.get("author", "unknown")
        if not url:
            return None
        if "?" in url:
            url = url.split("?", 1)[0]

        published_at = _parse_x_datetime(raw.get("published_at"))
        external_urls = [
            u for u in raw.get("external_urls", [])
            if isinstance(u, str) and u and "/status/" not in u and "x.com" not in u
        ]
        referenced_tweet_urls = [
            normalized for u in raw.get("referenced_tweet_urls", [])
            if isinstance(u, str)
            for normalized in [_normalize_tweet_url(u)]
            if normalized and normalized != url
        ]
        image_urls = [
            u for u in raw.get("image_urls", [])
            if isinstance(u, str) and u.startswith("http")
        ]

        return Bookmark(
            source=Source.X,
            url=url,
            title=text[:120] if text else f"Post by @{author}",
            post_text=text or None,
            external_urls=external_urls if external_urls else None,
            referenced_tweet_urls=referenced_tweet_urls if referenced_tweet_urls else None,
            image_urls=image_urls if image_urls else None,
            published_at=published_at,
            saved_at=None,
        )
    except Exception:
        logger.exception("Failed to parse bookmark from DOM tweet: %s", raw)
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
        skip_processed: bool = True,
        persist_cursor: bool = True,
        known_urls: set[str] | None = None,
        stop_after_known: int = 0,
        frontier_size: int = 20,
    ) -> None:
        self.page = page
        self.max_bookmarks = max_bookmarks
        self.skip_processed = skip_processed
        self.persist_cursor = persist_cursor
        self.cursors = CursorsStore(state_dir)
        self.processed = ProcessedUrlStore(state_dir)
        self.last_cursor: str | None = None
        self.bookmarks_endpoint_statuses: list[int] = []
        self.boundary = KnownSequenceBoundary(
            known_urls=known_urls or set(),
            stop_after_known=stop_after_known,
            recent_limit=frontier_size,
        )

    @property
    def saw_authenticated_bookmarks_endpoint(self) -> bool:
        """Return True once X's Bookmarks GraphQL endpoint responds successfully."""
        return any(200 <= status < 400 for status in self.bookmarks_endpoint_statuses)

    _JS_INTERCEPT = """
    (() => {
        if (window.__x_intercept_installed) return;
        window.__x_intercept_installed = true;
        window.__x_responses = [];

        function isXApi(url) {
            return url && (url.includes('/i/api/') || url.toLowerCase().includes('graphql'));
        }

        const _origFetch = window.fetch.bind(window);
        window.fetch = function(input, init) {
            const url = (typeof input === 'string') ? input : (input && input.url) || '';
            return _origFetch(input, init).then(function(response) {
                if (isXApi(url)) {
                    const clone = response.clone();
                    clone.text().then(function(body) {
                        window.__x_responses.push({url: url, status: response.status, body: body});
                    }).catch(function() {});
                }
                return response;
            });
        };

        const _OrigXHR = window.XMLHttpRequest;
        function PatchedXHR() {
            const xhr = new _OrigXHR();
            let _url = '';
            const _origOpen = xhr.open.bind(xhr);
            xhr.open = function(method, url) {
                _url = url || '';
                return _origOpen.apply(xhr, arguments);
            };
            xhr.addEventListener('load', function() {
                if (isXApi(_url)) {
                    try {
                        window.__x_responses.push({
                            url: _url, status: xhr.status, body: xhr.responseText
                        });
                    } catch(e) {}
                }
            });
            return xhr;
        }
        PatchedXHR.prototype = _OrigXHR.prototype;
        window.XMLHttpRequest = PatchedXHR;
    })();
    """

    async def _setup_api_interception(self) -> None:
        """Install JS-level API capture before X app scripts run."""
        await self.page.add_init_script(self._JS_INTERCEPT)

    async def _drain_js_responses(
        self,
        results: list[Bookmark],
        seen_urls: set[str],
        processed_urls: dict[str, object],
    ) -> str | None:
        """Drain JS-captured X API responses and parse bookmark data."""
        try:
            captured = await self.page.evaluate(
                "() => { const r = window.__x_responses || []; window.__x_responses = []; return r; }"
            )
        except Exception as exc:
            logger.warning("Failed to drain X JS responses: %s", exc)
            return None

        next_cursor = None
        for item in captured or []:
            url = item.get("url", "")
            body = item.get("body", "")
            if not body:
                continue
            if "graphql" in url.lower() or "bookmark" in url.lower():
                logger.info("CAPTURED X API: %s [status=%s]", url[:200], item.get("status"))
            cursor = self._process_body(body, results, seen_urls, processed_urls)
            if cursor:
                next_cursor = cursor
        return next_cursor

    def _process_body(
        self,
        body: str,
        results: list[Bookmark],
        seen_urls: set[str],
        processed_urls: dict[str, object],
    ) -> str | None:
        """Parse a raw X API body and append Bookmark objects."""
        next_cursor = None
        bms, cursor = _parse_graphql_response(body)
        if bms:
            for raw in bms:
                if len(results) >= self.max_bookmarks or self.boundary.matched:
                    break
                bm = _bookmark_from_graphql(raw)
                if bm:
                    url = str(bm.url)
                    reached_boundary = self.boundary.observe(url)
                    normalized_url = normalize_url(url)
                    if (
                        normalized_url not in processed_urls
                        and not self.boundary.is_known(url)
                        and url not in seen_urls
                    ):
                        results.append(bm)
                        seen_urls.add(url)
                    if reached_boundary:
                        logger.info(
                            "X delta frontier reached after %d consecutive known bookmarks",
                            self.boundary.consecutive_known,
                        )
                        break
            if cursor:
                next_cursor = cursor
        return next_cursor

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

            cursor = self._process_body(body, results, seen_urls, processed_urls)
            if cursor:
                next_cursor = cursor

        return next_cursor

    async def _dom_fallback(
        self,
        processed_urls: dict[str, object],
        seen_urls: set[str],
    ) -> list[Bookmark]:
        """Fallback: extract visible tweets from the rendered bookmarks page."""
        results: list[Bookmark] = []
        max_scroll_time_ms = DOM_FALLBACK_TIMEOUT_SECONDS * 1000
        elapsed = 0
        no_new_scrolls = 0
        max_no_new_scrolls = 5

        logger.info("Starting DOM fallback for X bookmarks")
        await self.page.wait_for_timeout(2000)

        while elapsed < max_scroll_time_ms:
            before_count = len(seen_urls)
            dom_posts = await self._extract_posts_from_dom()
            for raw in dom_posts:
                bm = _bookmark_from_dom_post(raw)
                if bm:
                    url = str(bm.url)
                    reached_boundary = self.boundary.observe(url)
                    normalized_url = normalize_url(url)
                    if (
                        normalized_url not in processed_urls
                        and not self.boundary.is_known(url)
                        and url not in seen_urls
                    ):
                        results.append(bm)
                        seen_urls.add(url)
                    if len(results) >= self.max_bookmarks or reached_boundary:
                        break

            if len(results) >= self.max_bookmarks or self.boundary.matched:
                break

            if len(seen_urls) == before_count:
                no_new_scrolls += 1
            else:
                no_new_scrolls = 0

            if no_new_scrolls >= max_no_new_scrolls:
                logger.info("X DOM fallback: no new posts after %d scrolls, stopping", max_no_new_scrolls)
                break

            await self.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            try:
                await self.page.keyboard.press("End")
            except Exception:
                logger.debug("Could not send End key during X DOM fallback scroll")
            await self.page.wait_for_timeout(2500)
            elapsed += 2500

        return results

    async def _extract_posts_from_dom(self) -> list[dict]:
        """Extract tweet data from rendered X article nodes."""
        js_code = """
            () => {
                const posts = [];
                const seen = new Set();
                const articles = document.querySelectorAll('article[data-testid="tweet"], article');
                articles.forEach((article) => {
                    const statusLink = Array.from(article.querySelectorAll('a[href*="/status/"]'))
                        .map(a => a.href)
                        .find(href => /\\/status\\/\\d+/.test(href));
                    if (!statusLink || seen.has(statusLink)) return;
                    seen.add(statusLink);

                    const textEl = article.querySelector('[data-testid="tweetText"]');
                    const text = (textEl ? textEl.innerText : article.innerText || '').trim();
                    const timeEl = article.querySelector('time[datetime]');
                    const authorMatch = statusLink.match(/x\\.com\\/([^/]+)\\/status\\//);
                    const author = authorMatch ? authorMatch[1] : 'unknown';
                    const externalUrls = Array.from(article.querySelectorAll('a[href^="http"]'))
                        .map(a => a.href)
                        .filter(href => !href.includes('x.com') && !href.includes('twitter.com'));
                    const referencedTweetUrls = Array.from(article.querySelectorAll('a[href*="/status/"]'))
                        .map(a => a.href)
                        .filter(href => href !== statusLink && /\\/status\\/\\d+/.test(href));
                    const imageUrls = Array.from(article.querySelectorAll('img[src]'))
                        .map(img => img.src)
                        .filter(src => src.includes('pbs.twimg.com/media/'));

                    posts.push({
                        url: statusLink,
                        text,
                        author,
                        published_at: timeEl ? timeEl.getAttribute('datetime') : '',
                        external_urls: externalUrls,
                        referenced_tweet_urls: referencedTweetUrls,
                        image_urls: imageUrls,
                    });
                });
                return posts;
            }
        """
        try:
            posts = await self.page.evaluate(js_code)
            return posts if isinstance(posts, list) else []
        except Exception:
            logger.exception("X DOM extraction failed")
            return []

    async def scrape(self, mode: ScrapeMode) -> list[Bookmark]:
        """Scrape bookmarks from X.com.

        Args:
            mode: BOOTSTRAP to paginate from start, DELTA to resume from cursor.

        Returns:
            List of Bookmark objects, skipping already-processed URLs.
        """
        results: list[Bookmark] = []
        seen_urls: set[str] = set()
        processed_urls = self.processed.load() if self.skip_processed else {}
        cursor: str | None = None

        if mode == ScrapeMode.DELTA:
            cursor_data = self.cursors.get(Source.X)
            cursor = cursor_data.get("cursor")
            if not cursor:
                logger.info("Delta mode requested but no cursor found, falling back to bootstrap")
                mode = ScrapeMode.BOOTSTRAP

        logger.info("Starting X scraper: mode=%s, cursor=%s", mode.value, cursor)
        await self._setup_api_interception()

        # Set up response interception — store Response objects for later
        # async processing (response.body() is async and can't be called
        # from a synchronous event handler).
        captured_responses: list[Response] = []
        error_responses: list[tuple[int, str]] = []

        def _on_response(response: Response) -> None:
            url = response.url
            status = getattr(response, "status", 0)
            if isinstance(status, int) and status >= 400 and ("x.com" in url or "twitter.com" in url):
                error_responses.append((status, url))
            # Log ALL graphql URLs regardless of match
            if "graphql" in url.lower():
                logger.info("GRAPHQL URL: %s", url[:200])
            # Case-insensitive: X.com may use "Bookmarks" or "bookmarks"
            if "graphql" in url.lower() and BOOKMARKS_QUERY_NAME.lower() in url.lower():
                logger.info("CAPTURED Bookmarks: %s", url[:200])
                if isinstance(status, int):
                    self.bookmarks_endpoint_statuses.append(status)
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
            cursor = await self._drain_js_responses(results, seen_urls, processed_urls) or cursor
            cursor = await self._process_captured(captured_responses, results, seen_urls, processed_urls) or cursor
            logger.debug("After initial load: %d bookmarks, cursor=%s", len(results), cursor)
            captured_responses.clear()

            if not results:
                body_len = -1
                title = ""
                with_body_error = False
                try:
                    body_len = await self.page.evaluate(
                        "() => document.body ? document.body.innerText.length : -1"
                    )
                    title = await self.page.title()
                except Exception:
                    with_body_error = True
                logger.warning(
                    "X API interception yielded no bookmarks; falling back to DOM parsing "
                    "(url=%s, title=%r, body_len=%s, body_probe_failed=%s, http_errors=%s)",
                    self.page.url, title, body_len, with_body_error,
                    [(status, url[:160]) for status, url in error_responses[-5:]],
                )
                results = await self._dom_fallback(processed_urls, seen_urls)
                cursor = await self._drain_js_responses(results, seen_urls, processed_urls) or cursor
                cursor = await self._process_captured(captured_responses, results, seen_urls, processed_urls) or cursor
                captured_responses.clear()

            # Pagination loop
            while cursor and len(results) < self.max_bookmarks:
                if self.boundary.matched:
                    logger.info("X pagination: known frontier matched, stopping")
                    break
                previous_count = len(results)
                previous_cursor = cursor
                # Scroll to trigger next page load
                await self.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await self.page.wait_for_timeout(3000)

                next_cursor = await self._drain_js_responses(results, seen_urls, processed_urls)
                next_cursor = (
                    await self._process_captured(captured_responses, results, seen_urls, processed_urls)
                    or next_cursor
                )
                if next_cursor:
                    cursor = next_cursor
                if not cursor:
                    break  # No more data
                captured_responses.clear()
                if len(results) == previous_count and cursor == previous_cursor:
                    logger.info("X pagination: no new bookmarks or cursor after scroll, stopping")
                    break

        except Exception:
            logger.exception("X scraper encountered an error")

        self.last_cursor = cursor
        if cursor:
            if self.persist_cursor:
                self.cursors.save(Source.X, cursor, ScrapeMode.DELTA.value)
                logger.info("Saved X cursor: %s", cursor)
            else:
                logger.info("Captured X cursor: %s", cursor)

        logger.info("X scraper complete: %d bookmarks", len(results))
        return results
