"""Auth manager for the PKM ingestion pipeline.

Launches a Patchright persistent browser context. Session is maintained
entirely via the persistent profile directory — no cookie injection needed.
Run auth/cookie_refresher.py whenever the session expires.
"""

import logging
import os
from pathlib import Path

from patchright.async_api import BrowserContext, async_playwright

logger = logging.getLogger(__name__)

# Chrome 132 matches the Chromium bundled with patchright/playwright 1.50
_CHROME_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/132.0.0.0 Safari/537.36"
)

# Stealth patches injected into every page before any script runs.
# Patchright already handles the binary-level tells; this covers JS-accessible
# APIs that sites probe to fingerprint automation.
_STEALTH_INIT_SCRIPT = """
(() => {
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

    Object.defineProperty(navigator, 'plugins', {
        get: () => {
            const arr = [
                { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
                { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: '' },
                { name: 'Native Client', filename: 'internal-nacl-plugin', description: '' },
            ];
            arr.refresh = () => {};
            arr.item = (i) => arr[i];
            arr.namedItem = (n) => arr.find(p => p.name === n) || null;
            Object.defineProperty(arr, 'length', { get: () => 3 });
            return arr;
        }
    });

    Object.defineProperty(navigator, 'languages', { get: () => ['es-ES', 'es', 'en-US', 'en'] });
    Object.defineProperty(navigator, 'platform', { get: () => 'Linux x86_64' });

    if (!window.chrome) {
        window.chrome = { runtime: {}, loadTimes: () => ({}), csi: () => ({}) };
    }

    if (navigator.permissions && navigator.permissions.query) {
        const _origQuery = navigator.permissions.query.bind(navigator.permissions);
        navigator.permissions.query = (params) => {
            if (params.name === 'notifications') {
                return Promise.resolve({ state: 'prompt', onchange: null });
            }
            return _origQuery(params);
        };
    }

    delete window.cdc_adoQpoasnfa76pfcZLmcfl_Array;
    delete window.cdc_adoQpoasnfa76pfcZLmcfl_Promise;
    delete window.cdc_adoQpoasnfa76pfcZLmcfl_Symbol;
})();
"""


class AuthManager:
    """Manages a Patchright persistent browser context.

    Session is maintained via the persistent profile directory (user_data_dir).
    No cookie file injection — run auth/cookie_refresher.py to renew the session.
    """

    def __init__(
        self,
        user_data_dir: str | Path,
        headless: bool | None = None,
    ) -> None:
        self.user_data_dir = Path(user_data_dir)
        if headless is None:
            headless = os.getenv("HEADLESS", "true").strip().lower() != "false"
        self._headless = headless
        self._slow_mo = int(os.getenv("SLOW_MO", "0"))
        self._playwright = None
        self._context: BrowserContext | None = None

    @property
    def context(self) -> BrowserContext | None:
        """Return the running BrowserContext, or None."""
        return self._context

    async def ensure_browser(self) -> BrowserContext:
        """Launch a persistent browser context with stealth configuration."""
        if self._context is not None:
            return self._context

        self.user_data_dir.mkdir(parents=True, exist_ok=True)
        logger.info(
            "Launching browser (profile: %s, headless=%s, slow_mo=%d)",
            self.user_data_dir, self._headless, self._slow_mo,
        )

        self._playwright = await async_playwright().start()

        launch_args = [
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-infobars",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
            "--disable-features=IsolateOrigins,site-per-process",
        ]
        if not self._headless:
            launch_args.append("--window-position=0,0")

        self._context = await self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(self.user_data_dir),
            headless=self._headless,
            slow_mo=self._slow_mo,
            args=launch_args,
            viewport={"width": 1280, "height": 800},
            user_agent=_CHROME_UA,
            locale="es-ES",
            timezone_id="Europe/Madrid",
        )

        await self._context.add_init_script(_STEALTH_INIT_SCRIPT)
        await self._setup_linkedin_csrf()

        logger.info("Browser context ready")
        return self._context

    async def _setup_linkedin_csrf(self) -> None:
        """Inject csrf-token header for LinkedIn Voyager API calls.

        Reads JSESSIONID from the persistent profile's cookies.
        No-op if not present (session not yet established).
        """
        try:
            all_cookies = await self._context.cookies()
            jsessionid = next(
                (c["value"].strip('"') for c in all_cookies if c["name"] == "JSESSIONID"),
                None,
            )
            if not jsessionid:
                logger.debug("JSESSIONID not in profile — LinkedIn CSRF header skipped")
                return

            csrf_token = jsessionid
            logger.info("Setting LinkedIn CSRF token from profile JSESSIONID")

            async def _add_linkedin_headers(route):
                headers = dict(route.request.headers)
                headers["csrf-token"] = csrf_token
                headers["x-restli-protocol-version"] = "2.0.0"
                await route.continue_(headers=headers)

            await self._context.route("**/voyager/**", _add_linkedin_headers)
        except Exception as exc:
            logger.warning("Could not set LinkedIn CSRF header: %s", exc)

    async def close(self) -> None:
        """Close the persistent browser context and playwright instance."""
        if self._context is not None:
            await self._context.close()
            self._context = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None
        logger.info("Browser closed")
