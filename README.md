# enmestador

> *Del verbo "enmestar" — recoger la mies, traer el grano a casa.*

Pipeline PKM personal que scrapa bookmarks de X.com y posts guardados de
LinkedIn, los enriquece con un LLM, y escribe notas Markdown listas para
Obsidian y para un vault de LLM Wiki.

---

**Sobre el nombre.** *Enmestar* es un término del habla de la Huerta de Murcia
(el panocho, dialecto de la región de Murcia, España), derivado de *mies* (del
latín *messis*: la cosecha de cereal madura, lista para segar). El prefijo
verbalizador *en-* convierte el sustantivo en acción: ir al campo, recoger lo
que está listo, traerlo a casa. No está en el diccionario de la RAE, pero sigue
vivo en el vocabulario agrícola murciano.

---

```text
X bookmarks ──┐
              ├─► Patchright scraper ─► LLM enrichment ─► LLM Wiki / Obsidian
LinkedIn ─────┘
```

## Qué Hace

- **Scraping sin API oficial** de X bookmarks y posts guardados de LinkedIn con
  Patchright e intercepción GraphQL/API.
- **Extracción enriquecida** de hilos completos de X, posts de LinkedIn,
  imágenes, tweets referenciados y artículos externos.
- **Enriquecimiento LLM** con resumen en 3 bullets, takeaway y etiquetas.
- **Fallbacks de modelo** para proveedores OpenAI-compatible como OpenCode,
  OpenRouter o NVIDIA.
- **Notas Markdown Obsidian-ready** con frontmatter YAML, JSON sidecar y
  organización por fuente.
- **Delta scraping** con cursores, frontera de bookmarks conocidos y dedupe por
  URL contra estado y contra el vault.
- **Auth operativa** separada por plataforma: X usa cookies exportadas,
  LinkedIn usa perfil persistente.
- **Scheduler Docker** cada 6h por defecto, con notificaciones Telegram.
- **Syncthing** para sincronizar el vault `llm_wiki_seed` con otros dispositivos.

## Estado Actual

El flujo de producción escribe en:

```text
volumes/llm_wiki_seed/Bookmarks/bookmarks
```

Dentro se generan notas por fuente:

```text
volumes/llm_wiki_seed/Bookmarks/bookmarks/x
volumes/llm_wiki_seed/Bookmarks/bookmarks/linkedin
```

Cada bookmark produce:

- un `.md` con frontmatter y contenido legible por Obsidian/agentes;
- un `.json` sidecar con el objeto completo serializado;
- URLs normalizadas para dedupe y frontera delta.

Los servicios esperados en servidor son:

```bash
docker compose up -d scheduler syncthing
```

## Cambios Recientes Desde El Último Push Anterior

- Migración de Playwright a **Patchright** para reducir fricción de scraping.
- Reescritura de `AuthManager` para LinkedIn con perfil persistente, user agent
  estable, locale Madrid, `SLOW_MO` opcional y sin inyección de cookies.
- Separación de X en `XAuthManager`: X ya no comparte el perfil persistente de
  LinkedIn; carga `volumes/user_data/x_cookies.txt` en un contexto limpio.
- Nuevo refresher de sesiones:
  - `auth/cookie_refresher.py` para login interactivo de LinkedIn/X;
  - `scripts/refresh_x_cookies.py` para exportar cookies frescas de X.
- Soporte de credenciales externas con `auth/credentials.py`: variables directas,
  comandos `*_USERNAME_CMD` / `*_PASSWORD_CMD`, o items de Bitwarden.
- LinkedIn cambió de CDP response events a intercept JS `fetch`/XHR y tiene DOM
  fallback con delays/scrolls más humanos.
- X captura GraphQL, DOM fallback, imágenes, tweets citados/referenciados y
  evita falsos avisos de auth si el endpoint `Bookmarks` responde autenticado.
- Añadidos `--source`, `--delta-only`, `--dry-run` y `--fresh-run` para runs
  controlados.
- Añadido `pipeline/frontier.py`: el scraper corta al alcanzar una secuencia de
  bookmarks conocidos, evitando repasar todo el histórico.
- Añadido `pipeline/dedupe.py`: dedupe del vault por URL canónica, conservando la
  nota más rica y moviendo duplicados a `dedupe_backup`.
- Scheduler Docker estabilizado a 6h por defecto con `SCHEDULER_INTERVAL_HOURS`.
- Añadido servicio `syncthing` y `Dockerfile.syncthing`.
- Logs sensibles saneados: `httpx/httpcore` no imprimen requests a nivel INFO y
  los tokens de Telegram se redactan defensivamente.
