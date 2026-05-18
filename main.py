"""Main orchestrator for the PKM ingestion pipeline.

Acquires a lock, scrapes X and LinkedIn bookmarks, deduplicates,
extracts web content, enriches via LLM, writes Markdown notes,
sends a Telegram summary, updates cursors, and releases the lock.

Any single-bookmark failure is caught, logged, and moved to dead letter
without aborting the pipeline.
"""

import asyncio
import logging
import re
import sys
from contextlib import suppress
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import BrowserContext

load_dotenv()

from auth_manager import AuthManager
from config import (
    DEFAULT_OUTPUT_DIR,
    DEFAULT_STATE_DIR,
    DEFAULT_USER_DATA_DIR,
    MAX_BOOKMARKS_PER_SOURCE,
    MAX_CONCURRENT,
    MAX_EXTERNAL_ARTICLES,
)
from llm_processor import EnrichmentError, LLMProcessor
from models import Bookmark, EnrichedBookmark, ExternalArticle, ExtractedContent, PipelineResult, ScrapeMode, Source
from notifier import Notifier
from playwright_extractor import extract_linkedin_post, extract_with_playwright, extract_x_thread
from scraper_linkedin import ScraperLinkedIn
from scraper_x import ScraperX
from state import CursorsStore, DeadLetterWriter, LockFile, ProcessedUrlStore, normalize_url
from web_extractor import extract as web_extract

logger = logging.getLogger(__name__)


