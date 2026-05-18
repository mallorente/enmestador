"""Standalone test for Playwright-based web extraction.

Tests the extractor against real URLs using the same authenticated browser
context that the pipeline uses. This lets you validate extraction quality
before running the full pipeline.

Usage:
    python test_playwright_extractor.py
    python test_playwright_extractor.py --url https://example.com/article
    python test_playwright_extractor.py --linkedin https://www.linkedin.com/posts/...
    python test_playwright_extractor.py --all

Requires: browser cookies in volumes/user_data/ directory.
"""

import argparse
import asyncio
import logging
import sys

from dotenv import load_dotenv

load_dotenv()

from auth_manager import AuthManager
from config import DEFAULT_USER_DATA_DIR
from playwright_extractor import extract_linkedin_post, extract_with_playwright
from web_extractor import extract as web_extract

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

DEFAULT_TEST_URLS = {
    "x_external": [
        "https://t.co/RldBc4h3ls",
    ],
    "linkedin": [
        "https://www.linkedin.com/feed/update/urn:li:activity:7084486501568282625",
    ],
}


async def test_url(context, url: str, label: str = "", use_linkedin: bool = False) -> None:
    """Test extraction for a single URL using both methods."""
    print(f"\n{'='*70}")
    print(f"Testing: {label or url}")
    print(f"URL: {url}")
    print(f"{'='*70}")

    # 1. Try trafilatura (plain HTTP)
    print("\n--- Trafilatura (httpx) ---")
    try:
        traf_result = await web_extract(url)
        if traf_result:
            text_preview = (traf_result.full_text or "")[:500]
            print(f"  Method: {traf_result.extraction_method}")
            print(f"  Length: {len(traf_result.full_text or '')} chars")
            print(f"  Preview: {text_preview[:300]}...")
        else:
            print("  Result: None (extraction failed)")
    except Exception as e:
        print(f"  Error: {e}")
        traf_result = None

    # 2. Try Playwright
    print("\n--- Playwright ---")
    try:
        if use_linkedin:
            pw_result = await extract_linkedin_post(context, url)
        else:
            pw_result = await extract_with_playwright(context, url)

        if pw_result:
            text_preview = (pw_result.full_text or "")[:500]
            print(f"  Method: {pw_result.extraction_method}")
            print(f"  Length: {len(pw_result.full_text or '')} chars")
            print(f"  Preview: {text_preview[:300]}...")
        else:
            print("  Result: None (extraction failed)")
    except Exception as e:
        print(f"  Error: {e}")
        pw_result = None

    # 3. Compare
    print("\n--- Comparison ---")
    traf_len = len(traf_result.full_text or "") if traf_result else 0
    pw_len = len(pw_result.full_text or "") if pw_result else 0
    print(f"  Trafilatura: {traf_len} chars")
    print(f"  Playwright:  {pw_len} chars")
    if pw_len > traf_len:
        print(f"  WINNER: Playwright (+{pw_len - traf_len} chars)")
    elif traf_len > pw_len:
        print(f"  WINNER: Trafilatura (+{traf_len - pw_len} chars)")
    else:
        print("  TIE (or both failed)")


async def main() -> None:
    parser = argparse.ArgumentParser(description="Test Playwright extractor")
    parser.add_argument("--url", help="Test a specific URL with generic extraction")
    parser.add_argument("--linkedin", help="Test a specific LinkedIn URL")
    parser.add_argument("--all", action="store_true", help="Test all default URLs")
    parser.add_argument("--headless", action="store_true", help="Run browser headless")
    args = parser.parse_args()

    auth = AuthManager(user_data_dir=DEFAULT_USER_DATA_DIR, headless=args.headless)
    print("Launching browser...")
    await auth.ensure_browser()
    context = auth.context

    try:
        if args.url:
            await test_url(context, args.url, label="Custom URL")
        elif args.linkedin:
            await test_url(context, args.linkedin, label="Custom LinkedIn", use_linkedin=True)
        elif args.all:
            for label, urls in DEFAULT_TEST_URLS.items():
                use_li = label == "linkedin"
                for url in urls:
                    await test_url(context, url, label=label, use_linkedin=use_li)
        else:
            print("No URLs specified. Use --url, --linkedin, or --all")
            print("\nDefault test URLs:")
            for label, urls in DEFAULT_TEST_URLS.items():
                for url in urls:
                    print(f"  [{label}] {url}")
            sys.exit(1)
    finally:
        print("\nClosing browser...")
        await auth.close()


if __name__ == "__main__":
    asyncio.run(main())
