"""LinkedIn saved-posts scraper for the PKM ingestion pipeline.

Intercepts LinkedIn API responses for saved posts via Playwright route
interception, with a configurable timeout before falling back to DOM
scrolling and parsing.
"""

import logging
import os
import random
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from patchright.async_api import Page

from models import Bookmark, ScrapeMode, Source
from pipeline.frontier import KnownSequenceBoundary
from pipeline.state import ProcessedUrlStore, normalize_url

logger = logging.getLogger(__name__)


def _human_scroll_ms() -> int:
    """Return a random scroll wait time in milliseconds (1500–4000ms)."""
    return random.randint(1500, 4000)


def _human_scroll_px() -> int:
    """Return a random vertical scroll amount in pixels (300–900px)."""
    return random.randint(300, 900)


_RELATIVE_TIME_RE = re.compile(
    r"\b(\d+)\s*(s|sec|secs|m|min|mins|h|hr|hrs|d|day|days|w|wk|wks|mo|mos|month|months|y|yr|yrs|year|years)\b",
    re.IGNORECASE,
)


def _parse_datetime_value(value: object) -> datetime | None:
    """Parse LinkedIn timestamp values into aware datetimes."""
    if not value:
        return None
    try:
        if isinstance(value, int | float):
            seconds = value / 1000 if value > 10_000_000_000 else value
            return datetime.fromtimestamp(seconds, tz=UTC)
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return None
            if stripped.isdigit():
                return _parse_datetime_value(int(stripped))
            return datetime.fromisoformat(stripped.replace("Z", "+00:00"))
    except (ValueError, OSError):
        return None
    return None


def _parse_relative_time(value: str, now: datetime | None = None) -> datetime | None:
    """Parse visible LinkedIn relative times like '3h' or '2 days' approximately."""
    if not value:
        return None
    match = _RELATIVE_TIME_RE.search(value)
    if not match:
        return None

    amount = int(match.group(1))
    unit = match.group(2).lower()
    if now is None:
        now = datetime.now(UTC)

    if unit.startswith("s"):
        delta = timedelta(seconds=amount)
    elif unit.startswith("m") and unit not in {"mo", "mos", "month", "months"}:
        delta = timedelta(minutes=amount)
    elif unit.startswith("h"):
        delta = timedelta(hours=amount)
    elif unit.startswith("d"):
        delta = timedelta(days=amount)
    elif unit.startswith("w"):
        delta = timedelta(weeks=amount)
    elif unit in {"mo", "mos", "month", "months"}:
        delta = timedelta(days=amount * 30)
    elif unit.startswith("y"):
        delta = timedelta(days=amount * 365)
    else:
        return None

    return now - delta


# LinkedIn API URL patterns — broader to catch current endpoints (2025-2026)
LINKEDIN_API_PATTERNS = [
    "/voyager/api/graphql",
    "/voyager/api/voyager",
    "/voyager/api/savedItems",
    "/voyager/api/bookmark",
    "/feed/recommendedEntities",
    "/saved-items",
    "/voyager/api/feed",
    "/voyager/api/clips",
]

# Any Voyager response URL is worth inspecting
LINKEDIN_VOYAGER_PATTERN = "/voyager/"

# API interception timeout: seconds to wait for API responses before DOM fallback
API_TIMEOUT_SECONDS = int(os.getenv("LI_API_TIMEOUT", "30"))

# DOM fallback timeout: seconds of scrolling before giving up
DOM_FALLBACK_TIMEOUT_SECONDS = int(os.getenv("LI_DOM_TIMEOUT", "60"))

# Initial saved-posts page navigation timeout: seconds before retry/failure.
GOTO_TIMEOUT_SECONDS = int(os.getenv("LI_GOTO_TIMEOUT", "180"))

# Default max posts per run
DEFAULT_MAX_POSTS = 500


