"""Playwright-based web content extractor for the PKM ingestion pipeline.

Uses an already-authenticated browser context to fetch and extract article
text from URLs that trafilatura (plain HTTP) can't handle, such as:
- LinkedIn posts (require auth cookies)
- X/Twitter internal pages (require JS rendering)
- Sites with heavy JS rendering or anti-bot protection

Falls back gracefully: if Playwright extraction fails, returns None so the
caller can try other methods or use post_text only.
"""

import logging
import re
from datetime import UTC, datetime

from patchright.async_api import BrowserContext, Page

from config import FETCH_TIMEOUT
from models import ExtractedContent

logger = logging.getLogger(__name__)

MAX_CONTENT_LENGTH = 50000
NAVIGATION_TIMEOUT = FETCH_TIMEOUT * 1000

CONTENT_SELECTORS = [
    "[data-testid='article-body']",  # X/Twitter long-form articles
    "article",
    '[role="article"]',
    ".post-content",
    ".article-content",
    ".entry-content",
    ".post-body",
    ".story-body",
    ".main-content",
    "#main-content",
    "#article-body",
    ".article-body",
    ".ArticleBody",
    ".styled-body",
    "[data-article]",
    ".blog-post-content",
    ".content-body",
    "main",
    '[role="main"]',
]

REMOVE_SELECTORS = [
    "nav",
    "header",
    "footer",
    "aside",
    '[role="navigation"]',
    '[role="banner"]',
    '[role="contentinfo"]',
    '[role="complementary"]',
    ".sidebar",
    ".ad",
    ".advertisement",
    ".social-share",
    ".share-buttons",
    ".related-posts",
    ".comments",
    "#comments",
    ".newsletter",
    ".popup",
    ".modal",
    "script",
    "style",
    "noscript",
    "iframe",
]


async def extract_with_playwright(
    context: BrowserContext,
    url: str,
    post_text: str | None = None,
) -> ExtractedContent | None:
    """Fetch and extract article text using an authenticated Playwright context.

    Args:
        context: An already-authenticated BrowserContext (from AuthManager).
        url: The URL to fetch and extract.
        post_text: Fallback text from the original post.

    Returns:
        ExtractedContent on success, None on failure.
    """
    logger.info("Playwright extraction: %s", url)
    page: Page | None = None
    try:
        page = await context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT)
        except Exception as exc:
            if "Timeout" in str(type(exc).__name__) or "timeout" in str(exc).lower():
                logger.warning("Playwright nav timeout for %s, trying partial content", url)
                await page.wait_for_timeout(3000)
            else:
                raise

        content = await _extract_from_page(page, url, post_text)
        return content
    except Exception:
        logger.exception("Playwright extraction failed for %s", url)
        return None
    finally:
        if page and not page.is_closed():
            await page.close()


async def extract_linkedin_post(
    context: BrowserContext,
    url: str,
    post_text: str | None = None,
) -> tuple[ExtractedContent | None, list[str]]:
    """Extract content and external URLs from a LinkedIn post in a single page visit.

    Returns (ExtractedContent | None, external_urls).
    """
    logger.info("Playwright LinkedIn extraction: %s", url)
    page: Page | None = None
    try:
        page = await context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT)
        except Exception:
            logger.warning("LinkedIn nav timeout for %s, trying partial content", url)
            await page.wait_for_timeout(5000)

        content = await _extract_linkedin_content(page, url, post_text)
        external_urls = await _extract_linkedin_external_urls(page)
        return content, external_urls
    except Exception:
        logger.exception("Playwright LinkedIn extraction failed for %s", url)
        return None, []
    finally:
        if page and not page.is_closed():
            await page.close()


async def _extract_from_page(
    page: Page,
    url: str,
    post_text: str | None = None,
) -> ExtractedContent | None:
    """Extract readable content from a fully loaded Playwright page.

    Tries multiple strategies:
    1. article/role=article elements
    2. Known content class/id selectors
    3. Full body text as last resort
    """
    await page.wait_for_timeout(2000)

    text = await page.evaluate(
        """(selectors) => {
            // Remove noise elements
            const removeSelectors = [
                'nav', 'header', 'footer', 'aside',
                '[role="navigation"]', '[role="banner"]', '[role="contentinfo"]',
                '[role="complementary"]', '.sidebar', '.ad', '.advertisement',
                '.social-share', '.share-buttons', '.related-posts',
                '.comments', '#comments', '.newsletter', '.popup', '.modal',
                'script', 'style', 'noscript', 'iframe'
            ];
            removeSelectors.forEach(sel => {
                document.querySelectorAll(sel).forEach(el => el.remove());
            });

            // Try each content selector
            for (const sel of selectors) {
                const el = document.querySelector(sel);
                if (el) {
                    const text = el.innerText.trim();
                    if (text.length > 200) return text;
                }
            }

            // Fallback: body text
            const body = document.body;
            if (body) {
                return body.innerText.trim();
            }
            return '';
        }""",
        CONTENT_SELECTORS,
    )

    if not text or len(text.strip()) < 50:
        logger.warning("Playwright extracted too little text from %s (%d chars)", url, len(text) if text else 0)
        return None

    text = _clean_extracted_text(text)

    if not text or len(text) < 50:
        return None

    return ExtractedContent(
        url=url,
        full_text=text[:MAX_CONTENT_LENGTH],
        post_text=post_text,
        extraction_method="playwright",
        extracted_at=datetime.now(UTC),
    )


