"""Diagnostic: dump ALL XHR/Fetch requests on X.com bookmarks page.

Run this to see the actual GraphQL URLs X.com is using.
"""
import asyncio
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

USER_DATA_DIR = os.getenv("USER_DATA_DIR", "./volumes/user_data")


async def main() -> None:
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        user_data = Path(USER_DATA_DIR)
        if not user_data.exists():
            print(f"ERROR: user_data_dir not found at {user_data.absolute()}")
            print("Run setup_auth.py first to log in.")
            return

        context = await p.chromium.launch_persistent_context(
            user_data_dir=str(user_data),
            headless=False,  # Visible so you can see what's happening
            viewport={"width": 1280, "height": 800},
            args=["--disable-blink-features=AutomationControlled"],
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/133.0.0.0 Safari/537.36"
            ),
        )

        page = await context.new_page()

        # Log ALL responses
        def log_response(response):
            url = response.url
            if "graphql" in url.lower():
                print(f"[GRAPHQL] {url[:150]}")
            elif "api" in url.lower():
                print(f"[API]     {url[:150]}")

        page.on("response", log_response)

        print("Navigating to X.com bookmarks...")
        await page.goto("https://x.com/bookmarks", wait_until="domcontentloaded", timeout=60000)

        print("Waiting 15 seconds for requests to fire...")
        await page.wait_for_timeout(15000)

        print("\nScrolling to trigger more requests...")
        for i in range(3):
            await page.evaluate("window.scrollBy(0, 1000)")
            await page.wait_for_timeout(3000)
            print(f"  Scroll {i+1}/3")

        print("\nDone. Check the output above for GraphQL URLs.")
        print("Press Enter to close the browser...")
        input()
        await context.close()


if __name__ == "__main__":
    asyncio.run(main())