async def run_pipeline(
    *,
    output_dir_override: Path | None = None,
    fresh_run: bool = False,
) -> PipelineResult:
    """Execute the full PKM ingestion pipeline.

    Args:
        output_dir_override: Use this output dir instead of the default from config.
        fresh_run: If True, force BOOTSTRAP mode for all sources and skip
            deduplication against previously-processed URLs (useful for a
            full historical re-run into a new output directory).

    Returns a PipelineResult with run statistics.
    """
    state_dir = Path(DEFAULT_STATE_DIR)
    output_dir = output_dir_override if output_dir_override else Path(DEFAULT_OUTPUT_DIR)
    user_data_dir = Path(DEFAULT_USER_DATA_DIR)

    # --- Lock ---
    lock = LockFile(state_dir)
    if not lock.acquire():
        logger.info("Pipeline already running (lock file present). Exiting.")
        return PipelineResult(processed=0, enriched=0, dead_letter=0)

    logger.info("Lock acquired. Starting pipeline.")

    notifier = Notifier()
    cursors = CursorsStore(state_dir)
    processed_store = ProcessedUrlStore(state_dir)
    dead_letter = DeadLetterWriter(state_dir)

    auth: AuthManager | None = None
    bookmarks: list[Bookmark] = []

    try:
        # --- Auth ---
        auth = AuthManager(user_data_dir=user_data_dir)
        await auth.ensure_browser()

        # --- Scrape X ---
        logger.info("Domain: X scraper")
        x_mode = ScrapeMode.BOOTSTRAP if fresh_run else _resolve_mode(cursors, Source.X)
        page_x = await auth.context.new_page()
        scraper_x = ScraperX(page=page_x, state_dir=state_dir, max_bookmarks=MAX_BOOKMARKS_PER_SOURCE)
        x_bookmarks = await scraper_x.scrape(x_mode)
        bookmarks.extend(x_bookmarks)
        logger.info("X scraper: %d new bookmarks", len(x_bookmarks))

        # --- Scrape LinkedIn ---
        logger.info("Domain: LinkedIn scraper")
        li_mode = ScrapeMode.BOOTSTRAP if fresh_run else _resolve_mode(cursors, Source.LINKEDIN)
        page_li = await auth.context.new_page()
        scraper_li = ScraperLinkedIn(page=page_li, state_dir=state_dir, max_posts=MAX_BOOKMARKS_PER_SOURCE)
        li_bookmarks = await scraper_li.scrape(li_mode)
        bookmarks.extend(li_bookmarks)
        logger.info("LinkedIn scraper: %d new bookmarks", len(li_bookmarks))

        # --- Auth health checks (only when scrapers returned nothing) ---
        if not x_bookmarks:
            x_ok = await _check_x_auth(auth.context)
            if not x_ok:
                logger.warning("X auth appears expired — cookies may need refreshing")
                with suppress(Exception):
                    await notifier.send_auth_expired("X")

        if not li_bookmarks:
            li_ok = await _check_linkedin_auth(auth.context)
            if not li_ok:
                logger.warning("LinkedIn auth appears expired — cookies may need refreshing")
                with suppress(Exception):
                    await notifier.send_auth_expired("LinkedIn")

        # --- Deduplicate against processed URLs ---
        # In fresh_run mode we skip dedup so all scraped bookmarks are processed
        processed_urls = {} if fresh_run else processed_store.load()
        new_bookmarks = [
            bm for bm in bookmarks
            if normalize_url(str(bm.url)) not in processed_urls
        ]
        logger.info("After dedup: %d bookmarks to process (fresh_run=%s)", len(new_bookmarks), fresh_run)

        if not new_bookmarks:
            logger.info("No new bookmarks. Skipping enrichment.")

        # --- Process bookmarks concurrently ---
        llm = LLMProcessor()
        pw_context = auth.context
        sem = asyncio.Semaphore(MAX_CONCURRENT)

        async def _process_task(bm: Bookmark) -> bool:
            """Wrapper: process one bookmark under the concurrency semaphore."""
            async with sem:
                try:
                    was_enriched = await _process_bookmark(
                        bm, llm, output_dir, dead_letter, notifier, pw_context
                    )
                    processed_store.add(normalize_url(str(bm.url)), bm.source.value)
                    return was_enriched
                except Exception as exc:
                    logger.exception("Failed to process bookmark %s", bm.url)
                    dead_letter.append(_dead_letter_for(bm, str(exc), "processing"))
                    processed_store.add(normalize_url(str(bm.url)), bm.source.value)
                    with suppress(Exception):
                        await notifier.send_bookmark_error(
                            bm.source.value, str(bm.url), "processing", str(exc)
                        )
                    return False

        results = await asyncio.gather(
            *[_process_task(bm) for bm in new_bookmarks]
        )
        enriched_count = sum(1 for r in results if r)

        # --- Update cursors ---
        new_cursor_x = cursors.get(Source.X).get("cursor")
        new_cursor_li = cursors.get(Source.LINKEDIN).get("cursor")
        cursors.save(Source.X, new_cursor_x, ScrapeMode.DELTA.value)
        cursors.save(Source.LINKEDIN, new_cursor_li, ScrapeMode.DELTA.value)

        # --- Summary ---
        result = PipelineResult(
            processed=len(new_bookmarks),
            enriched=enriched_count,
            dead_letter=len(dead_letter.read_all()),
            new_cursor_x=new_cursor_x,
            new_cursor_linkedin=new_cursor_li,
        )

        logger.info(
            "Pipeline complete: processed=%d enriched=%d dead=%d",
            result.processed, result.enriched, result.dead_letter,
        )

        # --- Notify ---
        try:
            await notifier.send_summary(result)
        except Exception:
            logger.exception("Failed to send summary notification")

        return result

    except Exception as exc:
        logger.exception("Pipeline crashed")
        with suppress(Exception):
            await notifier.send("main", f"Pipeline crashed: {exc}")
        raise
    finally:
        if auth:
            await auth.close()
        lock.release()
        logger.info("Lock released.")


