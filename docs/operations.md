# Operations

Guia operativa para desplegar, autenticar, notificar y programar `enmestador`.

## Requisitos

- Docker + Docker Compose, o Python 3.11+.
- Sesion de LinkedIn guardada en `volumes/user_data`.
- Cookies de X exportadas en `volumes/user_data/x_cookies.txt`.
- API key de un proveedor LLM OpenAI-compatible.
- Opcional: Bitwarden CLI (`bw`) para auto-login sin guardar secretos.
- Opcional: bot de Telegram para notificaciones.

## Instalacion

```bash
git clone git@github.com:mallorente/enmestador.git
cd enmestador
cp .env.example .env
```

Edita `.env` con tus claves y rutas.

## Autenticacion

### LinkedIn

LinkedIn usa el perfil persistente de Chromium en `volumes/user_data`.

```bash
AUTH_PLATFORM=linkedin docker compose --profile auth up auth
```

Abre `http://localhost:6080`, completa login/MFA si aparece, y cierra el run
cuando la sesion quede guardada.

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

En un servidor sin navegador local, ejecuta ese script dentro del contenedor
`auth` con un display/noVNC igual que el servicio de login interactivo. El
handoff mas reciente en `docs/handoffs/` conserva el comando largo usado en
produccion.

Este comando abre X en el perfil persistente general, pero por si solo no
actualiza `x_cookies.txt`; sirve para debug visual:

```bash
AUTH_PLATFORM=x docker compose --profile auth up auth
```

El pipeline carga `x_cookies.txt` con `XAuthManager` en un browser context
limpio para no mezclar el estado de X con LinkedIn.

## Bitwarden

`auth/credentials.py` conecta los refreshers con Bitwarden CLI para rellenar
usuario/password sin guardar secretos en git, docs, handoffs ni logs.

La resolucion de credenciales va en este orden:

1. Variables directas: `LINKEDIN_USERNAME` / `LINKEDIN_PASSWORD` o
   `X_USERNAME` / `X_PASSWORD`.
2. Comandos explicitos: `*_USERNAME_CMD` / `*_PASSWORD_CMD`.
3. Atajo Bitwarden: `*_BITWARDEN_ITEM`, que se expande a
   `bw get username <item>` y `bw get password <item>`.

Items esperados:

```text
Linkedin
X
```

Cada item debe tener `username` y `password`.

Antes de lanzar un refresher con `--auto-login`, desbloquea Bitwarden en la
misma shell:

```bash
bw login
export BW_SESSION="$(bw unlock --raw)"
bw sync
```

En `.env`:

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

Alternativa con comandos explicitos:

```env
LINKEDIN_USERNAME_CMD=bw get username Linkedin
LINKEDIN_PASSWORD_CMD=bw get password Linkedin
X_USERNAME_CMD=bw get username X
X_PASSWORD_CMD=bw get password X
```

Si el binario no se llama `bw`:

```env
BITWARDEN_CLI=/ruta/a/bw
```

`BW_SESSION` nunca debe escribirse en `.env`; es un secreto temporal de la
shell. Si `bw status` no muestra `unlocked`, el auto-login no podra recuperar
credenciales.

### Seguridad Frente A LLMs

La integracion con Bitwarden ocurre solo en el proceso local que ejecuta el
refresher. El LLM del pipeline no recibe usuario, contrasena, `BW_SESSION`,
cookies ni tokens de Bitwarden.

- `auth/credentials.py` ejecuta `bw get ...` localmente con `subprocess.run`.
- Los valores solo se usan para rellenar el formulario de login en Patchright.
- `pipeline/llm.py` solo recibe contenido de bookmarks: texto, threads,
  articulos extraidos, URLs publicas e imagenes.
- Los secretos no se serializan en notas Markdown ni JSON sidecars.
- `.env.example` documenta nombres de items, no valores secretos.
- `.env`, `volumes/user_data`, cookies y estado runtime no se commitean.
- Los logs bajan `httpx/httpcore` a `WARNING` y redactan tokens de Telegram.

Limites reales:

- El proceso local ve la contrasena en memoria el tiempo necesario para rellenar
  el login.
- El navegador recibe la contrasena igual que si la escribieras manualmente.
- Si alguien tiene shell con `BW_SESSION` desbloqueado, puede usar `bw` mientras
  dure esa sesion.

