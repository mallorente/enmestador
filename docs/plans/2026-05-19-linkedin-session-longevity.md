# LinkedIn Session Longevity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Maximizar la duración de las sesiones de LinkedIn reemplazando Playwright por Patchright (stealth a nivel binario), eliminando la inyección de cookies sobre perfil persistente, y añadiendo comportamiento más humano.

**Architecture:** Patchright parchea el binario de Chromium para eliminar todos los indicadores de automatización que LinkedIn detecta (navigator.webdriver, HeadlessChrome UA string, Canvas fingerprint, CDPSession leak). Combinado con un único perfil persistente donde el usuario hace login una sola vez — sin exportación/inyección de cookies — la sesión acumula el mismo estado que un browser real. Los delays aleatorios y slow_mo evitan patrones de request demasiado mecánicos.

**Tech Stack:** Python 3.11, patchright==1.50.0 (drop-in replacement de playwright), Chromium 132, Docker

---

## File Map

| Acción | Archivo | Qué cambia |
|--------|---------|------------|
| Modify | `requirements.txt` | `playwright` → `patchright` |
| Modify | `Dockerfile` | install command + comentario |
| Modify | `auth/manager.py` | import, UA, slow_mo, init script mejorado, eliminar `_inject_cookies` |
| Modify | `auth/cookie_refresher.py` | import + reescribir para login directo al user_data_dir principal |
| Modify | `extractors/playwright.py` | import |
| Modify | `main.py` | import |
| Modify | `scrapers/linkedin.py` | import + delays aleatorios en scroll |
| Modify | `scrapers/x.py` | import |
| Modify | `.env.example` | añadir `SLOW_MO` |
| Modify | `tests/test_scraper_linkedin.py` | sin cambios necesarios (usa mocks, no playwright directamente) |

---

## Task 1: Reemplazar playwright por patchright en dependencias y Dockerfile

**Files:**
- Modify: `requirements.txt`
- Modify: `Dockerfile`

- [ ] **Step 1: Verificar que los tests actuales pasan (baseline)**

```bash
cd /home/miguel/enmestador
python -m pytest tests/test_scraper_linkedin.py -v --tb=short
```
Expected: todos los tests pasan. Si alguno falla, anótalo — es un pre-existing issue.

- [ ] **Step 2: Actualizar requirements.txt**

Cambiar la línea `playwright==1.50.0` por `patchright==1.50.0`:

```
patchright==1.50.0
openai==1.61.0
pydantic==2.10.6
python-dotenv==1.0.1
trafilatura==2.0.0
httpx==0.28.1
pyyaml==6.0.2
```

- [ ] **Step 3: Actualizar Dockerfile**

El Dockerfile tiene dos referencias a playwright. Reemplazar ambas:

Antes:
```dockerfile
# Install Playwright Chromium system dependencies
...
# Install Playwright Chromium browser
RUN playwright install chromium
```

Después — solo el comentario y el comando de install cambian:
```dockerfile
# Install Playwright/Patchright Chromium system dependencies
...
# Install Patchright Chromium browser (drop-in replacement with stealth patches)
RUN patchright install chromium
```

- [ ] **Step 4: Instalar patchright localmente y el browser**

```bash
cd /home/miguel/enmestador
pip install patchright==1.50.0
python -m patchright install chromium
```

Expected output: descarga e instala Chromium. El path puede ser distinto al de playwright.

- [ ] **Step 5: Commit**

```bash
git add requirements.txt Dockerfile
git commit -m "deps: replace playwright with patchright for stealth browser automation"
```

---

## Task 2: Migrar todos los imports de playwright a patchright

**Files:**
- Modify: `auth/manager.py:12`
- Modify: `auth/cookie_refresher.py:23`
- Modify: `extractors/playwright.py:13`
- Modify: `main.py:19`
- Modify: `scrapers/linkedin.py:13`
- Modify: `scrapers/x.py` (buscar import)

- [ ] **Step 1: Escribir test que verifica que patchright es importable**

Añadir al final de `tests/test_scraper_linkedin.py`:

```python
class TestPatchrightImport:
    """Verify patchright is installed and importable."""

    def test_patchright_importable(self) -> None:
        import patchright  # noqa: F401
        from patchright.async_api import async_playwright, BrowserContext, Page, Response  # noqa: F401
```

- [ ] **Step 2: Ejecutar el test para ver que falla si playwright sigue instalado sin patchright**

```bash
python -m pytest tests/test_scraper_linkedin.py::TestPatchrightImport -v
```

