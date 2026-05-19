"""Interactive login refresher for X.com and LinkedIn using Patchright.

Opens a visible browser directly on the main user_data_dir so the session
is saved to the same persistent profile the pipeline uses. No cookie
export or injection needed — the profile IS the session.

Usage:
    python auth/cookie_refresher.py                      # both platforms
    python auth/cookie_refresher.py --platform linkedin
    python auth/cookie_refresher.py --platform x

Run whenever the pipeline reports authentication expired.
"""

import argparse
import asyncio
import logging
import os
import time
from pathlib import Path

from patchright.async_api import async_playwright

logger = logging.getLogger(__name__)

USER_DATA_DIR = Path(os.getenv("USER_DATA_DIR", "./volumes/user_data"))

X_BOOKMARKS_URL = "https://x.com/i/bookmarks"
X_LOGIN_URL = "https://x.com/login"

LI_SAVED_URL = "https://www.linkedin.com/my-items/saved-posts/"
LI_LOGIN_URL = "https://www.linkedin.com/login"

_CHROME_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/132.0.0.0 Safari/537.36"
)


async def _wait_for_manual_login(page, success_url_fragment: str, platform: str) -> None:
    """Block until the page navigates to a post-login URL."""
    print(f"\n[{platform}] Completa el login en la ventana del navegador.")
    print(f"[{platform}] Esperando...")
    deadline = time.time() + 300
    while time.time() < deadline:
        url = page.url
        if (
            success_url_fragment in url
            and "login" not in url
            and "authwall" not in url
            and "checkpoint" not in url
        ):
            print(f"[{platform}] Login detectado. Guardando sesión.")
            return
        await asyncio.sleep(2)
    raise TimeoutError(f"[{platform}] Timeout: login no detectado en 5 minutos.")


async def refresh_platform(platform: str, user_data_dir: Path) -> None:
    """Launch a visible browser on user_data_dir and wait for manual login."""
    if platform == "x":
        check_url = X_BOOKMARKS_URL
        success_fragment = "x.com"
        login_url = X_LOGIN_URL
    else:
        check_url = LI_SAVED_URL
        success_fragment = "linkedin.com"
        login_url = LI_LOGIN_URL

    launch_args = [
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-blink-features=AutomationControlled",
    ]

    pw = await async_playwright().start()
    try:
        context = await pw.chromium.launch_persistent_context(
            user_data_dir=str(user_data_dir),
            headless=False,
            args=launch_args,
            viewport={"width": 1280, "height": 900},
            user_agent=_CHROME_UA,
            locale="es-ES",
            timezone_id="Europe/Madrid",
        )
        page = await context.new_page()

        await page.goto(check_url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(3000)
        url = page.url

        already_logged_in = (
            success_fragment in url
            and "login" not in url
            and "authwall" not in url
        )

        if already_logged_in:
            print(f"[{platform.upper()}] Ya autenticado. Sesión vigente en {user_data_dir}")
        else:
            print(f"[{platform.upper()}] Redirigiendo a login...")
            await page.goto(login_url, wait_until="domcontentloaded", timeout=30000)
            await _wait_for_manual_login(page, success_fragment, platform.upper())

        await context.close()
        print(f"[{platform.upper()}] Sesión guardada en {user_data_dir}. El pipeline la reutilizará.")
    finally:
        await pw.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description="Login interactivo para X y/o LinkedIn")
    parser.add_argument(
        "--platform",
        choices=["x", "linkedin", "both"],
        default="both",
    )
    parser.add_argument(
        "--user-data-dir",
        type=Path,
        default=USER_DATA_DIR,
        help=f"Directorio de perfil persistente (default: {USER_DATA_DIR})",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    args.user_data_dir.mkdir(parents=True, exist_ok=True)

    platforms = ["x", "linkedin"] if args.platform == "both" else [args.platform]
    for p in platforms:
        asyncio.run(refresh_platform(p, args.user_data_dir))

    print("\nListo. Ejecuta el pipeline: python main.py")


if __name__ == "__main__":
    main()