async def _extract_linkedin_content(
    page: Page,
    url: str,
    post_text: str | None = None,
) -> ExtractedContent | None:
    """Extract content from a LinkedIn post page.

    LinkedIn has specific DOM patterns for posts. This uses targeted selectors
    before falling back to generic extraction.
    """
    # Wait for content to load — LinkedIn is slow even with cookies
    await page.wait_for_timeout(5000)

    # Try to scroll down to trigger lazy content loading
    try:
        await page.evaluate("window.scrollBy(0, 300)")
        await page.wait_for_timeout(2000)
    except Exception:
        pass

    # Dismiss cookie/auth walls if present
    await _dismiss_linkedin_overlay(page)

    # Try waiting for key content selectors
    try:
        await page.wait_for_selector(
            ".break-words, .update-components-text, article, [role='article'], main",
            timeout=8000,
        )
    except Exception:
        logger.warning("LinkedIn content selector timeout for %s, proceeding anyway", url)

    text = await page.evaluate(
        """() => {
            // Remove noise
            const removeSelectors = [
                'nav', 'footer', 'aside',
                '[role="navigation"]', '[role="banner"]', '[role="contentinfo"]',
                '.sidebar', '.ad', '.social-share', '.share-buttons',
                '.comments-container', '#comments',
                'script', 'style', 'noscript', 'iframe'
            ];
            removeSelectors.forEach(sel => {
                document.querySelectorAll(sel).forEach(el => el.remove());
            });

            // LinkedIn-specific selectors for post content
            const linkedInSelectors = [
                // Post content container (2024-2025 layout)
                '.update-components-text',
                '.feed-shared-update-v2__description',
                '.break-words',
                '[dir="ltr"] .break-words',
                // Article content
                '.article-content',
                // Generic article
                'article',
                '[role="article"]',
            ];

            for (const sel of linkedInSelectors) {
                const el = document.querySelector(sel);
                if (el) {
                    const t = el.innerText.trim();
                    if (t.length > 50) return t;
                }
            }

            // If the URL is an article (not a post), try article selectors
            const articleSelectors = [
                '.article-content',
                '.reader-article-content',
                '[data-article-body]',
                '.main-content',
            ];
            for (const sel of articleSelectors) {
                const el = document.querySelector(sel);
                if (el) {
                    const t = el.innerText.trim();
                    if (t.length > 50) return t;
                }
            }

            // Last resort: main content area
            const main = document.querySelector('main') || document.querySelector('[role="main"]');
            if (main) {
                return main.innerText.trim();
            }

            return '';
        }"""
    )

    if not text or len(text.strip()) < 50:
        logger.warning(
            "Playwright LinkedIn extraction too short for %s (%d chars), trying generic",
            url, len(text) if text else 0,
        )
        return await _extract_from_page(page, url, post_text)

    text = _clean_extracted_text(text)

    if not text or len(text.strip()) < 50:
        return None

    return ExtractedContent(
        url=url,
        full_text=text[:MAX_CONTENT_LENGTH],
        post_text=post_text,
        extraction_method="playwright_linkedin",
        extracted_at=datetime.now(UTC),
    )


async def _dismiss_linkedin_overlay(page: Page) -> None:
    """Try to dismiss cookie consent and auth-nag overlays on LinkedIn."""
    dismiss_selectors = [
        'button[action-type="ACCEPT"]',
        'button[data-tracking-control-name="cookie_consent_accept"]',
        'button.artdeco-modal__dismiss',
        '[aria-label="Dismiss"]',
        '[aria-label="Accept cookies"]',
        'button:has-text("Accept")',
        'button:has-text("Dismiss")',
    ]
    for sel in dismiss_selectors:
        try:
            button = await page.query_selector(sel)
            if button:
                await button.click(timeout=2000)
                await page.wait_for_timeout(500)
                break
        except Exception:
            pass