- Tests ampliados para auth, credenciales, frontier, dedupe, scrapers y writer.

## Requisitos

- Docker + Docker Compose, o Python 3.11+.
- Sesión de LinkedIn guardada en `volumes/user_data`.
- Cookies de X exportadas en `volumes/user_data/x_cookies.txt`.
- API key de un proveedor LLM OpenAI-compatible.
- Opcional: Telegram bot para notificaciones.
- Opcional: Bitwarden CLI (`bw`) para auto-login sin guardar secretos en el repo.

## Instalación

```bash
git clone git@github.com:mallorente/enmestador.git
cd enmestador
cp .env.example .env
# Edita .env con tus claves y rutas
```

## Autenticación

### LinkedIn

LinkedIn usa el perfil persistente de Chromium en `volumes/user_data`.

```bash
AUTH_PLATFORM=linkedin docker compose --profile auth up auth
```

Abre `http://localhost:6080`, completa login/MFA si aparece, y cierra el run
cuando la sesión quede guardada.

En local:

```bash
python auth/cookie_refresher.py --platform linkedin
```

### X

X usa cookies exportadas en formato Netscape:

```text
volumes/user_data/x_cookies.txt
```

Para refrescarlas usa el refresher dedicado:

```bash
python -m scripts.refresh_x_cookies --auto-login
```

En un servidor sin navegador local, ejecútalo dentro del contenedor `auth` con
un display/noVNC igual que el servicio de login interactivo. El handoff más
reciente en `docs/handoffs/` conserva el comando largo usado en producción.

El comando siguiente abre X en el perfil persistente general, pero por sí solo
no actualiza `x_cookies.txt`; sirve para debug visual, no como refresco final de
las cookies que usa el pipeline:

```bash
AUTH_PLATFORM=x docker compose --profile auth up auth
```

El pipeline carga esas cookies con `XAuthManager` en un browser context limpio.
Esto evita mezclar el estado de X con el perfil persistente de LinkedIn.

### Bitwarden

`auth/credentials.py` conecta el pipeline con Bitwarden CLI para que los
refreshers puedan rellenar usuario/password sin guardar secretos en git, en
handoffs ni en logs.

La resolución de credenciales va en este orden:

1. Variables directas: `LINKEDIN_USERNAME` / `LINKEDIN_PASSWORD` o
   `X_USERNAME` / `X_PASSWORD`.
2. Comandos explícitos: `*_USERNAME_CMD` / `*_PASSWORD_CMD`.
3. Atajo Bitwarden: `*_BITWARDEN_ITEM`, que se expande internamente a
   `bw get username <item>` y `bw get password <item>`.

En producción se usa el atajo Bitwarden. Los items esperados son:

```text
Linkedin
X
```

Cada item debe tener:

- `username`: email/usuario de la cuenta;
- `password`: contraseña de la cuenta.

Antes de lanzar un refresher con `--auto-login`, desbloquea Bitwarden en la
misma shell:

```bash
bw login
export BW_SESSION="$(bw unlock --raw)"
bw sync
```

Configura `.env` con nombres de items, no con secretos:

```env
LINKEDIN_BITWARDEN_ITEM=Linkedin
X_BITWARDEN_ITEM=X
```

Uso con LinkedIn:

```bash
python auth/cookie_refresher.py --platform linkedin --auto-login
```

Uso con X:

```bash
python -m scripts.refresh_x_cookies --auto-login
```

También puedes usar comandos explícitos:

```env
LINKEDIN_USERNAME_CMD=bw get username Linkedin
LINKEDIN_PASSWORD_CMD=bw get password Linkedin
X_USERNAME_CMD=bw get username X
X_PASSWORD_CMD=bw get password X
```

Si el binario no se llama `bw` en tu entorno:

```env
BITWARDEN_CLI=/ruta/a/bw
```

El `BW_SESSION` nunca debe escribirse en `.env`: es un secreto temporal de la
shell. Si `bw status` no muestra `unlocked`, el auto-login no podrá recuperar
credenciales.

#### Por Qué Esta Conexión Es Segura Frente A LLMs

La integración con Bitwarden ocurre **solo en el proceso local** que ejecuta el
refresher. El LLM del pipeline no recibe usuario, contraseña, `BW_SESSION`,
cookies ni tokens de Bitwarden.

Separación de responsabilidades:

