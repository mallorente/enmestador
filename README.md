# enmestador

> *Del verbo "enmestar" — recoger la mies, traer el grano a casa.*

`enmestador` recoge bookmarks de X.com y posts guardados de LinkedIn, extrae su
contenido, los resume con un LLM y los deja como notas Markdown listas para
Obsidian y LLM Wiki.

```text
X bookmarks ──┐
              ├─► Patchright scraper ─► LLM enrichment ─► LLM Wiki / Obsidian
LinkedIn ─────┘
```

## Qué Hace

- Scrapea X bookmarks y LinkedIn saved posts sin API oficial.
- Extrae hilos de X, posts de LinkedIn, articulos externos, tweets
  referenciados e imagenes.
- Enriquece cada pieza con resumen, takeaway y tags.
- Escribe notas `.md` con frontmatter y `.json` sidecars.
- Mantiene delta scraping con cursores, frontera de conocidos y dedupe de vault.
- Corre de forma puntual o programada con Docker scheduler.
- Puede notificar por Telegram y sincronizar el vault con Syncthing.

## Salida

El flujo de produccion escribe en:

```text
volumes/llm_wiki_seed/Bookmarks/bookmarks
```

Notas por fuente:

```text
volumes/llm_wiki_seed/Bookmarks/bookmarks/x
volumes/llm_wiki_seed/Bookmarks/bookmarks/linkedin
```

`Bookmarks/` es la bandeja de entrada enriquecida. El conocimiento durable vive
en `volumes/llm_wiki_seed/wiki`.

## Quickstart

```bash
git clone git@github.com:mallorente/enmestador.git
cd enmestador
cp .env.example .env
docker compose build
docker compose run --rm pipeline
```

Servicios habituales en servidor:

```bash
docker compose up -d scheduler syncthing
```

Ver logs del scheduler:

```bash
docker compose logs -f scheduler
```

## Documentación

- [Docs index](docs/README.md): mapa de la documentacion.
- [Operations](docs/operations.md): instalacion, auth, Bitwarden, Telegram,
  scheduler, Syncthing y configuracion.
- [LLM Wiki](docs/llm-wiki.md): como se conecta el pipeline con el vault y como
  usar las notas en el contexto de LLM Wiki.
- [Recent Changes](docs/recent-changes.md): resumen de lo integrado en la ultima
  tanda de trabajo.
- [Handoffs](docs/handoffs/): notas operativas historicas de sesiones largas.

## Organización Del Proyecto

- `auth/`: sesiones, cookies y credenciales externas.
- `scrapers/`: obtencion de bookmarks/posts desde X y LinkedIn.
- `extractors/`: extraccion de contenido web, threads y posts.
- `pipeline/`: enriquecimiento, escritura, estado, frontera, dedupe y Telegram.
- `scripts/`: utilidades manuales o one-shot.
- `docs/`: operacion, LLM Wiki, planes y handoffs.
- `tests/`: cobertura de scrapers, pipeline, auth y utilidades.

## Telegram

Telegram es opcional. Si configuras `TELEGRAM_BOT_TOKEN` y `TELEGRAM_CHAT_ID`,
el pipeline manda resumen de runs, errores por bookmark y avisos de auth
caducada. Ver [Operations](docs/operations.md#telegram).

## Scheduler

El scheduler Docker ejecuta el pipeline al arrancar y luego cada 6h por defecto.
Se configura con `SCHEDULER_INTERVAL_HOURS`.

```bash
docker compose up -d scheduler
docker compose logs -f scheduler
```

Ver [Operations](docs/operations.md#scheduler).

## Next Steps

- Mantener Telegram como canal operativo principal de errores/auth.
- Hacer mas comodo el refresco remoto de X cookies desde Docker/noVNC.
- Anadir dedupe semantico ademas del dedupe por URL.
- Crear UI minima para revisar dead letters.
- Automatizar mejor la promocion de `Bookmarks/` a `wiki/`.
- Anadir YouTube saved videos con transcripcion y resumen.

## Licencia

MIT