def _parse_linkedin_api_response(body: str) -> tuple[list[dict], str | None]:
    """Parse a LinkedIn API response into post dicts and next cursor.

    Handles multiple known response schemas:
    - searchDashClustersByAll (current saved posts GraphQL)
    - data.elements array (legacy)
    - included array (Voyager API)
    - top-level list

    Returns (posts, next_cursor).
    """
    import json

    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return [], None

    posts: list[dict] = []
    cursor: str | None = None

    # Structure A: searchDashClustersByAll (current GraphQL saved posts)
    # LinkedIn uses two formats: single-nested (data.KEY) and double-nested (data.data.KEY).
    # Try the double-nested first, fall back to single-nested so both formats work.
    top_data = data.get("data", {})
    search_data = top_data.get("data", {}) or top_data
    if search_data:
        for key in search_data:
            cluster = search_data[key]
            if isinstance(cluster, dict) and "elements" in cluster:
                for element in cluster.get("elements", []):
                    items = element.get("items", [])
                    for item in items:
                        post_info = _extract_post_from_element(item)
                        if post_info:
                            posts.append(post_info)
                    # Also try element itself as a post
                    if not items:
                        post_info = _extract_post_from_element(element)
                        if post_info:
                            posts.append(post_info)
                # Pagination from metadata
                metadata = cluster.get("metadata", {})
                if metadata:
                    cursor = metadata.get("paginationToken")
                # Or from paging
                paging = cluster.get("paging", {})
                if paging and not cursor:
                    cursor = str(paging.get("start", ""))
                if posts:
                    return posts, cursor

    # Structure B: data.elements array (original assumption)
    elements = data.get("data", {}).get("elements", []) or data.get("elements", [])

    # Structure C: included array (Voyager API uses this)
    if not elements:
        elements = data.get("included", [])

    # Structure D: response body is the elements directly (list at top level)
    if not elements and isinstance(data, list):
        elements = data

    for element in elements:
        post_info = _extract_post_from_element(element)
        if post_info:
            posts.append(post_info)

    # Check for pagination cursor
    paging = data.get("data", {}).get("paging", {}) or data.get("paging", {})
    if paging:
        cursor = paging.get("nextCursor") or paging.get("start")

    # Also check metadata for cursor
    metadata = data.get("data", {}).get("metadata", {}) or data.get("metadata", {})
    if not cursor and metadata:
        cursor = metadata.get("nextCursor") or metadata.get("paging", {}).get("start")

    return posts, cursor


def _extract_external_urls_from_post(post_data: dict) -> list[str]:
    """Extract external (non-LinkedIn) URLs from a LinkedIn post data dict.

    Scans all nested fields for URL strings, filtering out LinkedIn-internal
    URLs, navigation links, and tracking redirects.
    """
    import re as _re

    external: list[str] = []
    seen: set[str] = set()

    def _scan_value(val: object, depth: int = 0) -> None:
        if depth > 6 or val is None:
            return
        if isinstance(val, str):
            if val.startswith("http") and "linkedin.com" not in val:
                clean = val.split("?")[0] if "?" in val else val
                if clean not in seen:
                    seen.add(clean)
                    external.append(val)
        elif isinstance(val, dict):
            for v in val.values():
                _scan_value(v, depth + 1)
        elif isinstance(val, list):
            for v in val:
                _scan_value(v, depth + 1)

    _scan_value(post_data)

    # Also extract URLs from text via regex
    text = ""
    for key in ("text", "commentary", "description"):
        v = post_data.get(key, "")
        if isinstance(v, str):
            text += " " + v
        elif isinstance(v, dict):
            inner = v.get("text", "")
            if isinstance(inner, str):
                text += " " + inner
    if not text:
        commentary = post_data.get("commentary", {})
        if isinstance(commentary, dict):
            cv = commentary.get("value", {})
            if isinstance(cv, dict):
                text += " " + cv.get("text", "")
            elif isinstance(cv, str):
                text += " " + cv

    url_pattern = _re.compile(r'https?://[^\s<"\)\}\]]+')
    for match in url_pattern.findall(text):
        if "linkedin.com" not in match and match not in seen:
            seen.add(match)
            external.append(match)

    return external