- `auth/credentials.py` ejecuta `bw get username <item>` y
  `bw get password <item>` localmente con `subprocess.run`.
- Esos valores solo se usan para rellenar el formulario de login en el navegador
  controlado por Patchright.
- El enriquecimiento LLM (`pipeline/llm.py`) solo recibe contenido de bookmarks:
  texto del post, thread, artículos extraídos, URLs públicas e imágenes.
- Los secretos no se serializan en las notas Markdown ni en los JSON sidecars.
- `.env.example` documenta nombres de items, no valores secretos.
- `.env`, `volumes/user_data`, cookies y estado runtime no se commitean.
- Los logs evitan imprimir requests `httpx` a nivel INFO y redactan tokens de
  Telegram defensivamente.

Límites reales:

- El proceso local sí ve la contraseña en memoria el tiempo necesario para
  rellenar el login.
- El navegador recibe la contraseña igual que si la escribieras manualmente.
- Si alguien tiene acceso shell a la máquina con `BW_SESSION` desbloqueado,
  puede usar `bw` mientras dure esa sesión.

Por eso la regla operativa es: desbloquear Bitwarden solo para refrescar sesión,
no guardar `BW_SESSION`, no pegar contraseñas en prompts/handoffs/logs, y cerrar
la shell o hacer `bw lock` cuando termines.

Si aparece MFA, captcha o checkpoint, el sistema no intenta saltarlo: deja el
navegador abierto para resolverlo manualmente.

## Uso

### Docker

```bash
docker compose build

# Run puntual
docker compose run --rm pipeline

# Run solo X, sin escribir estado ni notas
docker compose run --rm pipeline python main.py --source x --delta-only --dry-run

# Run histórico completo a una carpeta nueva
docker compose run --rm pipeline \
  python main.py --fresh-run \
  --output-dir /app/volumes/llm_wiki_seed/Bookmarks/bookmarks_full

# Scheduler permanente
docker compose up -d scheduler
docker compose logs -f scheduler
```

### Local

```bash
pip install -r requirements.txt
pip install -r requirements-dev.txt
patchright install chromium

python main.py
python main.py --source linkedin --delta-only --dry-run
python scheduler.py --interval-hours 6
```

### Syncthing

El servicio Syncthing expone el vault completo:

```bash
docker compose up -d syncthing
```

Volumen sincronizado:

```text
volumes/llm_wiki_seed -> /data/llm_wiki_seed
```

UI:

```text
http://localhost:8384
```

## Configuración

| Variable | Descripción |
|----------|-------------|
| `LLM_BASE_URL` | Base URL del proveedor LLM OpenAI-compatible |
| `LLM_API_KEY` | API key |
| `LLM_MODEL` | Modelo primario |
| `LLM_MODEL_FALLBACK_1/2` | Modelos de fallback en el mismo proveedor |
| `LLM_FALLBACK_3_*` / `LLM_FALLBACK_4_*` | Proveedores/modelos extra |
| `TELEGRAM_BOT_TOKEN` | Token del bot Telegram |
| `TELEGRAM_CHAT_ID` | Chat ID Telegram |
| `MAX_BOOKMARKS` | Máximo de bookmarks por fuente por run |
| `MAX_EXTERNAL_ARTICLES` | Máximo de artículos/tweets referenciados por bookmark |
| `MAX_CONCURRENT` | Concurrencia de procesamiento |
| `DELTA_STOP_AFTER_KNOWN` | Número de conocidos consecutivos para cortar delta |
| `DELTA_FRONTIER_SIZE` | Tamaño de frontera reciente persistida por fuente |
| `SCHEDULER_INTERVAL_HOURS` | Intervalo del scheduler Docker |
| `HEADLESS` | `true` en servidor, `false` para debug visual |
| `USER_DATA_DIR` | Perfil/cookies de browser |
| `STATE_DIR` | Cursores, locks, dead letters y fronteras |
| `OUTPUT_DIR` | Carpeta de salida de notas |

## Arquitectura