Expected: PASS si patchright ya instalado en Task 1. Si falla, revisar que `pip install patchright==1.50.0` se ejecutó correctamente.

- [ ] **Step 3: Cambiar import en auth/manager.py**

Línea 12, cambiar:
```python
from playwright.async_api import BrowserContext, async_playwright
```
por:
```python
from patchright.async_api import BrowserContext, async_playwright
```

- [ ] **Step 4: Cambiar import en auth/cookie_refresher.py**

Línea 23, cambiar:
```python
from playwright.async_api import BrowserContext, async_playwright
```
por:
```python
from patchright.async_api import BrowserContext, async_playwright
```

- [ ] **Step 5: Cambiar import en extractors/playwright.py**

Líneas 13-14, cambiar:
```python
from playwright.async_api import BrowserContext, Page
```
por:
```python
from patchright.async_api import BrowserContext, Page
```

- [ ] **Step 6: Cambiar import en main.py**

Línea 19, cambiar:
```python
from playwright.async_api import BrowserContext
```
por:
```python
from patchright.async_api import BrowserContext
```

- [ ] **Step 7: Cambiar import en scrapers/linkedin.py**

Línea 13, cambiar:
```python
from playwright.async_api import Page, Response
```
por:
```python
from patchright.async_api import Page, Response
```

- [ ] **Step 8: Cambiar import en scrapers/x.py**

Buscar y cambiar el import de playwright:
```python
from patchright.async_api import Page, Response
```

- [ ] **Step 9: Verificar que todos los imports cambiaron**

```bash
grep -r "from playwright" /home/miguel/enmestador --include="*.py" | grep -v __pycache__
```

Expected: **sin resultados**. Si aparece alguno, corregirlo.

- [ ] **Step 10: Ejecutar suite de tests**

```bash
python -m pytest tests/test_scraper_linkedin.py -v --tb=short
```

Expected: mismos tests que en el baseline pasan.

- [ ] **Step 11: Commit**

```bash
git add auth/manager.py auth/cookie_refresher.py extractors/playwright.py main.py scrapers/linkedin.py scrapers/x.py tests/test_scraper_linkedin.py
git commit -m "feat: migrate all playwright imports to patchright"
```

---

## Task 3: Mejorar anti-detección en AuthManager (UA, init script, slow_mo)

El `add_init_script` actual solo parchea 4 propiedades via JS. Patchright ya hace la mayoría a nivel binario, pero un init script más completo elimina otras huellas. Además actualizamos el user agent y añadimos `slow_mo`.

**Files:**
- Modify: `auth/manager.py`
- Modify: `.env.example`

- [ ] **Step 1: Escribir test para los nuevos parámetros de lanzamiento**

Añadir a `tests/test_scraper_linkedin.py`:

```python
class TestAuthManagerConfig:
    """Verify AuthManager reads SLOW_MO env var."""

    def test_slow_mo_defaults_to_zero(self, monkeypatch) -> None:
        import os
        monkeypatch.delenv("SLOW_MO", raising=False)
        from auth.manager import AuthManager
        mgr = AuthManager(user_data_dir="/tmp/test_auth")
        assert mgr._slow_mo == 0

    def test_slow_mo_reads_env_var(self, monkeypatch) -> None:
        monkeypatch.setenv("SLOW_MO", "50")
        # Re-import to pick up env var at construction time
        import importlib
        import auth.manager as am
        importlib.reload(am)
        mgr = am.AuthManager(user_data_dir="/tmp/test_auth")
        assert mgr._slow_mo == 50
```

- [ ] **Step 2: Ejecutar el test para ver que falla**

```bash
python -m pytest tests/test_scraper_linkedin.py::TestAuthManagerConfig -v
```

Expected: FAIL — `AuthManager` no tiene `_slow_mo`.

- [ ] **Step 3: Actualizar auth/manager.py**

Reemplazar el método `__init__` y `ensure_browser` completos:

```python
import os
import random
from pathlib import Path

from patchright.async_api import BrowserContext, async_playwright

logger = logging.getLogger(__name__)

# Chrome 132 matches the Chromium bundled with playwright/patchright 1.50
_CHROME_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/132.0.0.0 Safari/537.36"
)

# Additional stealth properties injected into every page before any script runs.
# Patchright already patches the binary-level tells; this covers JS-accessible APIs
# that sites probe to fingerprint automation.
_STEALTH_INIT_SCRIPT = """
(() => {
    // Already handled by patchright at binary level, but belt-and-suspenders:
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

    // Realistic plugin list (real Chrome has many plugins)
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

    // Languages consistent with Spanish user
    Object.defineProperty(navigator, 'languages', { get: () => ['es-ES', 'es', 'en-US', 'en'] });

    // Realistic platform
    Object.defineProperty(navigator, 'platform', { get: () => 'Linux x86_64' });

    // Chrome object — patchright sets this but reinforce it
    if (!window.chrome) {
        window.chrome = { runtime: {}, loadTimes: () => ({}), csi: () => ({}) };
    }

    // Permissions API — real Chrome resolves 'notifications' to 'prompt' by default
    const originalQuery = window.Notification && Notification.requestPermission;
    if (navigator.permissions && navigator.permissions.query) {
        const _origQuery = navigator.permissions.query.bind(navigator.permissions);
        navigator.permissions.query = (params) => {
            if (params.name === 'notifications') {
                return Promise.resolve({ state: 'prompt', onchange: null });
            }
            return _origQuery(params);
        };
    }

    // Hide automation-related properties in window
    delete window.cdc_adoQpoasnfa76pfcZLmcfl_Array;
    delete window.cdc_adoQpoasnfa76pfcZLmcfl_Promise;
    delete window.cdc_adoQpoasnfa76pfcZLmcfl_Symbol;
})();
"""


class AuthManager:
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
        return self._context

    async def ensure_browser(self) -> BrowserContext:
        if self._context is not None:
            return self._context

        self.user_data_dir.mkdir(parents=True, exist_ok=True)
        logger.info(
            "Launching browser (persistent profile: %s, headless=%s, slow_mo=%d)",
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

        # CSRF header for LinkedIn Voyager API — read from existing profile cookies
        await self._setup_linkedin_csrf()

        logger.info("Browser context ready")
        return self._context

    async def _setup_linkedin_csrf(self) -> None:
        """Inject csrf-token header for LinkedIn Voyager API calls.

        Reads JSESSIONID from the browser's current cookies (set during login
        or loaded from the persistent profile). No-op if not present.
        """
        try:
            all_cookies = await self._context.cookies()
            jsessionid = next(
                (c["value"].strip('"') for c in all_cookies if c["name"] == "JSESSIONID"),
                None,
            )
            if not jsessionid:
                logger.debug("JSESSIONID not found in profile cookies — LinkedIn CSRF header skipped")
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
        if self._context is not None:
            await self._context.close()
            self._context = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None
        logger.info("Browser closed")
```

Note: este bloque reemplaza todo el contenido de `auth/manager.py` después de las primeras líneas de docstring y logging import. Asegúrate de mantener el docstring del módulo existente.

- [ ] **Step 4: Añadir SLOW_MO a .env.example**

Añadir al bloque "Pipeline Settings":
```
SLOW_MO=0
```

- [ ] **Step 5: Ejecutar los tests**

```bash
python -m pytest tests/test_scraper_linkedin.py::TestAuthManagerConfig -v
```

Expected: ambos tests PASS.

- [ ] **Step 6: Ejecutar suite completa**

```bash
python -m pytest tests/test_scraper_linkedin.py -v --tb=short
```

Expected: todos pasan.

- [ ] **Step 7: Commit**

```bash
git add auth/manager.py .env.example tests/test_scraper_linkedin.py
git commit -m "feat: enhance anti-detection — patchright stealth init script, SLOW_MO, updated UA, locale/timezone"
```

---

## Task 4: Reescribir cookie_refresher.py — login directo al perfil principal

El flujo actual exporta cookies a .txt e inyecta en un contexto separado. Este flujo crea fingerprint mismatches. El nuevo flujo: el usuario hace login directamente en el perfil principal (`user_data_dir`), y el pipeline reutiliza ese mismo perfil sin ninguna inyección adicional.

**Files:**
- Modify: `auth/cookie_refresher.py`

- [ ] **Step 1: Escribir tests para el nuevo comportamiento**

Añadir a `tests/test_scraper_linkedin.py`:

```python
class TestCookieRefresherImports:
    """Verify cookie_refresher uses patchright, not playwright."""

    def test_no_playwright_in_refresher(self) -> None:
        import ast
        import pathlib
        src = pathlib.Path("auth/cookie_refresher.py").read_text()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                module = getattr(node, 'module', '') or ''
                assert 'playwright' not in module, (
                    f"cookie_refresher.py still imports from playwright: {module}"
                )
```

- [ ] **Step 2: Verificar que el test falla antes del cambio**

```bash
python -m pytest tests/test_scraper_linkedin.py::TestCookieRefresherImports -v
```

Expected: FAIL — todavía importa de playwright.