def _extract_image_urls_from_post(post_data: dict) -> list[str]:
    """Extract likely image URLs from nested LinkedIn post data."""
    images: list[str] = []
    seen: set[str] = set()

    def _maybe_add(value: str) -> None:
        if not value.startswith("http"):
            return
        lower = value.lower()
        if not any(marker in lower for marker in (".jpg", ".jpeg", ".png", ".webp", "media.licdn.com", "image")):
            return
        if value not in seen:
            seen.add(value)
            images.append(value)

    def _scan_value(val: object, depth: int = 0) -> None:
        if depth > 8 or val is None:
            return
        if isinstance(val, str):
            _maybe_add(val)
        elif isinstance(val, dict):
            for key, value in val.items():
                if key.lower() in {"url", "rooturl", "fileidentifyingurlpathsegment", "image", "src"}:
                    if isinstance(value, str):
                        _maybe_add(value)
                _scan_value(value, depth + 1)
        elif isinstance(val, list):
            for value in val:
                _scan_value(value, depth + 1)

    _scan_value(post_data)
    return images


def _extract_post_from_element(element: dict) -> dict | None:
    """Extract post data from a single LinkedIn API element.

    Handles both the original API format and the Voyager format which uses
    type-tagged objects like {"value": {"com.linkedin.voyager...": {...}}},
    as well as SearchClusterViewModel items with navigationUrl/trackableAction.
    """
    try:
        # SearchClusterViewModel item format: has navigationUrl directly
        url = (
            element.get("navigationUrl")
            or (element.get("trackableAction") or {}).get("navigationUrl")
            or ""
        )

        # If we found a URL at the top level, skip the deeper navigation
        if url:
            # In SearchClusterViewModel items, 'text' is the card display title
            # (typically the author name), not the post body. Use commentary/description first.
            commentary = element.get("commentary") or element.get("description") or {}
            if isinstance(commentary, dict):
                text = commentary.get("text") or ""
            elif isinstance(commentary, str):
                text = commentary
            else:
                text = ""
            author = _extract_author(element)
            created_at = element.get("createdAt") or element.get("time", "")
            saved_at = element.get("savedAt") or element.get("savedTime") or element.get("saved_at")
            external_urls = _extract_external_urls_from_post(element)
            image_urls = _extract_image_urls_from_post(element)
            return {
                "url": url,
                "text": text,
                "author": author,
                "created_at": created_at,
                "saved_at": saved_at,
                "external_urls": external_urls,
                "image_urls": image_urls,
            }

        # Navigate various possible LinkedIn response shapes
        content = element.get("content", {})
        if not content:
            content = element

        # Try to get the actual post data
        post_data = content.get("data", {}) or content

        # Voyager format: value contains a type-tagged object
        # e.g. {"value": {"com.linkedin.voyager.feed.UpdateV2": {...}}}
        value = post_data.get("value", {})
        if isinstance(value, dict):
            # Find the voyager-typed object
            for key, val in value.items():
                if isinstance(val, dict) and "com.linkedin" in key:
                    post_data = val
                    break

        # Extract URL — try multiple known fields
        url = (
            post_data.get("url")
            or post_data.get("permalink")
            or post_data.get("shareUrl")
            or ""
        )
        if not url:
            # Try nested activity object
            activity = post_data.get("activity", {})
            url = activity.get("url") or activity.get("permalink") or ""
        if not url:
            # Try navigationUrl or canonicalUrl (Voyager format)
            nav = post_data.get("navigationUrl") or post_data.get("canonicalUrl")
            if nav:
                url = nav
        if not url:
            # Try to find any URL in nested objects
            for _key, val in post_data.items():
                if isinstance(val, dict):
                    found_url = val.get("url") or val.get("permalink") or val.get("shareUrl")
                    if found_url and isinstance(found_url, str) and found_url.startswith("http"):
                        url = found_url
                        break

        # Normalize URL to linkedin.com format
        if url and not url.startswith("http"):
            url = f"https://www.linkedin.com{url}"

        if not url:
            return None

        # Extract text — try multiple known fields
        text = (
            post_data.get("text")
            or post_data.get("commentary")
            or ""
        )
        # Voyager: text may be nested under commentary.value or similar
        if not text:
            commentary = post_data.get("commentary", {})
            if isinstance(commentary, dict):
                commentary_value = commentary.get("value", {})
                if isinstance(commentary_value, dict):
                    text = commentary_value.get("text", "")
                elif isinstance(commentary_value, str):
                    text = commentary_value
        if not text:
            content_text = post_data.get("content", {})
            if isinstance(content_text, dict):
                text = content_text.get("text", "")

        # Extract external URLs from the post data
        external_urls = _extract_external_urls_from_post(post_data)
        image_urls = _extract_image_urls_from_post(post_data)

        # Extract author
        author = _extract_author(post_data)

        # Extract timestamp
        created_at = post_data.get("createdAt") or post_data.get("time", "") or post_data.get("createdTime", "")
        saved_at = post_data.get("savedAt") or post_data.get("savedTime") or post_data.get("saved_at")

        return {
            "url": url,
            "text": text,
            "author": author,
            "created_at": created_at,
            "saved_at": saved_at,
            "external_urls": external_urls,
            "image_urls": image_urls,
        }
    except Exception:
        return None