async def extract_x_thread(
    context: BrowserContext,
    tweet_url: str,
    post_text: str | None = None,
    author_handle: str | None = None,
) -> ExtractedContent | None:
    """Extract a complete X/Twitter thread by navigating to the tweet and
    scrolling to load all replies from the same author.

    Args:
        context: Authenticated BrowserContext with X.com cookies.
        tweet_url: URL of the first tweet in the thread.
        post_text: Fallback text from the scraper.
        author_handle: Twitter handle of the thread author (used to filter out
            replies from other users). Extracted from the tweet URL when possible.

    Returns:
        ExtractedContent with the full thread text concatenated.
    """
    # Derive author handle from URL if not provided (x.com/{handle}/status/{id})
    if not author_handle:
        m = re.search(r"x\.com/([A-Za-z0-9_]+)/status/", tweet_url)
        if m:
            author_handle = m.group(1)

    logger.info("Playwright X thread extraction: %s (author=%s)", tweet_url, author_handle)
    page: Page | None = None
    try:
        page = await context.new_page()
        try:
            await page.goto(tweet_url, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT)
        except Exception:
            logger.warning("X thread nav timeout for %s, trying partial content", tweet_url)
            await page.wait_for_timeout(5000)

        # Wait for tweet to render
        await page.wait_for_timeout(4000)

        # Dismiss cookie/consent overlays on X
        await _dismiss_x_overlay(page)

        # Scroll down to load thread replies (filter to author only)
        thread_tweets = await _collect_thread_tweets(page, author_handle)

        if thread_tweets:
            full_text = "\n\n---\n\n".join(thread_tweets)
            logger.info("X thread extraction: collected %d tweets from %s", len(thread_tweets), tweet_url)
        else:
            # Fallback: extract whatever is on the page
            logger.warning("X thread: no tweets collected, falling back to page extraction")
            single = await _extract_x_single_tweet(page)
            full_text = single or ""

        if not full_text or len(full_text.strip()) < 30:
            return None

        return ExtractedContent(
            url=tweet_url,
            full_text=full_text[:MAX_CONTENT_LENGTH],
            post_text=post_text,
            extraction_method="playwright_x_thread",
            extracted_at=datetime.now(UTC),
        )
    except Exception:
        logger.exception("Playwright X thread extraction failed for %s", tweet_url)
        return None
    finally:
        if page and not page.is_closed():
            await page.close()


async def _collect_thread_tweets(page: Page, author_handle: str | None = None) -> list[str]:
    """Scroll through a tweet page and collect all tweets from the same author.

    Filters by author_handle (extracted from the tweet URL) so that only the
    thread author's tweets are included, not replies from other users.
    Uses only [data-testid="tweetText"] to avoid picking up UI chrome like
    author names, Subscribe buttons, or translation prompts.
    """
    max_scrolls = 10
    scroll_delay = 2000

    all_tweets: list[str] = []
    seen_texts: set[str] = set()
    stable_count = 0  # scrolls without new tweets

    for scroll_num in range(max_scrolls):
        prev_count = len(all_tweets)

        tweets = await page.evaluate(
            """(authorHandle) => {
                const results = [];
                const seen = new Set();

                // Iterate every tweet card on the page
                const containers = document.querySelectorAll('[data-testid="tweet"]');
                containers.forEach(container => {
                    // Resolve the author handle for this specific tweet card.
                    // X.com renders the handle as a link like href="/username"
                    // inside [data-testid="User-Name"].
                    let tweetAuthor = null;
                    const nameEl = container.querySelector('[data-testid="User-Name"]');
                    if (nameEl) {
                        const links = nameEl.querySelectorAll('a[href]');
                        for (const a of links) {
                            const href = (a.getAttribute('href') || '').split('?')[0];
                            // href is like "/handle" (no path segments after)
                            const match = href.match(new RegExp('^/([A-Za-z0-9_]+)$'));
                            if (match) {
                                tweetAuthor = match[1].toLowerCase();
                                break;
                            }
                        }
                    }

                    // Skip tweets from other authors when a filter is active
                    if (authorHandle && tweetAuthor && tweetAuthor !== authorHandle.toLowerCase()) {
                        return;
                    }

                    // Use ONLY the dedicated tweet-text element to avoid UI chrome
                    const textEl = container.querySelector('[data-testid="tweetText"]');
                    if (!textEl) return;

                    const text = textEl.innerText.trim();
                    const key = text.substring(0, 100);
                    if (text && text.length > 10 && !seen.has(key)) {
                        seen.add(key);
                        results.push(text);
                    }
                });

                return results;
            }""",
            author_handle,
        )

        for tweet in (tweets or []):
            key = tweet[:100]
            if key not in seen_texts:
                seen_texts.add(key)
                all_tweets.append(tweet)

        # Scroll down to load more of the thread
        await page.evaluate("window.scrollBy(0, window.innerHeight)")
        await page.wait_for_timeout(scroll_delay)

        if len(all_tweets) == prev_count:
            stable_count += 1
            if stable_count >= 3:
                break  # No new content after 3 consecutive scrolls
        else:
            stable_count = 0

    return all_tweets


