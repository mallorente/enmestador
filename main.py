"""Main orchestrator for the PKM ingestion pipeline.

Acquires a lock, scrapes X and LinkedIn bookmarks, deduplicates,
extracts web content, enriches via LLM, writes Markdown notes,
sends a Telegram summary, updates cursors, and releases the lock.

Any single-bookmark failure is caught, logged, and moved to dead letter
without aborting the pipeline.
"""

import asyncio
import json
import logging
import re
import sys
from contextlib import suppress
from pathlib import Path

from dotenv import load_dotenv
from patchright.async_api import BrowserContext

load_dotenv()

from auth.manager import AuthManager
from auth.x_manager import XAuthManager
from config import (
    DEFAULT_OUTPUT_DIR,
    DEFAULT_STATE_DIR,
    DEFAULT_USER_DATA_DIR,
    DELTA_FRONTIER_SIZE,
    DELTA_STOP_AFTER_KNOWN,
    MAX_BOOKMARKS_PER_SOURCE,
    MAX_CONCURRENT,
    MAX_EXTERNAL_ARTICLES,
)
from pipeline.dedupe import dedupe_vault_notes
from pipeline.frontier import BookmarkFrontierStore, collect_vault_note_urls
from pipeline.llm import EnrichmentError, LLMProcessor
from models import Bookmark, EnrichedBookmark, ExternalArticle, ExtractedContent, PipelineResult, ScrapeMode, Source
from pipeline.notifier import Notifier
from extractors.playwright import extract_linkedin_post, extract_with_playwright, extract_x_thread
from scrapers.linkedin import ScraperLinkedIn
from scrapers.x import ScraperX
from pipeline.state import CursorsStore, DeadLetterWriter, LockFile, ProcessedUrlStore, normalize_url
from extractors.web import extract as web_extract

logger = logging.getLogger(__name__)