- [ ] **Step 3: Reescribir auth/cookie_refresher.py**

Reemplazar el contenido completo del archivo:

```python
"""Interactive login refresher for X.com and LinkedIn using Patchright.

Abre un navegador visible para que hagas login. La sesión se guarda
directamente en user_data_dir (el mismo directorio que usa el pipeline),
así que no hay exportación ni inyección de cookies: el pipeline reutiliza
el mismo perfil persistente.

Uso:
    python auth/cookie_refresher.py                     # ambas plataformas
    python auth/cookie_refresher.py --platform linkedin
    python auth/cookie_refresher.py --platform x

Ejecutar cada vez que el pipeline reporte auth expirada.
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
    """Block until the page navigates to a URL that looks post-login."""
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
            print(f"[{platform}] Login detectado. Cerrando y guardando sesión.")
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

        # Check if already authenticated
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
```

- [ ] **Step 4: Ejecutar el test**

```bash
python -m pytest tests/test_scraper_linkedin.py::TestCookieRefresherImports -v
```

Expected: PASS.

- [ ] **Step 5: Ejecutar suite completa**

```bash
python -m pytest tests/test_scraper_linkedin.py -v --tb=short
```

Expected: todos pasan.

- [ ] **Step 6: Commit**

```bash
git add auth/cookie_refresher.py tests/test_scraper_linkedin.py
git commit -m "feat: rewrite cookie_refresher to login directly to main persistent profile (no cookie export/injection)"
```

---

## Task 5: Eliminar inyección de cookies del pipeline principal

El `AuthManager.ensure_browser` ya no necesita `_inject_cookies` porque el perfil persistente contiene la sesión completa. Eliminar el método y sus llamadas.

**Files:**
- Modify: `auth/manager.py`

- [ ] **Step 1: Escribir test que verifica que AuthManager no tiene _inject_cookies**

Añadir a `tests/test_scraper_linkedin.py`:

```python
class TestAuthManagerNoCookieInjection:
    """AuthManager must NOT inject cookies from .txt files."""

    def test_no_inject_cookies_method(self) -> None:
        from auth.manager import AuthManager
        assert not hasattr(AuthManager, '_inject_cookies'), (
            "_inject_cookies should be removed — use persistent profile only"
        )

    def test_no_cookie_txt_references(self) -> None:
        import pathlib
        src = pathlib.Path("auth/manager.py").read_text()
        assert "x_cookies.txt" not in src
        assert "li_cookies.txt" not in src
        assert "_inject_cookies" not in src
```

- [ ] **Step 2: Ejecutar el test para ver que falla (antes del cambio)**

```bash
python -m pytest tests/test_scraper_linkedin.py::TestAuthManagerNoCookieInjection -v
```

Expected: FAIL — el método todavía existe.

- [ ] **Step 3: Limpiar auth/manager.py**

En el archivo `auth/manager.py` ya reescrito en Task 3, verificar que `_inject_cookies`, `x_cookies.txt`, `li_cookies.txt` ya no aparecen (la nueva versión de Task 3 ya no los incluye). Si por alguna razón quedaron referencias, eliminarlas ahora.

```bash
grep -n "_inject_cookies\|x_cookies\|li_cookies" /home/miguel/enmestador/auth/manager.py
```

Expected: sin output. Si hay resultados, editar el archivo para eliminarlos.

- [ ] **Step 4: Ejecutar el test**

```bash
python -m pytest tests/test_scraper_linkedin.py::TestAuthManagerNoCookieInjection -v
```

Expected: PASS.

- [ ] **Step 5: Ejecutar suite completa**

```bash
python -m pytest tests/ -v --tb=short -x
```

Expected: todos pasan.

- [ ] **Step 6: Commit**

```bash
git add auth/manager.py tests/test_scraper_linkedin.py
git commit -m "feat: remove cookie injection from pipeline — rely solely on persistent browser profile"
```

---

## Task 6: Añadir delays humanos en el scraper de LinkedIn

El scraper hace scroll a velocidad constante con intervalos fijos. LinkedIn puede detectar ese patrón. Añadir variación aleatoria en los tiempos de scroll y en los desplazamientos.

**Files:**
- Modify: `scrapers/linkedin.py`

- [ ] **Step 1: Escribir tests para los parámetros de scroll humanizado**

Añadir a `tests/test_scraper_linkedin.py`:

```python
class TestHumanizedScroll:
    """LinkedIn scraper uses variable scroll amounts and delays."""

    def test_human_scroll_params_importable(self) -> None:
        from scrapers.linkedin import _human_scroll_ms, _human_scroll_px
        # Both should return positive integers within expected ranges
        for _ in range(20):
            assert 1500 <= _human_scroll_ms() <= 4000
            assert 300 <= _human_scroll_px() <= 900
```