async def _process_bookmark(
    bookmark: Bookmark,
    llm: LLMProcessor,
    output_dir: Path,
    dead_letter: DeadLetterWriter,
    notifier: Notifier,
    pw_context: "BrowserContext | None" = None,
) -> bool:
    """Process a single bookmark: extract → enrich → write.

    Returns True if LLM enrichment succeeded, False if it failed but the
    bookmark was still written without enrichment.
    Raises on any other failure so the caller can catch and dead-letter.
    """
    from writer import Writer

    extracted: ExtractedContent | None = None

    if bookmark.source == Source.X:
        # X: Try Playwright thread extraction for full thread content
        thread_text = bookmark.post_text or bookmark.title or ""
        thread_method = "tweet_text"

        if pw_context:
            logger.info("Trying X thread extraction for %s", bookmark.url)
            thread_content = await extract_x_thread(
                pw_context, str(bookmark.url), bookmark.post_text
            )
            if (
                thread_content
                and thread_content.full_text
                and len(thread_content.full_text.strip()) > len(thread_text.strip())
            ):
                thread_text = thread_content.full_text
                thread_method = thread_content.extraction_method

        # Append external links reference
        external_text = ""
        if bookmark.external_urls:
            external_text = "\n\nReferenced links:\n" + "\n".join(
                f"- {u}" for u in bookmark.external_urls
            )
        extracted = ExtractedContent(
            url=bookmark.url,
            full_text=thread_text + external_text or None,
            post_text=bookmark.post_text,
            extraction_method=thread_method,
            external_urls=bookmark.external_urls,
        )
    else:
        # LinkedIn: single Playwright visit extracts content + external URLs together
        extracted = None
        li_urls: list[str] = list(bookmark.external_urls) if bookmark.external_urls else []
        if pw_context:
            logger.info("LinkedIn Playwright extraction for %s", bookmark.url)
            extracted, pw_urls = await extract_linkedin_post(
                pw_context, str(bookmark.url), bookmark.post_text
            )
            if pw_urls and not li_urls:
                li_urls = pw_urls
                bookmark.external_urls = li_urls

        # Trafilatura fallback if Playwright failed or returned insufficient content
        if (
            extracted is None
            or not extracted.full_text
            or len(extracted.full_text.strip()) < 50
            or _is_cookie_noise(extracted.full_text)
        ):
            logger.info("LinkedIn Playwright insufficient for %s, trying trafilatura", bookmark.url)
            traf_extracted = await web_extract(str(bookmark.url), bookmark.post_text)
            if traf_extracted and traf_extracted.full_text and not _is_cookie_noise(traf_extracted.full_text):
                if extracted is None or len(traf_extracted.full_text) > len(extracted.full_text or ""):
                    extracted = traf_extracted

    # External articles: try trafilatura, then Playwright fallback
    ext_urls = bookmark.external_urls or (li_urls if bookmark.source == Source.LINKEDIN and li_urls else None)
    if ext_urls:
        external_articles: list[ExternalArticle] = []
        for ext_url in ext_urls[:MAX_EXTERNAL_ARTICLES]:
            try:
                # Playwright primary
                if pw_context:
                    pw_content = await extract_with_playwright(pw_context, ext_url)
                    if (
                        pw_content
                        and pw_content.full_text
                        and not _is_cookie_noise(pw_content.full_text)
                        and len(pw_content.full_text.strip()) >= 200
                    ):
                        external_articles.append(ExternalArticle(
                            url=ext_url,
                            text=pw_content.full_text[:8000],
                            extraction_method="playwright",
                        ))
                        continue

                # Trafilatura fallback
                ext_content = await web_extract(ext_url)
                if ext_content and ext_content.full_text and not _is_cookie_noise(ext_content.full_text) and len(ext_content.full_text.strip()) >= 200:
                    external_articles.append(ExternalArticle(
                        url=ext_url,
                        text=ext_content.full_text[:8000],
                        extraction_method="trafilatura",
                    ))
                    continue

                external_articles.append(ExternalArticle(
                    url=ext_url,
                    text=None,
                    extraction_method="failed",
                ))
            except Exception as exc:
                logger.warning("Failed to extract external URL %s: %s", ext_url, exc)
                external_articles.append(ExternalArticle(
                    url=ext_url,
                    text=None,
                    extraction_method="error",
                ))

        if external_articles:
            if extracted is not None:
                extracted.external_articles = external_articles
            else:
                extracted = ExtractedContent(
                    url=bookmark.url,
                    full_text=bookmark.post_text or bookmark.title,
                    post_text=bookmark.post_text,
                    extraction_method="post_only",
                    external_urls=bookmark.external_urls,
                    external_articles=external_articles,
                )

    # Enrich via LLM
    enriched_success = True
    try:
        enriched = await llm.enrich(bookmark, extracted)
    except EnrichmentError as exc:
        logger.warning("LLM enrichment failed for %s: %s", bookmark.url, exc)
        dead_letter.append(_dead_letter_for(bookmark, str(exc), "llm"))
        with suppress(Exception):
            await notifier.send_bookmark_error(
                bookmark.source.value, str(bookmark.url), "llm", str(exc)
            )
        enriched_success = False
        # Create a non-enriched bookmark to write anyway
        extracted_content = extracted or ExtractedContent(
            url=bookmark.url,
            post_text=bookmark.post_text,
            extraction_method="post_only",
        )
        enriched = EnrichedBookmark(
            bookmark=bookmark,
            content=extracted_content,
            enrichment=None,
        )

    # Write the note
    writer = Writer(output_dir)
    writer.write(enriched)

    return enriched_success


