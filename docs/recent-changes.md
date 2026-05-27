# Recent Changes

Resumen de los cambios integrados desde el ultimo estado remoto anterior.

## Browser Automation

- Migracion de Playwright a Patchright.
- `AuthManager` reescrito para LinkedIn con perfil persistente, user agent
  estable, locale Madrid, `SLOW_MO` opcional y sin inyeccion de cookies.
- `XAuthManager` separado: X carga `volumes/user_data/x_cookies.txt` en un
  browser context limpio.

## Auth

- `auth/cookie_refresher.py` renueva sesiones de forma interactiva.
- `scripts/refresh_x_cookies.py` exporta cookies frescas de X.
- `auth/credentials.py` carga credenciales desde variables directas, comandos o
  items de Bitwarden.
- Auto-login con Bitwarden para LinkedIn y X, sin saltarse MFA/captcha.

## Scraping

- LinkedIn usa intercept JS `fetch`/XHR en lugar de CDP response events.
- LinkedIn tiene DOM fallback con scroll/delays mas humanos.
- X captura GraphQL, DOM fallback, imagenes, tweets citados/referenciados y
  evita falsos avisos de auth si `Bookmarks` responde autenticado.

## Pipeline

- CLI con `--source`, `--delta-only`, `--dry-run` y `--fresh-run`.
- `pipeline/frontier.py` corta el delta al alcanzar bookmarks conocidos.
- `pipeline/dedupe.py` deduplica notas del vault por URL canonica.
- Writer enriquecido con frontmatter, imagenes, links externos, tweets
  referenciados y JSON sidecars.

## Operations

- Scheduler Docker estabilizado a 6h por defecto con
  `SCHEDULER_INTERVAL_HOURS`.
- Servicio `syncthing` y `Dockerfile.syncthing`.
- Logs sensibles saneados: `httpx/httpcore` no imprimen requests a nivel INFO y
  los tokens de Telegram se redactan defensivamente.

## Tests

- Tests ampliados para credenciales, auth, frontier, dedupe, scrapers, writer y
  orquestacion.
