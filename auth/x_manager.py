"""X.com auth manager using exported cookies in a clean browser context.

X regressed after moving the shared browser profile to Patchright/persistent
login for LinkedIn: the app can get stuck on the black X loader while the same
account cookies worked in the older cookie-injection flow. Keep X isolated from
the LinkedIn persistent profile and inject x_cookies.txt into a fresh context.
"""

import logging
import os
from pathlib import Path

from patchright.async_api import Browser, BrowserContext, async_playwright

from auth.cookie_loader import load_netscape_cookies

logger = logging.getLogger(__name__)

_X_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/133.0.0.0 Safari/537.36"
)


class XAuthManager:
    """Launch a clean Chromium context and inject x_cookies.txt."""

    def __init__(
        self,
        user_data_dir: str | Path,
        headless: bool | None = None,
    ) -> None:
        self.user_data_dir = Path(user_data_dir)
        if headless is None:
            headless = os.getenv("HEADLESS", "true").strip().lower() != "false"
        self._headless = headless
        self._playwright = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None

    @property
    def context(self) -> BrowserContext | None:
        return self._context

    async def ensure_browser(self) -> BrowserContext:
        if self._context is not None:
            return self._context

        cookie_path = self.user_data_dir / "x_cookies.txt"
        cookies = load_netscape_cookies(cookie_path)
        if not cookies:
            logger.warning("No X cookies loaded from %s", cookie_path)

        logger.info("Launching clean X browser context (headless=%s)", self._headless)
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=self._headless,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-infobars",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--use-gl=swiftshader",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        self._context = await self._browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=_X_UA,
            locale="en-US",
            timezone_id="Europe/Madrid",
        )
        if cookies:
            await self._context.add_cookies(cookies)
            logger.info("Injected %d X cookies into clean context", len(cookies))
        return self._context

    async def close(self) -> None:
        if self._context is not None:
            await self._context.close()
            self._context = None
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None
        logger.info("X browser closed")
