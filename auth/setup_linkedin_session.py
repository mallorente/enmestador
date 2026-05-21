"""One-time tool: import LinkedIn cookies into the persistent Chrome profile so the
pipeline can reuse the session without logging in again.

Accepted formats:
  - Netscape cookies.txt  (exported with "Get cookies.txt LOCALLY" or similar)
  - Cookie-Editor JSON    (.json, exported with "Cookie-Editor" extension)

Usage:
    python auth/setup_linkedin_session.py li_cookies.txt
    python auth/setup_linkedin_session.py li_cookies.json
    python auth/setup_linkedin_session.py li_cookies.txt --user-data-dir /custom/path

NOTE: li_at is IP-bound. This works best when run from the same IP you logged in from,
or when LinkedIn's session hasn't expired. If it fails, use cookie_refresher.py with VNC.
"""

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

from patchright.async_api import async_playwright
from cookie_loader import load_netscape_cookies

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

_CHROME_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/132.0.0.0 Safari/537.36"
)


def _load_cookie_editor_json(path: Path) -> list[dict]:
    """Parse Cookie-Editor JSON export into Playwright cookie dicts."""
    raw = json.loads(path.read_text())
    cookies = []
    for c in raw:
        domain = c.get("domain", "")
        # Ensure domain has leading dot for subdomains
        if domain and not domain.startswith(".") and domain != "linkedin.com":
            domain = f".{domain}"
        cookie = {
            "name": c["name"],
            "value": c["value"],
            "domain": domain or ".linkedin.com",
            "path": c.get("path", "/"),
            "secure": c.get("secure", True),
            "httpOnly": c.get("httpOnly", False),
            "sameSite": c.get("sameSite", "None"),
        }
        # expires: Cookie-Editor uses Unix timestamp or -1 for session cookies
        exp = c.get("expirationDate") or c.get("expires")
        if exp and float(exp) > 0:
            cookie["expires"] = float(exp)
        cookies.append(cookie)
    return cookies


def _load_cookies(path: Path) -> list[dict]:
    """Auto-detect format and load cookies."""
    if path.suffix.lower() == ".json":
        return _load_cookie_editor_json(path)
    return load_netscape_cookies(path)


async def _setup(cookies_file: Path, user_data_dir: Path) -> bool:
    logger.info("Loading cookies from %s", cookies_file)
    cookies = _load_cookies(cookies_file)
    li_at = next((c for c in cookies if c["name"] == "li_at"), None)
    if not li_at:
        logger.error("No li_at cookie found in %s — are these LinkedIn cookies?", cookies_file)
        return False
    logger.info("Found %d cookies (li_at present)", len(cookies))

    logger.info("Opening persistent profile at %s", user_data_dir)
    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            user_data_dir=str(user_data_dir),
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
            viewport={"width": 1280, "height": 800},
            user_agent=_CHROME_UA,
            locale="es-ES",
            timezone_id="Europe/Madrid",
        )

        await ctx.add_cookies(cookies)
        logger.info("Cookies written to profile")

        page = await ctx.new_page()
        logger.info("Verifying session on LinkedIn…")
        try:
            resp = await page.goto(
                "https://www.linkedin.com/my-items/saved-posts/",
                wait_until="domcontentloaded",
                timeout=20000,
            )
            status = resp.status if resp else "?"
            url = page.url
            logger.info("HTTP %s → %s", status, url)

            if "login" in url or "authwall" in url:
                logger.error("NOT logged in — session rejected (IP mismatch or expired cookie)")
                await ctx.close()
                return False

            logger.info("SUCCESS — session active, profile ready for pipeline use")
            await ctx.close()
            return True
        except Exception as exc:
            logger.error("Navigation failed: %s", exc)
            await ctx.close()
            return False


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("cookies_file", type=Path, help="Cookie-Editor JSON export file")
    parser.add_argument(
        "--user-data-dir",
        type=Path,
        default=Path(os.getenv("USER_DATA_DIR", "volumes/user_data")),
        help="Chrome persistent profile directory (default: volumes/user_data)",
    )
    args = parser.parse_args()

    if not args.cookies_file.exists():
        logger.error("File not found: %s", args.cookies_file)
        sys.exit(1)

    ok = asyncio.run(_setup(args.cookies_file, args.user_data_dir))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