def _extract_author(post_data: dict) -> str:
    """Extract author name from post data."""
    # Try various author field locations
    author_obj = (
        post_data.get("author")
        or post_data.get("actor")
        or post_data.get("owner")
        or {}
    )

    if isinstance(author_obj, dict):
        name = author_obj.get("name")
        if name:
            return name

        first = author_obj.get("firstName", "")
        last = author_obj.get("lastName", "")
        full = f"{first} {last}".strip()
        if full:
            return full

        pub_id = author_obj.get("publicIdentifier")
        if pub_id:
            return pub_id

        return "unknown"

    if isinstance(author_obj, str):
        return author_obj

    return "unknown"


def _bookmark_from_linkedin_post(raw: dict) -> Bookmark | None:
    """Convert a raw LinkedIn post dict into a Bookmark."""
    try:
        url = raw.get("url", "")
        if not url:
            return None

        text = raw.get("text", "")
        author = raw.get("author", "unknown")

        published_at = _parse_datetime_value(raw.get("created_at"))
        if not published_at:
            published_at = _parse_relative_time(str(raw.get("created_at_text") or ""))
        saved_at = _parse_datetime_value(raw.get("saved_at"))

        # LinkedIn API sometimes returns text as a dict-like string
        # (e.g. "{'textDirection': 'FIRST_STRONG', 'text': 'Author Name'...}")
        # Try to extract the actual text from it
        if isinstance(text, dict):
            text = text.get("text", "")
        elif isinstance(text, str) and text.startswith("{") and "'text'" in text:
            try:
                import ast
                parsed = ast.literal_eval(text)
                if isinstance(parsed, dict):
                    text = parsed.get("text", "")
                    # Also try to extract author if still unknown
                    if author == "unknown":
                        author_name = parsed.get("text", "")
                        if author_name:
                            author = author_name
            except (ValueError, SyntaxError):
                pass

        # Canonicalize LinkedIn URL variants before state/frontier checks.
        url = normalize_url(url)

        if not url:
            return None

        title = text[:120] if text else f"Post by {author}"
        external_urls = raw.get("external_urls") or []
        image_urls = raw.get("image_urls") or []

        return Bookmark(
            source=Source.LINKEDIN,
            url=url,
            title=title,
            post_text=text or None,
            external_urls=external_urls if external_urls else None,
            image_urls=image_urls if image_urls else None,
            published_at=published_at,
            saved_at=saved_at,
        )
    except Exception:
        logger.exception("Failed to create Bookmark from raw post: %s", raw)
        return None