def configure_logging() -> None:
    """Configure application logging without exposing third-party request URLs."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


async def run_pipeline(
    *,
    output_dir_override: Path | None = None,
    fresh_run: bool = False,
    sources: set[Source] | None = None,
    delta_only: bool = False,
    dry_run: bool = False,
) -> PipelineResult:
    """Execute the full PKM ingestion pipeline.

    Args:
        output_dir_override: Use this output dir instead of the default from config.
        fresh_run: If True, force BOOTSTRAP mode for all sources and skip
            deduplication against previously-processed URLs (useful for a
            full historical re-run into a new output directory).
        sources: Optional source filter. Defaults to both X and LinkedIn.
        delta_only: Force DELTA mode for selected sources, even with no cursor.
        dry_run: Scrape and report would-process counts without writing notes,
            processed URLs, cursors, or notifications.

    Returns a PipelineResult with run statistics.
    """
    state_dir = Path(DEFAULT_STATE_DIR)
    output_dir = output_dir_override if output_dir_override else Path(DEFAULT_OUTPUT_DIR)
    user_data_dir = Path(DEFAULT_USER_DATA_DIR)
    selected_sources = sources or {Source.X, Source.LINKEDIN}
    if fresh_run and delta_only:
        raise ValueError("fresh_run and delta_only cannot be used together")

    # --- Lock ---
    lock = LockFile(state_dir)
    if not lock.acquire():
        logger.info("Pipeline already running (lock file present). Exiting.")
        return PipelineResult(processed=0, enriched=0, dead_letter=0)

    logger.info("Lock acquired. Starting pipeline.")

    notifier = Notifier()
    cursors = CursorsStore(state_dir)
    processed_store = ProcessedUrlStore(state_dir)
    frontier_store = BookmarkFrontierStore(state_dir)
    dead_letter = DeadLetterWriter(state_dir)

    auth: AuthManager | None = None
    x_auth: XAuthManager | None = None
    bookmarks: list[Bookmark] = []

    try:
        known_urls_by_source = _known_urls_by_source(
            output_dir,
            processed_store,
            frontier_store,
            selected_sources,
        )

        # --- Auth ---
        if Source.X in selected_sources:
            x_auth = XAuthManager(user_data_dir=user_data_dir)
            await x_auth.ensure_browser()
        if Source.LINKEDIN in selected_sources:
            auth = AuthManager(user_data_dir=user_data_dir)
            await auth.ensure_browser()

        x_bookmarks: list[Bookmark] = []
        li_bookmarks: list[Bookmark] = []
        scraper_x: ScraperX | None = None
        scraper_li: ScraperLinkedIn | None = None

        # --- Scrape X ---
        if Source.X in selected_sources:
            logger.info("Domain: X scraper")
            x_mode = _requested_mode(cursors, Source.X, fresh_run, delta_only)
            if x_auth is None:
                raise RuntimeError("X auth context was not initialized")
            page_x = await x_auth.context.new_page()
            scraper_x = ScraperX(
                page=page_x,
                state_dir=state_dir,
                max_bookmarks=MAX_BOOKMARKS_PER_SOURCE,
                skip_processed=not fresh_run,
                persist_cursor=False,
                known_urls=known_urls_by_source.get(Source.X, set()) if not fresh_run else set(),
                stop_after_known=DELTA_STOP_AFTER_KNOWN if not fresh_run else 0,
                frontier_size=DELTA_FRONTIER_SIZE,
            )
            x_bookmarks = await scraper_x.scrape(x_mode)
            bookmarks.extend(x_bookmarks)
            logger.info("X scraper: %d candidate bookmarks", len(x_bookmarks))

        # --- Scrape LinkedIn ---
        if Source.LINKEDIN in selected_sources:
            logger.info("Domain: LinkedIn scraper")
            li_mode = _requested_mode(cursors, Source.LINKEDIN, fresh_run, delta_only)
            if auth is None:
                raise RuntimeError("LinkedIn auth context was not initialized")
            page_li = await auth.context.new_page()
            scraper_li = ScraperLinkedIn(
                page=page_li,
                state_dir=state_dir,
                max_posts=MAX_BOOKMARKS_PER_SOURCE,
                skip_processed=not fresh_run,
                known_urls=known_urls_by_source.get(Source.LINKEDIN, set()) if not fresh_run else set(),
                stop_after_known=DELTA_STOP_AFTER_KNOWN if not fresh_run else 0,
                frontier_size=DELTA_FRONTIER_SIZE,
            )
            li_bookmarks = await scraper_li.scrape(li_mode)
            bookmarks.extend(li_bookmarks)
            logger.info("LinkedIn scraper: %d candidate bookmarks", len(li_bookmarks))

        # --- Auth health checks (only when scrapers returned nothing) ---
        if (
            Source.X in selected_sources
            and not x_bookmarks
            and not (
                scraper_x is not None
                and scraper_x.saw_authenticated_bookmarks_endpoint
            )
        ):
            if x_auth is None:
                raise RuntimeError("X auth context was not initialized")
            x_ok = await _check_x_auth(x_auth.context)
            if not x_ok:
                logger.warning("X auth appears expired — cookies may need refreshing")
                with suppress(Exception):
                    await notifier.send_auth_expired("X")

        if Source.LINKEDIN in selected_sources and not li_bookmarks:
            if auth is None:
                raise RuntimeError("LinkedIn auth context was not initialized")
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

        if dry_run:
            dry_run_x_cursor = getattr(scraper_x, "last_cursor", None) if scraper_x else None
            dry_run_report = {
                "sources": sorted(source.value for source in selected_sources),
                "cursor_used": {
                    source.value: cursors.get(source).get("cursor")
                    for source in sorted(selected_sources, key=lambda item: item.value)
                },
                "scraped": len(bookmarks),
                "deduped": len(bookmarks) - len(new_bookmarks),
                "would_process": len(new_bookmarks),
                "frontier": {
                    source.value: {
                        "known_urls": len(known_urls_by_source.get(source, set())),
                    }
                    for source in sorted(selected_sources, key=lambda item: item.value)
                },
                "output_dir": str(output_dir),
                "state_dir": str(state_dir),
            }
            logger.info("Dry run report: %s", json.dumps(dry_run_report, sort_keys=True))
            return PipelineResult(
                processed=len(new_bookmarks),
                enriched=0,
                dead_letter=len(dead_letter.read_all()),
                new_cursor_x=(
                    dry_run_x_cursor
                    if Source.X in selected_sources and isinstance(dry_run_x_cursor, str)
                    else None
                ),
                new_cursor_linkedin=cursors.get(Source.LINKEDIN).get("cursor")
                if Source.LINKEDIN in selected_sources
                else None,
            )

        if not new_bookmarks:
            logger.info("No new bookmarks. Skipping enrichment.")

        # --- Process bookmarks concurrently ---
        llm = LLMProcessor()
        pw_context = auth.context if auth else None
        x_pw_context = x_auth.context if x_auth else None
        sem = asyncio.Semaphore(MAX_CONCURRENT)

        async def _process_task(bm: Bookmark) -> bool:
            """Wrapper: process one bookmark under the concurrency semaphore."""
            async with sem:
                try:
                    was_enriched = await _process_bookmark(
                        bm,
                        llm,
                        output_dir,
                        dead_letter,
                        notifier,
                        x_pw_context if bm.source == Source.X else pw_context,
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
        captured_x_cursor = getattr(scraper_x, "last_cursor", None) if scraper_x else None
        new_cursor_x = (
            captured_x_cursor
            if Source.X in selected_sources and isinstance(captured_x_cursor, str)
            else None
        )
        new_cursor_li = (
            cursors.get(Source.LINKEDIN).get("cursor")
            if Source.LINKEDIN in selected_sources
            else None
        )
        if Source.X in selected_sources:
            cursors.save(
                Source.X,
                new_cursor_x or cursors.get(Source.X).get("cursor"),
                ScrapeMode.DELTA.value,
            )
        if Source.LINKEDIN in selected_sources:
            cursors.save(Source.LINKEDIN, new_cursor_li, ScrapeMode.DELTA.value)

        if not fresh_run:
            if Source.X in selected_sources and scraper_x is not None:
                frontier_store.save(Source.X, scraper_x.boundary.recent_urls)
            if Source.LINKEDIN in selected_sources and scraper_li is not None:
                frontier_store.save(Source.LINKEDIN, scraper_li.boundary.recent_urls)

        # --- Vault-level dedupe ---
        if _is_vault_bookmarks_dir(output_dir):
            dedupe_result = dedupe_vault_notes(output_dir)
            if dedupe_result.moved:
                logger.info(
                    "Vault dedupe moved %d duplicate notes across %d groups; report=%s",
                    dedupe_result.moved,
                    dedupe_result.duplicate_groups,
                    dedupe_result.report_path,
                )
        else:
            logger.info("Skipping vault dedupe for non-vault output_dir=%s", output_dir)

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
        if x_auth:
            await x_auth.close()
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
    from pipeline.writer import Writer

    extracted: ExtractedContent | None = None

    if bookmark.source == Source.X:
        # X: Try Playwright thread extraction for full thread content
        thread_text = bookmark.post_text or bookmark.title or ""
        thread_method = "tweet_text"
        thread_images: list[str] | None = None

        if pw_context:
            logger.info("Trying X thread extraction for %s", bookmark.url)
            thread_content = await extract_x_thread(
                pw_context, str(bookmark.url), bookmark.post_text
            )
            if thread_content and thread_content.image_urls:
                thread_images = thread_content.image_urls
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
        if bookmark.referenced_tweet_urls:
            external_text += "\n\nReferenced tweets:\n" + "\n".join(
                f"- {u}" for u in bookmark.referenced_tweet_urls
            )
        extracted = ExtractedContent(
            url=bookmark.url,
            full_text=thread_text + external_text or None,
            post_text=bookmark.post_text,
            extraction_method=thread_method,
            external_urls=bookmark.external_urls,
            referenced_tweet_urls=bookmark.referenced_tweet_urls,
            image_urls=_merge_unique(bookmark.image_urls, thread_images) or None,
        )
        if extracted.image_urls:
            bookmark.image_urls = extracted.image_urls
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

        if extracted is not None:
            merged_images = _merge_unique(bookmark.image_urls, extracted.image_urls)
            if merged_images:
                extracted.image_urls = merged_images
                bookmark.image_urls = merged_images

    # External articles: try trafilatura, then Playwright fallback
    ext_urls = bookmark.external_urls or (li_urls if bookmark.source == Source.LINKEDIN and li_urls else None)
    if bookmark.source == Source.X and bookmark.referenced_tweet_urls and pw_context:
        referenced_articles: list[ExternalArticle] = []
        for tweet_url in bookmark.referenced_tweet_urls[:MAX_EXTERNAL_ARTICLES]:
            try:
                ref_content = await extract_x_thread(pw_context, tweet_url)
                if ref_content and ref_content.full_text:
                    referenced_articles.append(ExternalArticle(
                        url=tweet_url,
                        text=ref_content.full_text[:8000],
                        image_urls=ref_content.image_urls,
                        extraction_method="playwright_x_thread",
                    ))
                else:
                    referenced_articles.append(ExternalArticle(
                        url=tweet_url,
                        text=None,
                        extraction_method="failed_x_thread",
                    ))
            except Exception as exc:
                logger.warning("Failed to extract referenced tweet %s: %s", tweet_url, exc)
                referenced_articles.append(ExternalArticle(
                    url=tweet_url,
                    text=None,
                    extraction_method="error_x_thread",
                ))

        if referenced_articles:
            if extracted is not None:
                existing = extracted.external_articles or []
                extracted.external_articles = existing + referenced_articles
            else:
                extracted = ExtractedContent(
                    url=bookmark.url,
                    full_text=bookmark.post_text or bookmark.title,
                    post_text=bookmark.post_text,
                    extraction_method="post_only",
                    referenced_tweet_urls=bookmark.referenced_tweet_urls,
                    external_articles=referenced_articles,
                    image_urls=bookmark.image_urls,
                )

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
                            image_urls=pw_content.image_urls,
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
                existing = extracted.external_articles or []
                extracted.external_articles = existing + external_articles
            else:
                extracted = ExtractedContent(
                    url=bookmark.url,
                    full_text=bookmark.post_text or bookmark.title,
                    post_text=bookmark.post_text,
                    extraction_method="post_only",
                    external_urls=bookmark.external_urls,
                    referenced_tweet_urls=bookmark.referenced_tweet_urls,
                    external_articles=external_articles,
                    image_urls=bookmark.image_urls,
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


def _merge_unique(*groups: list[str] | None) -> list[str]:
    """Merge URL lists while preserving first-seen order."""
    merged: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for item in group or []:
            if item and item not in seen:
                seen.add(item)
                merged.append(item)
    return merged


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


def _requested_mode(
    cursors: CursorsStore,
    source: Source,
    fresh_run: bool,
    delta_only: bool,
) -> ScrapeMode:
    """Resolve scrape mode from explicit run flags and cursor state."""
    if fresh_run:
        return ScrapeMode.BOOTSTRAP
    if delta_only:
        return ScrapeMode.DELTA
    return _resolve_mode(cursors, source)


def _is_vault_bookmarks_dir(output_dir: Path) -> bool:
    """Return True for the production Obsidian notes layout."""
    return output_dir.name == "bookmarks" and (output_dir.parent / ".obsidian").exists()


def _known_urls_by_source(
    output_dir: Path,
    processed_store: ProcessedUrlStore,
    frontier_store: BookmarkFrontierStore,
    sources: set[Source],
) -> dict[Source, set[str]]:
    """Build known bookmark URL sets for delta boundary detection."""
    processed_urls = set(processed_store.load().keys())
    known: dict[Source, set[str]] = {}
    for source in sources:
        source_vault_urls = collect_vault_note_urls(output_dir / source.value)
        known[source] = processed_urls | source_vault_urls | frontier_store.load_known(source)
    return known


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
    # Spanish X.com cookie consent page
    re.compile(r"¿alguien dijo.*?cookies", re.IGNORECASE),
    re.compile(r"aceptar todas las cookies", re.IGNORECASE),
    re.compile(r"x y sus socios utilizan cookies", re.IGNORECASE),
    re.compile(r"rechazar cookies no necesarias", re.IGNORECASE),
    # Spanish generic cookie consent
    re.compile(r"nosotros y nuestros socios.*?cookies", re.IGNORECASE),
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
    parser.add_argument(
        "--source",
        choices=["x", "linkedin", "both"],
        default="both",
        help="Run only one source or both (default: both).",
    )
    parser.add_argument(
        "--delta-only",
        action="store_true",
        help="Force DELTA mode for selected sources, even if a cursor is missing.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Scrape and report would-process counts without writing notes, "
            "processed URLs, cursors, dedupe reports, or notifications."
        ),
    )
    args = parser.parse_args()

    configure_logging()

    if args.fresh_run and args.delta_only:
        parser.error("--fresh-run and --delta-only cannot be used together")

    if args.fresh_run and args.output_dir:
        args.output_dir.mkdir(parents=True, exist_ok=True)
        logger.info("Fresh run: output_dir=%s", args.output_dir)

    selected_sources = (
        {Source.X, Source.LINKEDIN}
        if args.source == "both"
        else {Source(args.source)}
    )
    result = asyncio.run(
        run_pipeline(
            output_dir_override=args.output_dir,
            fresh_run=args.fresh_run,
            sources=selected_sources,
            delta_only=args.delta_only,
            dry_run=args.dry_run,
        )
    )
    logger.info("Final result: %s", result.model_dump_json())
    sys.exit(0)


if __name__ == "__main__":
    main()
