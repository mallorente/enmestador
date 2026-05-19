"""Auth manager for the PKM ingestion pipeline.

Launches a Playwright browser context and injects exported cookies for X.com
and LinkedIn authentication.
"""

import logging
import os
from pathlib import Path

from patchright.async_api import BrowserContext, async_playwright

from auth.cookie_loader import load_netscape_cookies

logger = logging.getLogger(__name__)


class AuthManager:
    """Manages a Playwright browser context with injected cookies.

    Expects Netscape-format cookies.txt files in the user_data_dir:
        - x_cookies.txt   for x.com
        - li_cookies.txt  for linkedin.com

    These files should be exported from your real browser using an extension
    like "Get cookies.txt LOCALLY" or "Export Cookies".
    """

    def __init__(
        self,
        user_data_dir: str | Path,
        headless: bool | None = None,
    ) -> None:
        self.user_data_dir = Path(user_data_dir)
        # Explicit arg wins; otherwise read HEADLESS env var (default: true)
        if headless is None:
            headless = os.getenv("HEADLESS", "true").strip().lower() != "false"
        self._headless = headless
        self._playwright = None
        self._context: BrowserContext | None = None

    @property
    def context(self) -> BrowserContext | None:
        """Return the running BrowserContext, or None."""
        return self._context

    async def ensure_browser(self) -> BrowserContext:
        """Launch browser with persistent profile, and inject cookies."""
        if self._context is not None:
            return self._context

        self.user_data_dir.mkdir(parents=True, exist_ok=True)
        logger.info("Launching browser with persistent profile (dir: %s)", self.user_data_dir)

        self._playwright = await async_playwright().start()
        headless = self._headless if self._headless is not None else True

        launch_args = [
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-infobars",
            "--disable-dev-shm-usage",
        ]
        if not headless:
            launch_args.append("--window-position=-32000,-32000")

        # Use persistent context so LinkedIn session survives
        # (localStorage, IndexedDB, cookies all persist in user_data_dir)
        self._context = await self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(self.user_data_dir),
            headless=headless,
            args=launch_args,
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/133.0.0.0 Safari/537.36"
            ),
        )

        # Inject anti-detection script into every page
        await self._context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => false });
            window.chrome = { runtime: {} };
            Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3,4,5] });
            Object.defineProperty(navigator, 'languages', { get: () => ['en-US','en'] });
        """)

        # Inject cookies from Netscape files as supplement to profile data
        await self._inject_cookies("x_cookies.txt")
        li_cookies = await self._inject_cookies("li_cookies.txt")

        # For LinkedIn: intercept requests and add csrf-token header from JSESSIONID.
        # LinkedIn's Voyager API requires this header for authenticated calls.
        if li_cookies:
            jsessionid = None
            for cookie in li_cookies:
                if cookie["name"] == "JSESSIONID":
                    jsessionid = cookie["value"].strip('"')
                    break
            if jsessionid:
                csrf_token = jsessionid
                logger.info("Setting LinkedIn CSRF token from JSESSIONID")

                async def _add_linkedin_headers(route):
                    headers = dict(route.request.headers)
                    headers["csrf-token"] = csrf_token
                    headers["x-restli-protocol-version"] = "2.0.0"
                    await route.continue_(headers=headers)

                await self._context.route(
                    "**/voyager/**",
                    _add_linkedin_headers,
                )

        logger.info("Browser context ready (persistent profile, headless=%s)", headless)
        return self._context

    async def _inject_cookies(self, filename: str) -> list[dict] | None:
        """Load a Netscape cookies file and inject into the browser context.

        Returns the list of cookies loaded, or None if the file doesn't exist.
        """
        cookie_path = self.user_data_dir / filename
        if not cookie_path.exists():
            logger.warning("Cookie file not found: %s", cookie_path)
            return None

        cookies = load_netscape_cookies(cookie_path)
        if cookies:
            await self._context.add_cookies(cookies)
            logger.info("Injected %d cookies from %s", len(cookies), filename)
        return cookies

    async def close(self) -> None:
        """Close the persistent browser context and playwright instance."""
        if self._context is not None:
            await self._context.close()
            self._context = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None
        logger.info("Browser closed")
