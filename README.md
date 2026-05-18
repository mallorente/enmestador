# enmestador

> *Del verbo "enmestar" — recoger la mies, traer el grano a casa.*

Pipeline PKM personal que scrapa los bookmarks de X.com y los posts guardados de LinkedIn, los enriquece con un LLM, y escribe notas Markdown listas para Obsidian.

---

**Sobre el nombre.** *Enmestar* es un término del habla de la Huerta de Murcia (el panocho, dialecto de la región de Murcia, España), derivado de *mies* (del latín *messis*: la cosecha de cereal madura, lista para segar). El prefijo verbalizador *en-* convierte el sustantivo en acción: ir al campo, recoger lo que está listo, traerlo a casa. No está en el diccionario de la RAE, pero sigue vivo en el vocabulario agrícola murciano.

---

```
X bookmarks ──┐
              ├─► Playwright scraper ─► LLM enrichment ─► Obsidian Markdown
LinkedIn ──────┘
```

## Qué hace

- **Scraping** de bookmarks de X y posts guardados de LinkedIn via intercepción de GraphQL/API con Playwright (sin necesidad de API keys de Twitter/LinkedIn)
- **Extracción de hilos completos** en X — recoge todos los tweets del autor en un thread
- **Enriquecimiento LLM**: resumen en 3 bullets, takeaway, etiquetas. Compatible con cualquier API OpenAI-compatible (OpenRouter, NVIDIA, etc.)
- **Múltiples fallbacks** de modelo para garantizar disponibilidad
- **Notas Markdown** limpias para Obsidian, organizadas por fuente
- **Notificaciones Telegram** al terminar cada run y ante errores de auth
- **Scheduler** integrado: corre el pipeline cada N horas como servicio Docker

## Requisitos

- Docker + Docker Compose, o Python 3.11+
- Cookies exportadas de X.com y LinkedIn (formato Netscape)
- API key de un LLM compatible con OpenAI (OpenRouter, OpenCode, NVIDIA, etc.)
- (Opcional) Bot de Telegram para notificaciones

## Instalación

```bash
git clone https://github.com/mallorente/enmestador.git
cd enmestador
cp .env.example .env
# Edita .env con tus claves
```

### Exportar cookies

Instala la extensión [Get cookies.txt LOCALLY](https://chromewebstore.google.com/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc) en Chrome:

1. Ve a **x.com** (logueado) → extensión → Export → guarda como `x_cookies.txt`
2. Ve a **linkedin.com** (logueado) → extensión → Export → guarda como `li_cookies.txt`
3. Coloca ambos en `volumes/user_data/`

## Uso

### Con Docker (recomendado)

```bash
# Build
docker compose build

# Run puntual
docker compose run --rm pipeline

# Run histórico completo en carpeta nueva
docker compose run --rm pipeline \
  python main.py --fresh-run --output-dir /app/volumes/obsidian_output_full

# Scheduler permanente (cada 6h, se reinicia solo)
docker compose up -d scheduler
docker compose logs -f scheduler
```

### Sin Docker

```bash
pip install -r requirements.txt
playwright install chromium

python main.py                    # run normal (delta — solo bookmarks nuevos)
python main.py --fresh-run \
  --output-dir obsidian_output_full  # run histórico completo
python scheduler.py               # scheduler (cada 6h por defecto)
```

### Refrescar cookies caducadas

```bash
python auth/cookie_refresher.py             # abre Chrome visible, detecta sesión, exporta
python auth/cookie_refresher.py --platform x
python auth/cookie_refresher.py --platform linkedin
```

El pipeline detecta automáticamente cuando una sesión caduca (0 bookmarks + redirect a login) y envía una notificación Telegram con las instrucciones.

## Configuración

Copia `.env.example` a `.env` y rellena:

| Variable | Descripción |
|----------|-------------|
| `LLM_BASE_URL` | Base URL de tu proveedor LLM (OpenAI-compatible) |
| `LLM_API_KEY` | API key |
| `LLM_MODEL` | Modelo primario (ej. `deepseek-v4-pro`) |
| `LLM_MODEL_FALLBACK_1/2` | Modelos de fallback |
| `TELEGRAM_BOT_TOKEN` | Token del bot de Telegram (opcional) |
| `TELEGRAM_CHAT_ID` | Chat ID para notificaciones (opcional) |
| `MAX_BOOKMARKS` | Máximo de bookmarks por fuente por run (default: 500) |
| `MAX_CONCURRENT` | Concurrencia de procesamiento (default: 3) |
| `HEADLESS` | `true` para servidor, `false` para debug local |

## Arquitectura

```
main.py                     — orquestador principal, CLI
config.py / models.py       — configuración y modelos de datos
scheduler.py                — loop de ejecución periódica

scrapers/
  x.py                      — intercepción GraphQL Bookmarks de X.com
  linkedin.py               — intercepción API de posts guardados de LinkedIn

extractors/
  playwright.py             — extracción de hilos X y posts LinkedIn con Playwright
  web.py                    — extracción de artículos externos (trafilatura)

auth/
  manager.py                — gestión del browser context con cookies
  cookie_loader.py          — carga de cookies en formato Netscape
  cookie_refresher.py       — refresh interactivo de cookies caducadas

pipeline/
  llm.py                    — enriquecimiento LLM con fallbacks
  writer.py                 — escritura de notas Markdown
  notifier.py               — notificaciones Telegram
  state.py                  — persistencia de cursores y URLs procesadas
```

## Estructura de volúmenes

```
volumes/
  user_data/          ← cookies del browser (x_cookies.txt, li_cookies.txt)
  state/              ← cursores de paginación y URLs procesadas
  obsidian_output/    ← notas Markdown generadas (monta en tu vault de Obsidian)
```

## Roadmap

### Próximamente

- **Bot Telegram bidireccional** — recibir cookies como adjunto vía Telegram para refrescar sesión desde el móvil sin necesidad de acceder al servidor
- **Deduplicación semántica** — detectar bookmarks con contenido similar ya procesado

### v2

- **YouTube saved videos** — scraping de vídeos guardados y listas de reproducción, con transcripción y resumen LLM

### Ideas abiertas

- Interfaz web mínima para revisar el dead letter (bookmarks que fallaron)
- Exportación a formatos alternativos (Notion, Logseq)
- Soporte para más fuentes (Reddit saved, Pocket, Instapaper)

## Licencia

MIT