async def _check_x_auth(pw_context: "BrowserContext") -> bool:
    """Return True if the X.com session is still authenticated."""
    page = await pw_context.new_page()
    try:
        await page.goto("https://x.com/i/bookmarks", wait_until="domcontentloaded", timeout=20000)
        await page.wait_for_timeout(2000)
        return "login" not in page.url
    except Exception:
        return False
    finally:
        if not page.is_closed():
            await page.close()


async def _check_linkedin_auth(pw_context: "BrowserContext") -> bool:
    """Return True if the LinkedIn session is still authenticated."""
    page = await pw_context.new_page()
    try:
        await page.goto(
            "https://www.linkedin.com/my-items/saved-posts/",
            wait_until="domcontentloaded",
            timeout=20000,
        )
        await page.wait_for_timeout(2000)
        url = page.url
        return "login" not in url and "authwall" not in url
    except Exception:
        return False
    finally:
        if not page.is_closed():
            await page.close()


def _resolve_mode(cursors: CursorsStore, source: Source) -> ScrapeMode:
    """Determine scrape mode from cursor state."""
    cursor_data = cursors.get(source)
    if cursor_data.get("cursor"):
        return ScrapeMode.DELTA
    return ScrapeMode.BOOTSTRAP


def _dead_letter_for(bookmark: Bookmark, error: str, stage: str):
    """Create a DeadLetter for a failed bookmark."""
    from models import DeadLetter
    return DeadLetter(
        bookmark=bookmark,
        error=error,
        stage=stage,
    )


_COOKIE_NOISE_PATTERNS = [
    re.compile(r"privacy related extensions", re.IGNORECASE),
    re.compile(r"something went wrong, but don'?t fret", re.IGNORECASE),
    re.compile(r"select accept to consent", re.IGNORECASE),
    re.compile(r"linkedin and 3rd parties use.*cookie", re.IGNORECASE),
    re.compile(r"manage settings.*cookie policy", re.IGNORECASE),
]


def _is_cookie_noise(text: str | None) -> bool:
    """Check if extracted text is just cookie consent or error page noise."""
    if not text:
        return True
    text_lower = text.lower()
    return any(p.search(text_lower) for p in _COOKIE_NOISE_PATTERNS)


def main() -> None:
    """Entry point for the pipeline."""
    import argparse

    parser = argparse.ArgumentParser(description="PKM ingestion pipeline")
    parser.add_argument(
        "--fresh-run",
        action="store_true",
        help=(
            "Force BOOTSTRAP mode for all sources and skip dedup against previously-processed "
            "URLs. Use with --output-dir to write into a clean directory."
        ),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        metavar="DIR",
        help="Override the output directory (default: from OUTPUT_DIR env var).",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    if args.fresh_run and args.output_dir:
        args.output_dir.mkdir(parents=True, exist_ok=True)
        logger.info("Fresh run: output_dir=%s", args.output_dir)

    result = asyncio.run(
        run_pipeline(
            output_dir_override=args.output_dir,
            fresh_run=args.fresh_run,
        )
    )
    logger.info("Final result: %s", result.model_dump_json())
    sys.exit(0)


if __name__ == "__main__":
    main()