Regla operativa: desbloquear Bitwarden solo para refrescar sesion, no guardar
`BW_SESSION`, no pegar secretos en prompts/handoffs/logs, y cerrar la shell o
hacer `bw lock` al terminar.

## Telegram

Telegram es opcional. Sirve para recibir:

- resumen al final de cada run;
- alerta si una plataforma parece haber caducado;
- errores por bookmark durante procesamiento/enriquecimiento.

Configura en `.env`:

```env
TELEGRAM_BOT_TOKEN=123456:...
TELEGRAM_CHAT_ID=...
```

Para obtenerlos:

1. Crea un bot con BotFather y copia el token.
2. Escribe al bot o anadelo al chat/grupo destino.
3. Obtiene el `chat_id` con `getUpdates` o con una herramienta equivalente.

Si falta una de las dos variables, el notifier se desactiva sin abortar el
pipeline. Si Telegram devuelve 401, el notifier se desactiva para ese run. Las
requests de `httpx` no se imprimen a nivel INFO y los tokens se redactan
defensivamente.

## Uso

### Docker

```bash
docker compose build

# Run puntual
docker compose run --rm pipeline

# Run solo X, sin escribir estado ni notas
docker compose run --rm pipeline python main.py --source x --delta-only --dry-run

# Run historico completo a una carpeta nueva
docker compose run --rm pipeline \
  python main.py --fresh-run \
  --output-dir /app/volumes/llm_wiki_seed/Bookmarks/bookmarks_full
```

### Local

```bash
pip install -r requirements.txt
pip install -r requirements-dev.txt
patchright install chromium

python main.py
python main.py --source linkedin --delta-only --dry-run
```

## Scheduler

El scheduler ejecuta el pipeline una vez al arrancar y despues duerme el
intervalo configurado.

Docker:

```bash
docker compose up -d scheduler
docker compose logs -f scheduler
```

Intervalo por defecto: 6 horas.

Para cambiarlo en `.env`:

```env
SCHEDULER_INTERVAL_HOURS=6
```

Recrear despues de cambiarlo:

```bash
docker compose up -d --build --force-recreate scheduler
```

Local:

```bash
python scheduler.py --interval-hours 6
```

Estado:

```bash
docker compose ps scheduler
docker compose logs --tail 120 scheduler
```

El scheduler usa lock file en `STATE_DIR` para evitar ejecuciones simultaneas.
`LOCK_STALE_HOURS` controla cuando se considera obsoleto un lock antiguo.

## Syncthing

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

## Configuracion

| Variable | Descripcion |
|----------|-------------|
| `LLM_BASE_URL` | Base URL del proveedor LLM OpenAI-compatible |
| `LLM_API_KEY` | API key |
| `LLM_MODEL` | Modelo primario |
| `LLM_MODEL_FALLBACK_1/2` | Modelos de fallback en el mismo proveedor |
| `LLM_FALLBACK_3_*` / `LLM_FALLBACK_4_*` | Proveedores/modelos extra |
| `TELEGRAM_BOT_TOKEN` | Token del bot Telegram |
| `TELEGRAM_CHAT_ID` | Chat ID Telegram |
| `MAX_BOOKMARKS` | Maximo de bookmarks por fuente por run |
| `MAX_EXTERNAL_ARTICLES` | Maximo de articulos/tweets referenciados por bookmark |
| `MAX_CONCURRENT` | Concurrencia de procesamiento |
| `DELTA_STOP_AFTER_KNOWN` | Numero de conocidos consecutivos para cortar delta |
| `DELTA_FRONTIER_SIZE` | Tamano de frontera reciente persistida por fuente |
| `SCHEDULER_INTERVAL_HOURS` | Intervalo del scheduler Docker |
| `HEADLESS` | `true` en servidor, `false` para debug visual |
| `USER_DATA_DIR` | Perfil/cookies de browser |
| `STATE_DIR` | Cursores, locks, dead letters y fronteras |
| `OUTPUT_DIR` | Carpeta de salida de notas |

## Verificacion

```bash
test_venv_new/bin/pytest -q
docker compose ps
docker compose logs -f scheduler
docker compose run --rm pipeline python main.py --source x --delta-only --dry-run
```
