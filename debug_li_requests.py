"""Diagnostic: dump ALL API requests on LinkedIn saved posts page.

Run this to see the actual API URLs and response structures LinkedIn is using.
"""
import asyncio
import hashlib
import json
import os
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import async_playwright

from cookie_loader import load_netscape_cookies

load_dotenv()

USER_DATA_DIR = os.getenv("USER_DATA_DIR", "./volumes/user_data")


async def main() -> None:
    async with async_playwright() as p:
        user_data = Path(USER_DATA_DIR)

        # Launch browser with cookie injection (same as AuthManager)
        browser = await p.chromium.launch(
            headless=False,  # Visible so you can see what's happening
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/133.0.0.0 Safari/537.36"
            ),
        )

        # Anti-detection
        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => false });
            window.chrome = { runtime: {} };
        """)

        # Inject cookies
        for cookie_file in ["x_cookies.txt", "li_cookies.txt"]:
            cookie_path = user_data / cookie_file
            if cookie_path.exists():
                cookies = load_netscape_cookies(cookie_path)
                if cookies:
                    await context.add_cookies(cookies)
                    print(f"Loaded {len(cookies)} cookies from {cookie_file}")

        page = await context.new_page()

        # Log ALL responses and save voyager/API responses to disk
        saved = []

        def log_response(response):
            url = response.url
            if "voyager" in url.lower() or "saved" in url.lower() or "/api/" in url.lower():
                print(f"[CAPTURED] {response.status} {url[:150]}")
                saved.append(response)
            elif "graphql" in url.lower() or "api" in url.lower():
                print(f"[API]       {response.status} {url[:150]}")

        page.on("response", log_response)

        print("Navigating to LinkedIn saved posts...")
        await page.goto(
            "https://www.linkedin.com/my-items/saved-posts/",
            wait_until="domcontentloaded",
            timeout=60000,
        )

        print("Waiting 20 seconds for API requests to fire...")
        await page.wait_for_timeout(20000)

        print("\nScrolling to trigger more requests...")
        for i in range(5):
            await page.evaluate("window.scrollBy(0, window.innerHeight)")
            await page.wait_for_timeout(3000)
            print(f"  Scroll {i+1}/5")

        # Dump captured responses to files for analysis
        output_dir = Path("debug_li_output")
        output_dir.mkdir(exist_ok=True)

        print(f"\nDumping {len(saved)} captured responses...")
        for resp in saved:
            try:
                body = await resp.text()
                url_hash = hashlib.md5(resp.url.encode()).hexdigest()[:8]
                status = resp.status
                path = output_dir / f"{status}_{url_hash}.json"
                path.write_text(body, encoding="utf-8")
                print(f"  Saved: {path.name} ({len(body)} bytes)")
            except Exception as e:
                print(f"  Failed to read response: {e}")

        print(f"\nDone. Check {output_dir.absolute()} for response dumps.")
        print("Press Enter to close the browser...")
        input()
        await context.close()
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())