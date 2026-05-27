"""Export raw X and LinkedIn bookmarks without LLM or notifications.

This is intentionally narrower than main.py: it only authenticates to the
source sites, runs the bookmark scrapers, and writes local files for later
LLM-wiki work.
"""

import argparse
import asyncio
import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

load_dotenv()

from auth.manager import AuthManager
from auth.x_manager import XAuthManager
from config import DEFAULT_OUTPUT_DIR, DEFAULT_STATE_DIR, DEFAULT_USER_DATA_DIR, MAX_BOOKMARKS_PER_SOURCE
from models import Bookmark, EnrichedBookmark, ExtractedContent, ScrapeMode, Source
from pipeline.writer import Writer
from scrapers.linkedin import ScraperLinkedIn
from scrapers.x import ScraperX

logger = logging.getLogger(__name__)


def _bookmark_dict(bookmark: Bookmark) -> dict:
    return bookmark.model_dump(mode="json")


def _write_jsonl(path: Path, bookmarks: list[Bookmark]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for bookmark in bookmarks:
            f.write(json.dumps(_bookmark_dict(bookmark), ensure_ascii=False) + "\n")


def _write_notes(notes_dir: Path, bookmarks: list[Bookmark]) -> None:
    writer = Writer(notes_dir)
    for bookmark in bookmarks:
        content = ExtractedContent(
            url=bookmark.url,
            post_text=bookmark.post_text,
            extraction_method="post_only",
            external_urls=bookmark.external_urls,
            referenced_tweet_urls=bookmark.referenced_tweet_urls,
            image_urls=bookmark.image_urls,
        )
        writer.write(EnrichedBookmark(bookmark=bookmark, content=content, enrichment=None))


def _write_manifest(
    output_dir: Path,
    notes_dir: Path,
    bookmarks: list[Bookmark],
    max_items: int,
    sources: list[str],
) -> None:
    by_source = {source.value: 0 for source in Source}
    for bookmark in bookmarks:
        by_source[bookmark.source.value] = by_source.get(bookmark.source.value, 0) + 1

    manifest = {
        "exported_at": datetime.now(UTC).isoformat(),
        "sources_requested": sources,
        "max_items_per_source": max_items,
        "total": len(bookmarks),
        "by_source": by_source,
        "files": {
            "all_json": "all_bookmarks.json",
            "all_jsonl": "all_bookmarks.jsonl",
            "x_jsonl": "raw/x.jsonl",
            "linkedin_jsonl": "raw/linkedin.jsonl",
            "notes": str(notes_dir),
        },
        "notes": "Raw local export only: no LLM enrichment, no Telegram notification, no third-party article extraction.",
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    readme = [
        "# Raw Bookmark Export",
        "",
        f"- Exported at: {manifest['exported_at']}",
        f"- Total bookmarks: {len(bookmarks)}",
        f"- X: {by_source.get(Source.X.value, 0)}",
        f"- LinkedIn: {by_source.get(Source.LINKEDIN.value, 0)}",
        "",
        "This folder is intended as seed material for the LLM wiki.",
        "It contains raw scraper output only, without LLM enrichment or notifications.",
        "",
        "Useful entry points:",
        "",
        "- `all_bookmarks.json`",
        "- `all_bookmarks.jsonl`",
        "- `raw/x.jsonl`",
        "- `raw/linkedin.jsonl`",
        f"- `{notes_dir}`",
        "",
    ]
    (output_dir / "README.md").write_text("\n".join(readme), encoding="utf-8")


async def _scrape_x(
    state_dir: Path,
    user_data_dir: Path,
    max_items: int,
    auth_mode: str,
) -> list[Bookmark]:
    auth = (
        AuthManager(user_data_dir=user_data_dir)
        if auth_mode == "profile"
        else XAuthManager(user_data_dir=user_data_dir)
    )
    try:
        context = await auth.ensure_browser()
        page = await context.new_page()
        scraper = ScraperX(
            page=page,
            state_dir=state_dir,
            max_bookmarks=max_items,
            skip_processed=False,
        )
        return await scraper.scrape(ScrapeMode.BOOTSTRAP)
    finally:
        await auth.close()


async def _scrape_linkedin(state_dir: Path, user_data_dir: Path, max_items: int) -> list[Bookmark]:
    auth = AuthManager(user_data_dir=user_data_dir)
    try:
        context = await auth.ensure_browser()
        page = await context.new_page()
        scraper = ScraperLinkedIn(
            page=page,
            state_dir=state_dir,
            max_posts=max_items,
            skip_processed=False,
        )
        return await scraper.scrape(ScrapeMode.BOOTSTRAP)
    finally:
        await auth.close()


async def export_raw_bookmarks(
    output_dir: Path,
    notes_dir: Path,
    state_dir: Path,
    user_data_dir: Path,
    max_items: int,
    sources: list[str],
    x_auth_mode: str,
) -> list[Bookmark]:
    output_dir.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)

    bookmarks: list[Bookmark] = []
    if "x" in sources:
        logger.info("Scraping X bookmarks (auth_mode=%s)", x_auth_mode)
        x_bookmarks = await _scrape_x(state_dir, user_data_dir, max_items, x_auth_mode)
        bookmarks.extend(x_bookmarks)
        logger.info("X exported: %d", len(x_bookmarks))

    if "linkedin" in sources:
        logger.info("Scraping LinkedIn saved posts")
        linkedin_bookmarks = await _scrape_linkedin(state_dir, user_data_dir, max_items)
        bookmarks.extend(linkedin_bookmarks)
        logger.info("LinkedIn exported: %d", len(linkedin_bookmarks))

    all_dicts = [_bookmark_dict(bookmark) for bookmark in bookmarks]
    (output_dir / "all_bookmarks.json").write_text(
        json.dumps(all_dicts, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    _write_jsonl(output_dir / "all_bookmarks.jsonl", bookmarks)
    _write_jsonl(output_dir / "raw" / "x.jsonl", [bm for bm in bookmarks if bm.source == Source.X])
    _write_jsonl(
        output_dir / "raw" / "linkedin.jsonl",
        [bm for bm in bookmarks if bm.source == Source.LINKEDIN],
    )
    _write_notes(notes_dir, bookmarks)
    _write_manifest(output_dir, notes_dir, bookmarks, max_items, sources)
    return bookmarks


def main() -> None:
    parser = argparse.ArgumentParser(description="Export raw X/LinkedIn bookmarks locally")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--notes-dir", type=Path, default=Path(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--state-dir", type=Path, default=Path(DEFAULT_STATE_DIR))
    parser.add_argument("--user-data-dir", type=Path, default=Path(DEFAULT_USER_DATA_DIR))
    parser.add_argument("--max-items", type=int, default=MAX_BOOKMARKS_PER_SOURCE)
    parser.add_argument(
        "--source",
        choices=["x", "linkedin", "both"],
        default="both",
        help="Source to export",
    )
    parser.add_argument(
        "--x-auth-mode",
        choices=["cookies", "profile"],
        default="cookies",
        help="Use x_cookies.txt or the persistent Chromium profile for X",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    sources = ["x", "linkedin"] if args.source == "both" else [args.source]
    bookmarks = asyncio.run(
        export_raw_bookmarks(
            output_dir=args.output_dir,
            notes_dir=args.notes_dir,
            state_dir=args.state_dir,
            user_data_dir=args.user_data_dir,
            max_items=args.max_items,
            sources=sources,
            x_auth_mode=args.x_auth_mode,
        )
    )
    logger.info("Raw export complete: %d bookmarks", len(bookmarks))


if __name__ == "__main__":
    main()
