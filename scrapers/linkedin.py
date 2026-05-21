"""LinkedIn saved-posts scraper for the PKM ingestion pipeline.

Intercepts LinkedIn API responses for saved posts via Playwright route
interception, with a configurable timeout before falling back to DOM
scrolling and parsing.
"""

import logging
import os
import random
from datetime import UTC, datetime
from pathlib import Path

from patchright.async_api import Page

from models import Bookmark, ScrapeMode, Source
from pipeline.state import ProcessedUrlStore

logger = logging.getLogger(__name__)


def _human_scroll_ms() -> int:
    """Return a random scroll wait time in milliseconds (1500–4000ms)."""
    return random.randint(1500, 4000)


def _human_scroll_px() -> int:
    """Return a random vertical scroll amount in pixels (300–900px)."""
    return random.randint(300, 900)


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
            external_urls = _extract_external_urls_from_post(element)
            return {
                "url": url,
                "text": text,
                "author": author,
                "created_at": created_at,
                "external_urls": external_urls,
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

        # Extract author
        author = _extract_author(post_data)

        # Extract timestamp
        created_at = post_data.get("createdAt") or post_data.get("time", "") or post_data.get("createdTime", "")

        return {
            "url": url,
            "text": text,
            "author": author,
            "created_at": created_at,
            "external_urls": external_urls,
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

        # Parse created_at if available
        saved_at: datetime | None = None
        created_at_raw = raw.get("created_at")
        if created_at_raw:
            try:
                if isinstance(created_at_raw, int | float):
                    saved_at = datetime.fromtimestamp(created_at_raw / 1000, tz=UTC)
                elif isinstance(created_at_raw, str):
                    saved_at = datetime.fromisoformat(created_at_raw.replace("Z", "+00:00"))
            except (ValueError, OSError):
                pass

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

        # Clean up LinkedIn URLs — remove tracking params
        # e.g. "?updateEntityUrn=urn:li:..." → clean URL
        if "?" in url:
            url = url.split("?")[0]

        if not url:
            return None

        title = text[:120] if text else f"Post by {author}"
        external_urls = raw.get("external_urls") or []

        return Bookmark(
            source=Source.LINKEDIN,
            url=url,
            title=title,
            post_text=text or None,
            external_urls=external_urls if external_urls else None,
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
    ) -> None:
        self.page = page
        self.max_posts = max_posts
        self.processed = ProcessedUrlStore(state_dir)
        self._api_posts: list[dict] = []
        self._api_cursor: str | None = None
        self._seen_urls: set[str] = set()

    async def scrape(self, mode: ScrapeMode) -> list[Bookmark]:
        """Scrape saved posts from LinkedIn.

        Args:
            mode: BOOTSTRAP or DELTA (cursor not yet implemented for LinkedIn).

        Returns:
            List of Bookmark objects, skipping already-processed URLs.
        """
        results: list[Bookmark] = []
        seen_urls: set[str] = set()
        processed_urls = self.processed.load()

        logger.info("Starting LinkedIn scraper: mode=%s", mode.value)

        # Set up API interception
        await self._setup_api_interception()

        # Navigate to saved posts
        await self.page.goto(
            "https://www.linkedin.com/my-items/saved-posts/",
            wait_until="domcontentloaded",
            timeout=60000,
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
            elif str(bm.url) in processed_urls:
                logger.info("Skipping already-processed: %s", str(bm.url)[:100])
            elif str(bm.url) in seen_urls:
                logger.info("Skipping duplicate in this run: %s", str(bm.url)[:100])
            else:
                logger.info("Adding bookmark: %s", str(bm.url)[:100])
                results.append(bm)
                seen_urls.add(str(bm.url))
                if len(results) >= self.max_posts:
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
        previous_count = 0

        logger.info("Starting DOM fallback for LinkedIn saved posts")

        # Wait a moment for the page to fully render
        await self.page.wait_for_timeout(2000)

        while elapsed < max_scroll_time_ms:
            # Extract posts from current DOM
            dom_posts = await self._extract_posts_from_dom()

            for raw in dom_posts:
                bm = _bookmark_from_linkedin_post(raw)
                if bm and str(bm.url) not in processed_urls and str(bm.url) not in seen_urls:
                    results.append(bm)
                    seen_urls.add(str(bm.url))
                    if len(results) >= self.max_posts:
                        break

            if len(results) >= self.max_posts:
                break

            # Check if we found new posts this iteration
            current_count = len(dom_posts)
            if current_count == previous_count and current_count > 0 and elapsed > _human_scroll_ms() * 2:
                logger.info("DOM fallback: no new posts found, stopping")
                break

            previous_count = current_count

            # Scroll down with randomized amounts and delays
            scroll_wait = _human_scroll_ms()
            await self.page.evaluate(f"window.scrollBy(0, {_human_scroll_px()})")
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
                const cardSelectors = [
                    'article', 'section', '[data-urn]',
                    '[class*="update"]', '[class*="feed"]',
                    '[class*="saved"]', 'li', 'div[class*="card"]',
                ];
                const textSelectors = [
                    '[dir="ltr"]', '[class*="break-words"]',
                    '[class*="text"]', 'span[class*="attributed"]', 'p',
                ];
                const authorSelectors = [
                    '[class*="actor"]', '[class*="author"]', 'a[href*="/in/"]',
                ];

                // Strategy 1: Find all links that look like post URLs
                const allLinks = document.querySelectorAll(
                    linkSelectors.join(', ')
                );
                allLinks.forEach((link) => {
                    const href = link.href || '';
                    if (!seen.has(href)) {
                        seen.add(href);
                        const card = link.closest(
                            cardSelectors.join(', ')
                        ) || link;
                        const textEl = card.querySelector(
                            textSelectors.join(', ')
                        );
                        const authorEl = card.querySelector(
                            authorSelectors.join(', ')
                        );
                        const text = textEl
                            ? textEl.innerText.trim() : '';
                        const author = authorEl
                            ? authorEl.innerText.trim() : '';
                        if (href || text) {
                            posts.push({
                                url: href, text: text,
                                author: author || 'unknown',
                                created_at: '',
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
                        const textEl = el.querySelector(
                            textSelectors.join(', ')
                        );
                        const authorEl = el.querySelector(
                            authorSelectors.join(', ')
                        );
                        const text = textEl
                            ? textEl.innerText.trim() : '';
                        const author = authorEl
                            ? authorEl.innerText.trim() : '';
                        posts.push({
                            url: url, text: text,
                            author: author || 'unknown',
                            created_at: '',
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
                            const textEl = card.querySelector(
                                textSelectors.join(', ')
                            );
                            const authorEl = card.querySelector(
                                authorSelectors.join(', ')
                            );
                            const text = textEl
                                ? textEl.innerText.trim() : '';
                            const author = authorEl
                                ? authorEl.innerText.trim() : '';
                            posts.push({
                                url: url, text: text,
                                author: author || 'unknown',
                                created_at: '',
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