async def _extract_x_single_tweet(page: Page) -> str | None:
    """Extract text from a single tweet on the current page."""
    text = await page.evaluate(
        """() => {
            // Primary: data-testid="tweetText"
            const tweetText = document.querySelector('[data-testid="tweetText"]');
            if (tweetText) {
                const text = tweetText.innerText.trim();
                if (text.length > 10) return text;
            }

            // Fallback: article with lang attribute
            const article = document.querySelector('article [lang]');
            if (article) {
                return article.innerText.trim();
            }

            // Last resort: any tweet-like container
            const containers = document.querySelectorAll('[data-testid="tweet"]');
            for (const c of containers) {
                const lang = c.querySelector('[lang]');
                if (lang) {
                    const text = lang.innerText.trim();
                    if (text.length > 10) return text;
                }
            }
            return '';
        }"""
    )
    return text if text and len(text) > 10 else None


async def _dismiss_x_overlay(page: Page) -> None:
    """Try to dismiss cookie/consent overlays on X.com."""
    dismiss_selectors = [
        '[data-testid="button"] span:text("Accept all cookies")',
        '[data-testid="button"] span:text("Accept cookies")',
        '[data-testid="button"] span:text("Aceptar todas las cookies")',
        'button:has-text("Aceptar todas las cookies")',
    ]
    for sel in dismiss_selectors:
        try:
            button = await page.query_selector(sel)
            if button:
                await button.click(timeout=2000)
                await page.wait_for_timeout(1000)
                break
        except Exception:
            pass


async def _extract_linkedin_external_urls(page: Page) -> list[str]:
    """Extract external (non-LinkedIn) URLs from the already-loaded LinkedIn page."""
    try:
        urls = await page.evaluate(
            """() => {
                const links = new Set();
                const containers = [
                    '.update-components-text',
                    '.feed-shared-update-v2__description',
                    '.break-words',
                    '[dir="ltr"]',
                    'article',
                ];
                for (const sel of containers) {
                    document.querySelectorAll(sel).forEach(el => {
                        el.querySelectorAll('a[href]').forEach(a => {
                            const href = a.href;
                            if (href && href.startsWith('http') &&
                                !href.includes('linkedin.com') &&
                                !href.includes('/help/') &&
                                !href.includes('/hashtag/')) {
                                links.add(href);
                            }
                        });
                    });
                }
                if (links.size === 0) {
                    const main = document.querySelector('main, [role="main"]');
                    if (main) {
                        main.querySelectorAll('a[href]').forEach(a => {
                            const href = a.href;
                            if (href && href.startsWith('http') &&
                                !href.includes('linkedin.com') &&
                                !href.includes('/help/') &&
                                !href.includes('/hashtag/')) {
                                links.add(href);
                            }
                        });
                    }
                }
                return Array.from(links);
            }"""
        )
        return urls or []
    except Exception:
        logger.warning("LinkedIn external URL extraction failed")
        return []


def _clean_extracted_text(text: str) -> str:
    """Clean extracted text by removing noise and normalizing whitespace."""
    if not text:
        return ""

    # Remove common cookie/consent noise
    noise_patterns = [
        r"Privacy related extensions.*?try again\.",
        r"Something went wrong, but don't fret.*?try again\.",
        r"LinkedIn and 3rd parties use.*?Cookie Policy\.",
        r"Select Accept to consent.*?choices at any time\.",
        r"Manage settings.*?Read more in our Cookie Policy\.",
        r"We and our partners use cookies.*?privacy policy\.",
        r"By clicking Accept.*?continue\.",
        # X.com UI chrome that leaks into extracted text (multi-language)
        r"Suscribirse\n.*?(?:\n|$)",
        r"Haz clic para [Ss]uscribirse \S+\n?",
        r"Mostrar traducción\n?",
        r"Show translation\n?",
        r"^Subscribe\n.*?(?:\n|$)",
        r"^Follow\n.*?(?:\n|$)",
        # Spanish X.com cookie consent block
        r"¿Alguien dijo[^\n]*cookies\?[\s\S]*?(?:Rechazar cookies no necesarias|Aceptar todas las cookies)\n?",
        r"Para ver los atajos del teclado[^\n]*\n?",
        r"Ver atajos (?:del teclado|de teclado)\n?",
        r"Ver posts nuevos\n?",
    ]
    for pattern in noise_patterns:
        text = re.sub(pattern, "", text, flags=re.IGNORECASE | re.DOTALL | re.MULTILINE)

    # Remove excessive whitespace
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]+", " ", text)

    return text.strip()
