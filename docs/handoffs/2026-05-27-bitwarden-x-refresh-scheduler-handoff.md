# Handoff: Bitwarden, X Refresh, Daily Scheduler

Date: 2026-05-27
Repo: `/home/miguel/enmestador`

## Suggested Skills

- `diagnose`: use for auth/session regressions, especially X refresh failures or LinkedIn/X scrape drift.
- `handoff`: use again at the end of the next long session, but keep the note in `docs/handoffs/`.
- `tdd`: use before touching auth, scheduler, or cursor/state behavior.

## Current State

The current Obsidian-facing vault is:

```text
volumes/llm_wiki_seed/Bookmarks/bookmarks
```

That path matters because it contains `.obsidian`, and `main.py` now uses that to decide whether vault dedupe should run.

Current scheduler setup:

- Docker scheduler is running.
- Default interval is back to `6h`.
- `scheduler.py` defaults to `--interval-hours 6`.
- `docker-compose.yml` uses `SCHEDULER_INTERVAL_HOURS` with a `6` fallback.

Bitwarden state:

- Bitwarden CLI is installed on the host.
- `bw status` reported `unlocked` when provided with the active `BW_SESSION`.
- The relevant Bitwarden items exist and are accessible after `bw sync`.
- The item names are `Linkedin` and `X`.
- Do not store or echo `BW_SESSION`, passwords, or recovery codes in future notes.

X state:

- `volumes/user_data/x_cookies.txt` was successfully refreshed using the X refresh script with Bitwarden-backed auto-login.
- `ScraperX` now loads the refreshed cookies via `XAuthManager`.
- A post-refresh dry-run for X returned `would_process=0`, so the cookies are currently usable.
- X still produced some `403` responses on auxiliary endpoints during scraping, but the run completed successfully and fell back to DOM when needed.

LinkedIn state:

- LinkedIn continues to use the persistent profile in `volumes/user_data`.
- The LinkedIn auth refresher now supports Bitwarden-backed auto-login via `--auto-login`.
- LinkedIn login detection rejects login/authwall/checkpoint pages and login-form noise.

## Important Changes Made Recently

### Bitwarden-backed credentials

Added:

```text
auth/credentials.py
```

It loads credentials from one of these sources, in order:

1. Direct env vars: `X_USERNAME` / `X_PASSWORD` or `LINKEDIN_USERNAME` / `LINKEDIN_PASSWORD`
2. Command env vars: `*_USERNAME_CMD` / `*_PASSWORD_CMD`
3. Bitwarden item names: `*_BITWARDEN_ITEM`

This keeps secrets out of git and out of the handoff.

### Auto-login support

Updated:

```text
auth/cookie_refresher.py
scripts/refresh_x_cookies.py
```

Behavior:

- `auth/cookie_refresher.py --auto-login` can fill LinkedIn or X credentials from the configured provider and then wait for a real logged-in session.
- `scripts/refresh_x_cookies.py --auto-login` can renew `x_cookies.txt` directly.
- `scripts/refresh_x_cookies.py` needs to run with `PYTHONPATH=/app` inside the auth container when invoked as a module.

### Scheduler

Updated:

```text
scheduler.py
docker-compose.yml
.env
.env.example
README.md
```

Current cadence:

- default: every `6h`
- override: `SCHEDULER_INTERVAL_HOURS`

The scheduler service is currently started and healthy enough to run the pipeline on its interval.

### Vault dedupe and output layout

The vault-aware dedupe is still keyed off the presence of `.obsidian`, not the literal directory name `vault`.

Output and note-writing should continue to use:

```text
volumes/llm_wiki_seed/Bookmarks/bookmarks
```

## Commands That Matter

Check scheduler:

```bash
docker compose logs -f scheduler
```

Recreate scheduler after changing interval:

```bash
docker compose up -d --force-recreate scheduler
```

Renew X cookies interactively with Bitwarden-backed auto-login:

```bash
docker compose run --rm --service-ports auth bash -lc "
  rm -f /tmp/.X99-lock /tmp/.X11-unix/X99 &&
  Xtigervnc :99 -rfbport 5910 -SecurityTypes None -geometry 1280x800 -depth 24 -ac -localhost no 2>/tmp/vnc.log &
  for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do test -S /tmp/.X11-unix/X99 && break; sleep 0.5; done &&
  test -S /tmp/.X11-unix/X99 || (cat /tmp/vnc.log && exit 1) &&
  ln -sf /usr/share/novnc/vnc.html /usr/share/novnc/index.html &&
  websockify --web /usr/share/novnc 6080 localhost:5910 &
  sleep 1 &&
  env DISPLAY=:99 PYTHONPATH=/app X_USERNAME='...' X_PASSWORD='...' python -m scripts.refresh_x_cookies --auto-login --timeout 900
"
```

In practice, `X_USERNAME` and `X_PASSWORD` were loaded from Bitwarden, not hardcoded.

Dry-run verification for X after refresh:

```bash
docker compose run --rm --build -e HEADLESS=true -e DELTA_STOP_AFTER_KNOWN=1 -e TELEGRAM_BOT_TOKEN= -e TELEGRAM_CHAT_ID= pipeline python main.py --delta-only --dry-run --source x
```

That returned `would_process=0`.

## Verification Already Run

Relevant test batches passed after the Bitwarden and refresh changes:

```text
tests/test_credentials.py
tests/test_scraper_linkedin.py
tests/test_scraper_x.py
```

Full suite also passed:

```text
280 passed, 3 warnings
```

The warnings are the same existing AsyncMock/pytest-asyncio warnings already seen in E2E tests.

## What To Do Next

1. If X breaks again, rerun `scripts/refresh_x_cookies.py --auto-login` with Bitwarden available and then validate with a X-only dry-run.
2. If Bitwarden items get renamed, update `.env` and `auth/credentials.py` configuration, not the secret values.
3. If scheduler behavior needs tuning, change `SCHEDULER_INTERVAL_HOURS` and recreate the scheduler service.
4. If LinkedIn or X scraping starts returning new candidates unexpectedly, inspect URL canonicalization and frontier state before changing extraction logic.

## Sensitive Data

Do not include any of the following in future handoffs or logs:

- `BW_SESSION`
- passwords
- recovery codes
- cookie values
- API keys