class ScraperLinkedIn:
    """Scrape saved posts from LinkedIn via API interception with DOM fallback.

    Uses Playwright response interception to capture saved-posts API results.
    If interception yields zero posts after timeout, falls back to DOM
    scrolling and parsing. Timeout is configurable via environment variables.
    """

    def __init__(
        self,
        page: Page,
        state_dir: Path,
        max_posts: int = DEFAULT_MAX_POSTS,
        skip_processed: bool = True,
        known_urls: set[str] | None = None,
        stop_after_known: int = 0,
        frontier_size: int = 20,
    ) -> None:
        self.page = page
        self.max_posts = max_posts
        self.skip_processed = skip_processed
        self.processed = ProcessedUrlStore(state_dir)
        self._api_posts: list[dict] = []
        self._api_cursor: str | None = None
        self._seen_urls: set[str] = set()
        self.boundary = KnownSequenceBoundary(
            known_urls=known_urls or set(),
            stop_after_known=stop_after_known,
            recent_limit=frontier_size,
        )

    async def scrape(self, mode: ScrapeMode) -> list[Bookmark]:
        """Scrape saved posts from LinkedIn.

        Args:
            mode: BOOTSTRAP or DELTA (cursor not yet implemented for LinkedIn).

        Returns:
            List of Bookmark objects, skipping already-processed URLs.
        """
        results: list[Bookmark] = []
        seen_urls: set[str] = set()
        processed_urls = self.processed.load() if self.skip_processed else {}

        logger.info("Starting LinkedIn scraper: mode=%s", mode.value)

        # Set up API interception
        await self._setup_api_interception()

        # Navigate to saved posts
        await self.page.goto(
            "https://www.linkedin.com/my-items/saved-posts/",
            wait_until="domcontentloaded",
            timeout=GOTO_TIMEOUT_SECONDS * 1000,
        )
        await self.page.wait_for_timeout(3000)
        # Diagnostic: capture what LinkedIn is showing
        try:
            screenshot_path = "/app/volumes/state/linkedin_debug.png"
            await self.page.screenshot(path=screenshot_path)
            logger.info("LinkedIn page URL after load: %s", self.page.url)
            logger.info("LinkedIn screenshot saved to %s", screenshot_path)
            page_title = await self.page.title()
            logger.info("LinkedIn page title: %s", page_title)
        except Exception as e:
            logger.warning("Could not take LinkedIn screenshot: %s", e)

        # Phase 1: wait for initial API responses (JS wrapper populates window.__li_responses)
        initial_timeout = API_TIMEOUT_SECONDS * 1000
        check_interval = 2000
        elapsed = 0
        initial_found = False

        while elapsed < initial_timeout:
            await self.page.wait_for_timeout(check_interval)
            elapsed += check_interval

            await self._drain_js_responses()

            if self._api_posts:
                initial_found = True
                break

        if not initial_found:
            logger.warning(
                "LinkedIn API interception yielded no posts after %ds, falling back to DOM parsing",
                API_TIMEOUT_SECONDS,
            )
            results = await self._dom_fallback(processed_urls, seen_urls)
            logger.info("LinkedIn scraper complete: %d bookmarks", len(results))
            return results

        # Phase 2: paginate — scroll to trigger more API responses
        logger.info("LinkedIn API interception: %d initial posts, paginating...", len(self._api_posts))
        previous_count = len(self._api_posts)
        no_new_count = 0
        max_no_new = 3  # Stop after 3 consecutive scrolls with no new posts
        pagination_timeout = int(os.getenv("LI_PAGINATION_TIMEOUT", "60")) * 1000
        pagination_elapsed = 0

        while pagination_elapsed < pagination_timeout and len(self._api_posts) < self.max_posts:
            scroll_px = _human_scroll_px()
            scroll_wait = _human_scroll_ms()
            # Scroll down to trigger more API calls
            await self.page.evaluate(f"window.scrollBy(0, {scroll_px})")
            await self.page.wait_for_timeout(scroll_wait)
            pagination_elapsed += scroll_wait

            await self._drain_js_responses()

            current_count = len(self._api_posts)
            if current_count == previous_count:
                no_new_count += 1
                if no_new_count >= max_no_new:
                    logger.info("LinkedIn pagination: no new posts after %d scrolls, stopping", max_no_new)
                    break
            else:
                no_new_count = 0
                previous_count = current_count

        # Convert raw posts to Bookmarks, deduplicating against processed URLs
        logger.info("Converting %d raw posts to bookmarks (processed_urls has %d entries)", len(self._api_posts), len(processed_urls))
        for raw in self._api_posts:
            bm = _bookmark_from_linkedin_post(raw)
            if bm is None:
                logger.warning("Could not convert raw post to bookmark: %s", {k: str(v)[:80] for k, v in raw.items()})
            else:
                url = str(bm.url)
                reached_boundary = self.boundary.observe(url)
                normalized_url = normalize_url(url)
                if self.skip_processed and normalized_url in processed_urls:
                    logger.info("Skipping already-processed: %s", url[:100])
                elif self.boundary.is_known(url):
                    logger.info("Skipping known vault/frontier bookmark: %s", url[:100])
                elif url in seen_urls:
                    logger.info("Skipping duplicate in this run: %s", url[:100])
                else:
                    logger.info("Adding LinkedIn bookmark: %s title=%r", url[:100], bm.title[:120])
                    results.append(bm)
                    seen_urls.add(url)
                if len(results) >= self.max_posts or reached_boundary:
                    if reached_boundary:
                        logger.info(
                            "LinkedIn delta frontier reached after %d consecutive known bookmarks",
                            self.boundary.consecutive_known,
                        )
                    break

        logger.info("LinkedIn scraper complete: %d bookmarks (from %d raw posts)", len(results), len(self._api_posts))
        return results

    _JS_INTERCEPT = """
    (() => {
        if (window.__li_intercept_installed) return;
        window.__li_intercept_installed = true;
        window.__li_responses = [];

        const PATTERNS = ['/voyager/', '/saved-items/', '/bookmark'];
        function isLiApi(url) {
            return url && PATTERNS.some(p => url.includes(p));
        }

        // Wrap fetch — clone body before returning to caller
        const _origFetch = window.fetch.bind(window);
        window.fetch = function(input, init) {
            const url = (typeof input === 'string') ? input : (input && input.url) || '';
            return _origFetch(input, init).then(function(response) {
                if (isLiApi(url)) {
                    const clone = response.clone();
                    clone.text().then(function(body) {
                        window.__li_responses.push({url: url, status: response.status, body: body});
                    }).catch(function() {});
                }
                return response;
            });
        };

        // Wrap XHR
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
                if (isLiApi(_url)) {
                    try {
                        window.__li_responses.push({
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
        """Inject JS fetch/XHR wrapper that captures LinkedIn API responses.

        JS-level capture reads the body before Chrome evicts it from the CDP
        resource cache, which happens within seconds of the response arriving.
        Captured responses are stored in window.__li_responses and drained by
        _drain_js_responses() during the scrape loop.
        """
        await self.page.add_init_script(self._JS_INTERCEPT)

    async def _drain_js_responses(self) -> None:
        """Read and clear window.__li_responses, parsing any LinkedIn posts found."""
        try:
            captured = await self.page.evaluate(
                "() => { const r = window.__li_responses || []; window.__li_responses = []; return r; }"
            )
        except Exception as exc:
            logger.warning("Failed to drain JS responses: %s", exc)
            return

        for item in captured:
            url = item.get("url", "")
            body = item.get("body", "")
            status = item.get("status", 0)
            if not body:
                continue
            logger.info("CAPTURED LinkedIn: %s [status=%s]", url[:200], status)
            posts, cursor = _parse_linkedin_api_response(body)
            if posts:
                for post in posts:
                    post_url = post.get("url", "")
                    if post_url and post_url not in self._seen_urls:
                        self._api_posts.append(post)
                        self._seen_urls.add(post_url)
                logger.info("Parsed %d posts from JS capture (total: %d)", len(posts), len(self._api_posts))
            else:
                logger.debug("JS capture yielded 0 posts from %s (preview: %.300s)", url[:80], body)
            if cursor:
                self._api_cursor = cursor

    async def _dom_fallback(
        self,
        processed_urls: dict[str, dict[str, str]],
        seen_urls: set[str],
    ) -> list[Bookmark]:
        """Fallback: scroll the saved-posts feed and extract posts from DOM.

        Scrolls the page incrementally, extracting post cards from the DOM.
        Stops after timeout or when no new posts are found.
        """
        results: list[Bookmark] = []
        max_scroll_time_ms = DOM_FALLBACK_TIMEOUT_SECONDS * 1000
        elapsed = 0
        no_new_scrolls = 0
        max_no_new_scrolls = 5

        logger.info("Starting DOM fallback for LinkedIn saved posts")

        # Wait a moment for the page to fully render
        await self.page.wait_for_timeout(2000)

        while elapsed < max_scroll_time_ms:
            before_count = len(seen_urls)

            # Extract posts from current DOM
            dom_posts = await self._extract_posts_from_dom()

            for raw in dom_posts:
                bm = _bookmark_from_linkedin_post(raw)
                if bm:
                    url = str(bm.url)
                    reached_boundary = self.boundary.observe(url)
                    normalized_url = normalize_url(url)
                    if (
                        normalized_url not in processed_urls
                        and not self.boundary.is_known(url)
                        and url not in seen_urls
                    ):
                        logger.info("Adding LinkedIn DOM bookmark: %s title=%r", url[:100], bm.title[:120])
                        results.append(bm)
                        seen_urls.add(url)
                    if len(results) >= self.max_posts or reached_boundary:
                        if reached_boundary:
                            logger.info(
                                "LinkedIn DOM delta frontier reached after %d consecutive known bookmarks",
                                self.boundary.consecutive_known,
                            )
                        break

            if len(results) >= self.max_posts or self.boundary.matched:
                break

            new_count = len(seen_urls) - before_count
            if new_count == 0:
                no_new_scrolls += 1
            else:
                no_new_scrolls = 0

            if no_new_scrolls >= max_no_new_scrolls:
                logger.info(
                    "DOM fallback: no new posts after %d scrolls, stopping",
                    max_no_new_scrolls,
                )
                break

            # Scroll down with randomized amounts and delays
            scroll_wait = _human_scroll_ms()
            await self.page.evaluate(
                f"window.scrollBy(0, Math.max({_human_scroll_px()}, Math.floor(window.innerHeight * 0.85))); "
                "window.scrollTo(0, document.body.scrollHeight);"
            )
            try:
                await self.page.keyboard.press("End")
            except Exception:
                logger.debug("Could not send End key during LinkedIn DOM fallback scroll")
            await self.page.wait_for_timeout(scroll_wait)
            elapsed += scroll_wait

        return results

    async def _extract_posts_from_dom(self) -> list[dict]:
        """Extract post data from the current DOM state.

        Returns a list of raw post dicts with url, text, author, created_at.
        Uses multiple selector strategies for robustness against LinkedIn's
        frequent HTML changes.
        """
        js_code = """
            () => {
                const posts = [];
                const seen = new Set();

                const linkSelectors = [
                    'a[href*="/feed/update/"]',
                    'a[href*="/posts/"]',
                    'a[href*="/activity/"]',
                ];
                const authorSelectors = [
                    '[class*="actor"]', '[class*="author"]', 'a[href*="/in/"]',
                ];

                function findCard(link) {
                    let node = link;
                    for (let i = 0; i < 12 && node && node !== document.body; i++) {
                        const text = (node.innerText || '').trim();
                        const updateLinks = node.querySelectorAll
                            ? node.querySelectorAll(linkSelectors.join(', ')).length
                            : 0;
                        if (updateLinks >= 1 && text.length > 80) {
                            return node;
                        }
                        node = node.parentElement;
                    }
                    return link;
                }

                function cleanText(text) {
                    return (text || '')
                        .replace(/\\n{3,}/g, '\\n\\n')
                        .replace(/^Status is (?:online|offline)\\n/gm, '')
                        .trim();
                }

                function authorFromCard(card) {
                    const authorEl = card.querySelector(authorSelectors.join(', '));
                    if (!authorEl) return 'unknown';
                    return cleanText(authorEl.innerText)
                        .replace(/\\nView .*?profile.*$/is, '')
                        .replace(/\\nView company:.*$/is, '')
                        .trim() || 'unknown';
                }

                function relativeTimeNear(el) {
                    let node = el;
                    for (let i = 0; i < 10 && node && node !== document.body; i++) {
                        const text = node.innerText || '';
                        const match = text.match(/\\b\\d+\\s*(?:s|sec|secs|m|min|mins|h|hr|hrs|d|day|days|w|wk|wks|mo|mos|month|months|y|yr|yrs|year|years)\\b\\s*•/i);
                        if (match) return match[0].replace('•', '').trim();
                        node = node.parentElement;
                    }
                    return '';
                }

                // Strategy 1: Find all links that look like post URLs
                const allLinks = document.querySelectorAll(
                    linkSelectors.join(', ')
                );
                allLinks.forEach((link) => {
                    const href = link.href || '';
                    if (!seen.has(href)) {
                        seen.add(href);
                        const card = findCard(link);
                        const text = cleanText(card.innerText);
                        const author = authorFromCard(card);
                        if (href || text) {
                            const imageUrls = Array.from(card.querySelectorAll('img[src]'))
                                .map(img => img.src)
                                .filter(src => src.startsWith('http') && !src.includes('profile-displayphoto'));
                            posts.push({
                                url: href, text: text,
                                author: author || 'unknown',
                                created_at: '',
                                created_at_text: relativeTimeNear(link),
                                image_urls: imageUrls,
                            });
                        }
                    }
                });

                // Strategy 2: Find elements with data-urn attributes
                const urnSelectors = [
                    '[data-urn*="activity"]',
                    '[data-urn*="update"]',
                    '[data-urn*="post"]',
                ].join(', ');
                const urnElements = document.querySelectorAll(urnSelectors);
                urnElements.forEach((el) => {
                    const urn = el.getAttribute('data-urn') || '';
                    const links = el.querySelectorAll('a[href]');
                    const url = links.length > 0 ? links[0].href : '';
                    if (url && !seen.has(url)) {
                        seen.add(url);
                        const text = cleanText(el.innerText);
                        const author = authorFromCard(el);
                        const imageUrls = Array.from(el.querySelectorAll('img[src]'))
                            .map(img => img.src)
                            .filter(src => src.startsWith('http') && !src.includes('profile-displayphoto'));
                        posts.push({
                            url: url, text: text,
                            author: author || 'unknown',
                            created_at: '',
                            created_at_text: relativeTimeNear(el),
                            image_urls: imageUrls,
                        });
                    }
                });

                // Strategy 3: LinkedIn My Items / Saved Posts cards
                const savedCardSelectors = [
                    '[class*="save-caFE"]',
                    '[class*="saved-item"]',
                    '[class*="entity-result"]',
                    '[class*="occludable"]',
                ].join(', ');
                const savedCards = document.querySelectorAll(savedCardSelectors);
                savedCards.forEach((card) => {
                    const link = card.querySelector(
                        linkSelectors.join(', ')
                    );
                    if (link) {
                        const url = link.href;
                        if (!seen.has(url)) {
                            seen.add(url);
                            const text = cleanText(card.innerText);
                            const author = authorFromCard(card);
                            const imageUrls = Array.from(card.querySelectorAll('img[src]'))
                                .map(img => img.src)
                                .filter(src => src.startsWith('http') && !src.includes('profile-displayphoto'));
                            posts.push({
                                url: url, text: text,
                                author: author || 'unknown',
                                created_at: '',
                                created_at_text: relativeTimeNear(card),
                                image_urls: imageUrls,
                            });
                        }
                    }
                });

                return posts;
            }
        """
        try:
            posts = await self.page.evaluate(js_code)
            return posts if isinstance(posts, list) else []
        except Exception:
            logger.exception("DOM extraction failed")
            return []
