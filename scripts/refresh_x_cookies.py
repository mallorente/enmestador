"""Interactive X login that exports fresh cookies to x_cookies.txt.

This avoids the shared persistent Chromium profile used for LinkedIn. X is
opened in a dedicated profile, then the authenticated cookies are written in
Netscape format for XAuthManager.
"""

import argparse
import asyncio
import logging
import time
from pathlib import Path

from dotenv import load_dotenv
from patchright.async_api import async_playwright

from auth.credentials import CredentialError, get_credentials
from auth.cookie_refresher import _attempt_auto_login

load_dotenv()

logger = logging.getLogger(__name__)

X_BOOKMARKS_URL = "https://x.com/i/bookmarks"
X_LOGIN_URL = "https://x.com/i/flow/login"
COOKIE_DOMAINS = ("x.com", ".x.com", "twitter.com", ".twitter.com")
REQUIRED_COOKIES = {"auth_token", "ct0"}

_X_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/133.0.0.0 Safari/537.36"
)


def _netscape_bool(value: bool) -> str:
    return "TRUE" if value else "FALSE"


def _write_netscape_cookies(path: Path, cookies: list[dict]) -> None:
    lines = ["# Netscape HTTP Cookie File"]
    for cookie in cookies:
        domain = cookie.get("domain", "")
        if not any(domain.endswith(d.strip(".")) for d in COOKIE_DOMAINS):
            continue
        include_subdomains = domain.startswith(".")
        secure = bool(cookie.get("secure"))
        expires = int(cookie.get("expires") or 0)
        if expires < 0:
            expires = 0
        lines.append(
            "\t".join(
                [
                    domain,
                    _netscape_bool(include_subdomains),
                    cookie.get("path", "/"),
                    _netscape_bool(secure),
                    str(expires),
                    cookie.get("name", ""),
                    cookie.get("value", ""),
                ]
            )
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


async def _has_required_cookies(context) -> bool:
    cookies = await context.cookies()
    names = {cookie.get("name") for cookie in cookies}
    return REQUIRED_COOKIES.issubset(names)


async def _page_has_content(page) -> bool:
    try:
        text_len = await page.evaluate("() => document.body ? document.body.innerText.trim().length : 0")
    except Exception:
        return False
    return text_len > 50


async def refresh_x_cookies(
    profile_dir: Path,
    output_file: Path,
    timeout: int,
    auto_login: bool = False,
) -> None:
    profile_dir.mkdir(parents=True, exist_ok=True)
    for lock in profile_dir.glob("Singleton*"):
        try:
            lock.unlink()
        except OSError:
            logger.warning("Could not remove Chromium profile lock: %s", lock)

    async with async_playwright() as pw:
        context = await pw.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=False,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-infobars",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-blink-features=AutomationControlled",
            ],
            viewport={"width": 1280, "height": 900},
            user_agent=_X_UA,
            locale="en-US",
            timezone_id="Europe/Madrid",
        )
        try:
            page = await context.new_page()
            await page.goto(X_BOOKMARKS_URL, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(3000)
            if "login" in page.url or not await _has_required_cookies(context):
                await page.goto(X_LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
                if auto_login:
                    try:
                        credentials = get_credentials("x")
                        print("[X] Rellenando login con credenciales de Bitwarden.")
                        await _attempt_auto_login(page, "x", credentials)
                    except CredentialError as exc:
                        print(f"[X] Auto-login no disponible: {exc}")
                    except Exception as exc:
                        logger.warning("Auto-login failed for X: %s", exc)

            print("\n[X] Login en la ventana noVNC. Si ves pantalla negra, pulsa Ctrl+R dentro del navegador.")
            print("[X] Espero hasta detectar auth_token + ct0 y contenido real de X.")

            deadline = time.time() + timeout
            last_status = ""
            while time.time() < deadline:
                cookies_ok = await _has_required_cookies(context)
                content_ok = await _page_has_content(page)
                status = f"url={page.url} cookies_ok={cookies_ok} content_ok={content_ok}"
                if status != last_status:
                    print(f"[X] {status}")
                    last_status = status
                if cookies_ok and ("x.com" in page.url or "twitter.com" in page.url) and content_ok:
                    cookies = await context.cookies()
                    _write_netscape_cookies(output_file, cookies)
                    print(f"[X] Cookies exportadas a {output_file}")
                    return
                await asyncio.sleep(2)

            raise TimeoutError("[X] Timeout: no se detectó sesión usable.")
        finally:
            await context.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Refresh X cookies.txt via interactive login")
    parser.add_argument("--profile-dir", type=Path, default=Path("volumes/user_data/x_profile"))
    parser.add_argument("--output-file", type=Path, default=Path("volumes/user_data/x_cookies.txt"))
    parser.add_argument("--timeout", type=int, default=3600)
    parser.add_argument("--auto-login", action="store_true", help="Rellena credenciales desde Bitwarden si X pide login.")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    asyncio.run(refresh_x_cookies(args.profile_dir, args.output_file, args.timeout, auto_login=args.auto_login))


if __name__ == "__main__":
    main()
