"""Cookie loader for Playwright browser contexts.

Supports Netscape cookies.txt format (the standard export format from
browser extensions like "Get cookies.txt LOCALLY").
"""

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def load_netscape_cookies(path: str | Path) -> list[dict]:
    """Parse a Netscape-format cookies.txt file into Playwright cookie dicts.

    Playwright expects cookies as:
        {
            "name": str,
            "value": str,
            "domain": str,
            "path": str,
            "expires": float | None,
            "httpOnly": bool,
            "secure": bool,
            "sameSite": "Strict" | "Lax" | "None",
        }

    Netscape format (tab-separated):
        domain  httpOnly  path  secure  expires  name  value
    """
    cookies: list[dict] = []
    cookie_file = Path(path)

    if not cookie_file.exists():
        logger.warning("Cookie file not found: %s", cookie_file)
        return cookies

    with cookie_file.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) < 7:
                continue

            domain, http_only, path, secure, expires, name, value, *rest = parts
            try:
                expires_val = float(expires) if expires and expires != "0" else -1
            except ValueError:
                expires_val = -1

            cookies.append(
                {
                    "name": name,
                    "value": value,
                    "domain": domain,
                    "path": path,
                    "expires": expires_val,
                    "httpOnly": http_only.upper() == "TRUE",
                    "secure": secure.upper() == "TRUE",
                    "sameSite": "None",
                }
            )

    logger.info("Loaded %d cookies from %s", len(cookies), cookie_file)
    return cookies
