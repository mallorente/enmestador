"""End-to-end test: thread collector + LinkedIn URL extraction + Playwright fallback."""
import asyncio
from datetime import UTC, datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from auth_manager import AuthManager
from config import DEFAULT_USER_DATA_DIR
from playwright_extractor import (
    extract_linkedin_post,
    extract_linkedin_urls,
    extract_with_playwright,
    extract_x_thread,
)
from web_extractor import extract as web_extract

OUTPUT_FILE = Path("test_output/full_extraction_test.md")


async def main():
    output_lines: list[str] = []
    output_lines.append("# Full Extraction Test: Thread Collector + LinkedIn URLs + Playwright")
    output_lines.append(f"Date: {datetime.now(UTC).isoformat()}\n")

    auth = AuthManager(user_data_dir=DEFAULT_USER_DATA_DIR, headless=True)
    await auth.ensure_browser()
    context = auth.context

    try:
        # =====================================================================
        # TEST 1: X Thread Collector
        # =====================================================================
        output_lines.append("## 1. X Thread Collector\n")
        tweet_url = "https://x.com/dair_ai/status/2048909068409147460"
        output_lines.append(f"URL: `{tweet_url}`\n")

        print("Testing X thread extraction...")
        thread = await extract_x_thread(context, tweet_url)
        if thread and thread.full_text:
            safe = thread.full_text.encode("ascii", errors="replace").decode("ascii")
            output_lines.append(f"- Method: `{thread.extraction_method}`")
            output_lines.append(f"- Length: {len(thread.full_text)} chars\n")
            output_lines.append(f"```\n{safe[:2000]}\n```\n")
        else:
            output_lines.append("**Result: None (thread extraction failed)**\n")

        output_lines.append("---\n")

        # =====================================================================
        # TEST 2: LinkedIn Post + URL Extraction
        # =====================================================================
        output_lines.append("## 2. LinkedIn Post Extraction + URL Discovery\n")
        li_url = "https://www.linkedin.com/feed/update/urn:li:activity:7084486501568282625"
        output_lines.append(f"URL: `{li_url}`\n")

        print("Testing LinkedIn post extraction...")
        li_content = await extract_linkedin_post(context, li_url)
        if li_content and li_content.full_text:
            safe = li_content.full_text.encode("ascii", errors="replace").decode("ascii")
            output_lines.append(f"- Method: `{li_content.extraction_method}`")
            output_lines.append(f"- Length: {len(li_content.full_text)} chars\n")
            output_lines.append(f"```\n{safe[:2000]}\n```\n")
        else:
            output_lines.append("**Result: None (post extraction failed)**\n")

        print("Testing LinkedIn URL extraction...")
        li_urls = await extract_linkedin_urls(context, li_url)
        output_lines.append(f"- External URLs found: {len(li_urls)}\n")
        for u in li_urls:
            output_lines.append(f"  - {u}")

        output_lines.append("\n---\n")

        # =====================================================================
        # TEST 3: Comparison table
        # =====================================================================
        output_lines.append("## 3. Comparison Summary\n")
        output_lines.append("| Source | Trafilatura | Playwright | Improvement |")
        output_lines.append("|--------|-------------|------------|-------------|")

        # X tweet comparison
        traf_x = await web_extract(tweet_url)
        traf_x_len = len(traf_x.full_text) if traf_x and traf_x.full_text else 0
        pw_x_len = len(thread.full_text) if thread and thread.full_text else 0
        output_lines.append(
            f"| X tweet | {traf_x_len} chars | {pw_x_len} chars | "
            f"+{pw_x_len - traf_x_len} chars ({'thread' if pw_x_len > traf_x_len else 'no improve'}) |"
        )

        # LinkedIn comparison
        traf_li = await web_extract(li_url)
        traf_li_len = len(traf_li.full_text) if traf_li and traf_li.full_text else 0
        pw_li_len = len(li_content.full_text) if li_content and li_content.full_text else 0
        output_lines.append(
            f"| LinkedIn | {traf_li_len} chars | {pw_li_len} chars | "
            f"+{pw_li_len - traf_li_len} chars |"
        )

        output_lines.append("\n---\n")

    finally:
        await auth.close()

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.write_text("\n".join(output_lines), encoding="utf-8")
    print(f"\nResults written to: {OUTPUT_FILE}")


if __name__ == "__main__":
    asyncio.run(main())