```text
main.py                     — orquestador principal y CLI
config.py / models.py       — configuración y modelos Pydantic
scheduler.py                — loop periódico
healthcheck.py              — healthcheck Docker

auth/
  manager.py                — perfil persistente LinkedIn
  x_manager.py              — contexto limpio X + x_cookies.txt
  credentials.py            — carga segura de credenciales externas
  cookie_refresher.py       — login interactivo
  setup_linkedin_session.py — utilidades de sesión LinkedIn

scrapers/
  x.py                      — GraphQL/DOM X bookmarks
  linkedin.py               — API/DOM LinkedIn saved posts

extractors/
  playwright.py             — hilos X, posts LinkedIn, páginas JS
  web.py                    — extracción trafilatura/httpx

pipeline/
  llm.py                    — enriquecimiento LLM y fallbacks
  writer.py                 — Markdown + JSON sidecars
  notifier.py               — Telegram con redacción de tokens
  state.py                  — cursores, lock, processed URLs, dead letter
  frontier.py               — frontera delta por URLs conocidas
  dedupe.py                 — dedupe de vault por URL canónica

scripts/
  refresh_x_cookies.py      — refresca x_cookies.txt
  export_raw_bookmarks.py   — export auxiliar de material crudo
```

## Estructura De Volúmenes

```text
volumes/
  user_data/
    x_cookies.txt           ← cookies X exportadas
    x_profile/              ← perfil dedicado para refrescar X
    ...                     ← perfil persistente LinkedIn
  state/
    cursors.json
    processed_urls.json
    bookmark_frontiers.json
    dead_letters.jsonl
  llm_wiki_seed/
    raw/                    ← datos crudos/importaciones
    Bookmarks/
      .obsidian/
      bookmarks/
        x/
        linkedin/
      dedupe_backup/
    wiki/                   ← conocimiento compilado por agentes
```

## Conexión Con LLM Wiki

`volumes/llm_wiki_seed` es el vault raíz de LLM Wiki. `enmestador` no escribe
directamente en `wiki/`; escribe material fuente limpio en:

```text
volumes/llm_wiki_seed/Bookmarks/bookmarks
```

La separación es intencionada:

- `Bookmarks/bookmarks/` contiene fuentes atomizadas, con una nota por bookmark.
- `raw/` conserva material crudo o importaciones no normalizadas.
- `wiki/` es la base de conocimiento durable, curada por agentes.

El contrato práctico es:

1. `enmestador` trae material nuevo desde X/LinkedIn.
2. El LLM del pipeline resume y etiqueta cada pieza, pero no decide todavía si
   algo entra en la wiki durable.
3. Obsidian o un agente lee `Bookmarks/bookmarks/**`.
4. Si una nota contiene una idea reutilizable, el agente la promueve a `wiki/`:
   conceptos, entidades, claims, decisiones, mapas, preguntas, playbooks o
   síntesis.
5. El agente actualiza `wiki/index.md` y `wiki/log.md` según las instrucciones
   del vault.

En otras palabras: `Bookmarks/` es la bandeja de entrada enriquecida; `wiki/` es
el conocimiento destilado.

## Uso En El Contexto De LLM Wiki

Abre `volumes/llm_wiki_seed` como vault de Obsidian o sincronízalo con
Syncthing. Dentro del vault:

- revisa material nuevo en `Bookmarks/bookmarks/x` y
  `Bookmarks/bookmarks/linkedin`;
- usa el frontmatter `tags`, `source`, `url`, `external_urls`,
  `referenced_tweet_urls` e `image_urls` para filtrar;
- usa los JSON sidecars cuando un agente necesite estructura completa;
- trata cada nota como evidencia o material de entrada, no como conocimiento
  final;
- cuando algo merezca persistir, crea o actualiza notas en `wiki/`.

Flujo recomendado para agentes:

```text
Read Bookmarks note
  -> identify reusable claim/concept/entity/question
  -> add/update wiki/* note
  -> link back to source URL or bookmark note
  -> update wiki/index.md if a new durable page exists
  -> append wiki/log.md with the maintenance action
```

El pipeline ya ayuda a ese trabajo dejando cada nota con:

- resumen;
- takeaway;
- tags;
- texto original;
- thread/artículo extraído cuando existe;
- imágenes;
- links y tweets referenciados.

## Verificación Operativa

```bash
# Tests
test_venv_new/bin/pytest -q

# Estado de servicios
docker compose ps

# Logs scheduler
docker compose logs -f scheduler

# Dry-run de X
docker compose run --rm pipeline python main.py --source x --delta-only --dry-run
```

## Roadmap

- Bot Telegram bidireccional para recuperación de sesiones desde móvil.
- Deduplicación semántica sobre contenido, además de URL canónica.
- YouTube saved videos con transcripción y resumen.
- UI mínima para dead letters.
- Integración más explícita con el mantenimiento automático de `wiki/`.
- NotebookLM como destino adicional.

## Licencia

MIT