- [ ] **Step 2: Ejecutar el test para ver que falla**

```bash
python -m pytest tests/test_scraper_linkedin.py::TestHumanizedScroll -v
```

Expected: FAIL — las funciones no existen.

- [ ] **Step 3: Añadir funciones de scroll humanizado en scrapers/linkedin.py**

Añadir después de los imports (antes de `LINKEDIN_API_PATTERNS`):

```python
import random


def _human_scroll_ms() -> int:
    """Return a random scroll wait time in milliseconds (1500–4000ms)."""
    return random.randint(1500, 4000)


def _human_scroll_px() -> int:
    """Return a random vertical scroll amount in pixels (300–900px)."""
    return random.randint(300, 900)
```

- [ ] **Step 4: Usar las funciones en el bucle de paginación**

En el método `scrape` de `ScraperLinkedIn`, localizar el bucle de paginación (alrededor de la línea 500) y reemplazar los valores fijos:

Antes:
```python
scroll_interval = 2000
...
while pagination_elapsed < pagination_timeout and len(self._api_posts) < self.max_posts:
    await self.page.evaluate("window.scrollBy(0, window.innerHeight)")
    await self.page.wait_for_timeout(scroll_interval)
    pagination_elapsed += scroll_interval
```

Después:
```python
while pagination_elapsed < pagination_timeout and len(self._api_posts) < self.max_posts:
    scroll_px = _human_scroll_px()
    scroll_wait = _human_scroll_ms()
    await self.page.evaluate(f"window.scrollBy(0, {scroll_px})")
    await self.page.wait_for_timeout(scroll_wait)
    pagination_elapsed += scroll_wait
```

También reemplazar el `scroll_interval` de la fase inicial (check_interval) por algo más variable:

Antes (alrededor de línea 468):
```python
check_interval = 2000
```

Después:
```python
check_interval = 2500  # initial wait is less critical, keep simple
```

- [ ] **Step 5: Ejecutar el test**

```bash
python -m pytest tests/test_scraper_linkedin.py::TestHumanizedScroll -v
```

Expected: PASS.

- [ ] **Step 6: Ejecutar suite completa**

```bash
python -m pytest tests/test_scraper_linkedin.py -v --tb=short
```

Expected: todos pasan.

- [ ] **Step 7: Commit**

```bash
git add scrapers/linkedin.py tests/test_scraper_linkedin.py
git commit -m "feat: randomize LinkedIn scroll delays and amounts for human-like behavior"
```

---

## Task 7: Verificación final e integración

- [ ] **Step 1: Ejecutar suite de tests completa**

```bash
python -m pytest tests/ -v --tb=short
```

Expected: todos los tests existentes pasan. Anotar cualquier fallo nuevo (no pre-existing).

- [ ] **Step 2: Verificar que no quedan referencias a playwright**

```bash
grep -r "from playwright\|import playwright" /home/miguel/enmestador --include="*.py" | grep -v __pycache__
```

Expected: sin resultados.

- [ ] **Step 3: Verificar que patchright puede lanzar un browser headless**

```bash
python -c "
import asyncio
from patchright.async_api import async_playwright

async def check():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto('https://example.com')
        title = await page.title()
        await browser.close()
        print(f'OK: {title}')

asyncio.run(check())
"
```

Expected: `OK: Example Domain` (u similar).

- [ ] **Step 4: Commit final si hay cambios sin commitear**

```bash
git status
# Si hay algo: git add . && git commit -m "chore: final cleanup after patchright migration"
```

---

## Resumen de cambios

| # | Cambio | Impacto esperado |
|---|--------|-----------------|
| 1 | Patchright reemplaza Playwright | Elimina automation fingerprints a nivel binario — mayor impacto |
| 2 | User agent Chrome 132 + locale/timezone Madrid | Fingerprint más coherente |
| 3 | Init script completo (plugins, permissions, cdc_* props) | Elimina huellas JS adicionales |
| 4 | SLOW_MO configurable via env | Permite simular latencia humana entre acciones |
| 5 | Login directo al perfil principal (cookie_refresher reescrito) | Elimina fingerprint mismatch por importación de cookies |
| 6 | Sin inyección de cookies en el pipeline | Sesión coherente con el perfil del navegador |
| 7 | Scroll aleatorio (300–900px, 1500–4000ms) | Evita patrones mecánicos de scroll |
