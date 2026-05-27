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

from dotenv import load_dotenv
from patchright.async_api import async_playwright

from auth.credentials import CredentialError, Credentials, get_credentials

load_dotenv()

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


async def _wait_for_manual_login(page, success_url_fragment: str, platform: str, timeout: int = 300) -> None:
    """Block until the page navigates to a post-login URL."""
    print(f"\n[{platform}] Completa el login en la ventana del navegador.")
    print(f"[{platform}] Esperando...")
    deadline = time.time() + timeout
    while time.time() < deadline:
        url = page.url
        if (
            success_url_fragment in url
            and "login" not in url
            and "authwall" not in url
            and "checkpoint" not in url
            and await _page_has_real_session(page, platform.lower())
        ):
            print(f"[{platform}] Login detectado. Guardando sesión.")
            return
        await asyncio.sleep(2)
    raise TimeoutError(f"[{platform}] Timeout: login no detectado en 5 minutos.")


async def _attempt_auto_login(page, platform: str, credentials: Credentials) -> None:
    """Fill the login form with password-manager credentials.

    MFA, captcha, and checkpoint screens are intentionally not bypassed. After
    this method submits credentials, the normal login wait handles success or
    leaves the browser available for manual completion.
    """
    if platform == "linkedin":
        await _fill_first(page, [
            "input#username",
            "input[name='session_key']",
            "input[type='email']",
            "input[type='text']",
        ], credentials.username)
        await _fill_first(page, [
            "input#password",
            "input[name='session_password']",
            "input[type='password']",
        ], credentials.password)
        await _click_first(page, [
            "button[type='submit']",
            "button:has-text('Sign in')",
            "button:has-text('Iniciar sesión')",
        ])
        return

    if platform == "x":
        await _fill_first(page, [
            "input[autocomplete='username']",
            "input[name='text']",
            "input[type='email']",
            "input[type='text']",
        ], credentials.username)
        await _click_first(page, [
            "button:has-text('Next')",
            "button:has-text('Siguiente')",
            "div[role='button']:has-text('Next')",
            "div[role='button']:has-text('Siguiente')",
        ])
        await page.wait_for_timeout(1500)

        # X sometimes asks for username/phone again before showing password.
        try:
            await _fill_first(page, [
                "input[data-testid='ocfEnterTextTextInput']",
            ], credentials.username, required=False)
            await _click_first(page, [
                "button:has-text('Next')",
                "button:has-text('Siguiente')",
                "div[role='button']:has-text('Next')",
                "div[role='button']:has-text('Siguiente')",
            ], required=False)
            await page.wait_for_timeout(1000)
        except Exception:
            logger.debug("X did not show an intermediate username challenge")

        await _fill_first(page, [
            "input[name='password']",
            "input[type='password']",
            "input[autocomplete='current-password']",
        ], credentials.password)
        await _click_first(page, [
            "button:has-text('Log in')",
            "button:has-text('Iniciar sesión')",
            "div[role='button']:has-text('Log in')",
            "div[role='button']:has-text('Iniciar sesión')",
        ])
        return

    raise ValueError(f"Unsupported platform for auto-login: {platform}")


async def _fill_first(page, selectors: list[str], value: str, *, required: bool = True) -> bool:
    for selector in selectors:
        try:
            await page.fill(selector, value, timeout=2500)
            return True
        except Exception:
            continue
    if required:
        raise RuntimeError("Could not find expected login input")
    return False


async def _click_first(page, selectors: list[str], *, required: bool = True) -> bool:
    for selector in selectors:
        try:
            await page.click(selector, timeout=2500)
            return True
        except Exception:
            continue
    if required:
        raise RuntimeError("Could not find expected login button")
    return False


async def _page_has_real_session(page, platform: str) -> bool:
    """Return True when the loaded page shows authenticated content."""
    url = page.url
    if (
        "login" in url
        or "authwall" in url
        or "checkpoint" in url
        or "/uas/" in url
    ):
        return False

    try:
        body_text = await page.evaluate("() => document.body ? document.body.innerText : ''")
    except Exception:
        body_text = ""
    normalized = body_text.lower()

    if platform == "x":
        # X can return 200 and stay forever on the black logo loader when the
        # session/API bootstrap is rate-limited or challenged. URL alone is not
        # enough to consider the profile healthy.
        return "x.com" in url and len(body_text.strip()) > 50

    linkedin_login_markers = (
        "iniciar sesión",
        "email o teléfono",
        "contraseña",
        "continue with google",
        "sign in",
        "join now",
        "unirse ahora",
    )
    if any(marker in normalized for marker in linkedin_login_markers):
        return False

    linkedin_session_markers = (
        "saved posts",
        "saved posts and articles",
        "my items",
        "messaging",
        "notifications",
        "mi red",
        "empleos",
        "mensajes",
        "notificaciones",
        "para negocios",
    )
    return "linkedin.com" in url and any(marker in normalized for marker in linkedin_session_markers)


async def refresh_platform(
    platform: str,
    user_data_dir: Path,
    timeout: int = 300,
    auto_login: bool = False,
) -> None:
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
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--use-gl=swiftshader",
        "--disable-blink-features=AutomationControlled",
    ]

    user_data_dir.mkdir(parents=True, exist_ok=True)
    for lock in user_data_dir.glob("Singleton*"):
        try:
            lock.unlink()
        except OSError:
            logger.warning("Could not remove Chromium profile lock: %s", lock)

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

        already_logged_in = await _page_has_real_session(page, platform)

        if already_logged_in:
            print(f"[{platform.upper()}] Ya autenticado. Sesión vigente en {user_data_dir}")
        else:
            print(f"[{platform.upper()}] La sesión no está usable; redirigiendo a login/challenge...")
            await page.goto(login_url, wait_until="domcontentloaded", timeout=30000)
            if auto_login:
                try:
                    credentials = get_credentials(platform)
                    print(f"[{platform.upper()}] Rellenando login con credenciales del password manager.")
                    await _attempt_auto_login(page, platform, credentials)
                except CredentialError as exc:
                    print(f"[{platform.upper()}] Auto-login no disponible: {exc}")
                    print(f"[{platform.upper()}] Continúa manualmente en el navegador.")
                except Exception as exc:
                    logger.warning("Auto-login failed for %s: %s", platform, exc)
                    print(f"[{platform.upper()}] Auto-login falló. Continúa manualmente en el navegador.")
            await _wait_for_manual_login(page, success_fragment, platform.upper(), timeout=timeout)

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
    parser.add_argument(
        "--timeout",
        type=int,
        default=300,
        help="Segundos para esperar el login manual (default: 300)",
    )
    parser.add_argument(
        "--auto-login",
        action="store_true",
        help="Rellena usuario/password desde Bitwarden/comandos si la sesión caducó.",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    args.user_data_dir.mkdir(parents=True, exist_ok=True)

    platforms = ["x", "linkedin"] if args.platform == "both" else [args.platform]
    for p in platforms:
        asyncio.run(refresh_platform(
            p,
            args.user_data_dir,
            timeout=args.timeout,
            auto_login=args.auto_login,
        ))

    print("\nListo. Ejecuta el pipeline: python main.py")


if __name__ == "__main__":
    main()
