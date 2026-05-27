# Handoff: Vault Notes, LinkedIn Auth, Dedupe

Date: 2026-05-26
Repo: `/home/miguel/enmestador`

## Suggested Skills

- `diagnose`: use for auth/session regressions or stuck scraper runs.
- `handoff`: use at the end of future long sessions, but save under
  `docs/handoffs/`, not `/tmp`.
- `tdd`: use before changing cursor, writer, or scraper behavior.

## Current State

The Obsidian-facing vault is:

```text
volumes/llm_wiki_seed/vault
```

Bookmark notes are under:

```text
volumes/llm_wiki_seed/vault/bookmarks/x
volumes/llm_wiki_seed/vault/bookmarks/linkedin
```

Current deduped note counts:

- X: `471` `.md`
- LinkedIn: `81` `.md`
- Duplicate groups remaining by URL: `0`
- Notes without detectable URL: `0`

Raw LinkedIn export from the refreshed session:

```text
volumes/llm_wiki_seed/raw/2026-05-26_linkedin_refresh
```

Dedupe backup:

```text
volumes/llm_wiki_seed/dedupe_backup/20260526_115232
```

That backup contains `dedupe_report.json` plus the moved duplicate Markdown
files. Nothing was deleted.

## Important Changes Made

### Separate Notes By Default

`pipeline/writer.py` now writes one Markdown note per bookmark using deterministic
filenames:

```text
{sanitized-title}-{source-id-or-stable-hash}.md
```

It no longer appends colliding bookmarks into one Markdown file. Matching JSON
sidecars use the same basename.

Output defaults now point at:

```text
volumes/llm_wiki_seed/vault/bookmarks
```

Updated files include:

- `pipeline/writer.py`
- `config.py`
- `.env`
- `.env.example`
- `docker-compose.yml`
- `Dockerfile`
- `healthcheck.py`
- `README.md`
- `tests/test_writer.py`
- `tests/test_e2e.py`

### Raw Export Writes Notes To The Vault

`scripts/export_raw_bookmarks.py` now keeps raw JSON/JSONL in its `--output-dir`
but writes Markdown notes through `Writer` to `--notes-dir`, defaulting to
`DEFAULT_OUTPUT_DIR`.

### Cursor Safety For X

During a delta run, X found `33` candidate bookmarks, but LinkedIn auth failed
and later the run hung after writing `31` X notes. A bug was found: the X scraper
persisted the cursor before the orchestrator had processed bookmarks.

Fix:

- `scrapers/x.py` now supports `persist_cursor=False` and exposes
  `last_cursor`.
- `main.py` creates `ScraperX(..., persist_cursor=False)`.
- `main.py` only saves the captured X cursor after processing completes.
- `main.py` guards against mocked `last_cursor` values by accepting only `str`.

The X cursor in `volumes/state/cursors.json` was restored to:

```text
HBa+9cyPnJaEkDAAAA==
```

This means the next normal pipeline run will revisit the same X window. The
`31` already-written/processed X bookmarks should be skipped by
`processed_urls.json`; the remaining candidates can still be processed.

### LinkedIn Auth Detection Fix

The refresher previously accepted the LinkedIn login form as a valid session
because `_page_has_real_session()` only required a LinkedIn URL and non-empty
body text.

Fix in `auth/cookie_refresher.py`:

- Reject login/authwall/checkpoint/`/uas/` URLs.
- Reject login form markers such as `Iniciar sesión`, `Email o teléfono`, and
  `Contraseña`.
- Require authenticated-page markers such as `Saved Posts`, `My Items`,
  `Messaging`, or Spanish nav equivalents.

Tests added:

- `tests/test_scraper_linkedin.py::TestCookieRefresherSessionDetection`

Verified:

```text
pytest tests/test_scraper_linkedin.py::TestCookieRefresherSessionDetection tests/test_scraper_linkedin.py::TestCookieRefresherNoPlawright
```

passed.

### LinkedIn Refresh And Scrape

After the auth detection fix:

- `docker compose --profile auth run --rm --service-ports -e AUTH_PLATFORM=linkedin auth`
  reported an authenticated session.
- A LinkedIn-only raw scrape in headless mode loaded:

  ```text
  https://www.linkedin.com/my-items/saved-posts/
  ```

- Page title was:

  ```text
  Saved Posts | LinkedIn
  ```

- LinkedIn API interception did not produce saved posts, matching previous docs.
- DOM fallback worked and exported `81` bookmarks.

Command used:

```bash
docker compose run --rm --entrypoint python \
  -e HEADLESS=true \
  -e LI_GOTO_TIMEOUT=180 \
  -e LI_API_TIMEOUT=45 \
  -e LI_DOM_TIMEOUT=120 \
  pipeline scripts/export_raw_bookmarks.py \
  --source linkedin \
  --output-dir /app/volumes/llm_wiki_seed/raw/2026-05-26_linkedin_refresh \
  --state-dir /app/volumes/state \
  --notes-dir /app/volumes/llm_wiki_seed/vault/bookmarks \
  --max-items 500
```

## Dedupe Details

The user correctly questioned why LinkedIn had `81` new notes when many were
probably not new. A vault-level dedupe was run by canonical bookmark URL across
both `x` and `linkedin`.

Moved to backup:

- X: `28` duplicate `.md`
- LinkedIn: `79` duplicate `.md`
- Total: `107` duplicate `.md`

Kept version scoring favored:

1. Enriched notes.
2. Notes with JSON sidecars.
3. Notes with fuller extracted text/articles/images.
4. Larger Markdown when otherwise tied.

Backup path:

```text
volumes/llm_wiki_seed/dedupe_backup/20260526_115232
```

Report path:

```text
volumes/llm_wiki_seed/dedupe_backup/20260526_115232/dedupe_report.json
```

## Verification Already Run

Writer/main/X tests after cursor and writer changes:

```text
pytest tests/test_writer.py tests/test_main.py tests/test_scraper_x.py
```

Result:

```text
72 passed
```

LinkedIn auth detection tests:

```text
pytest tests/test_scraper_linkedin.py::TestCookieRefresherSessionDetection tests/test_scraper_linkedin.py::TestCookieRefresherNoPlawright
```

Result:

```text
4 passed
```

Compilation checks were also run for touched modules with `python3 -m py_compile`.

## /tmp Documentation Migration

The user requested bringing dispersed `/tmp` docs into the repo. A search of
`/tmp` at the end of the session found no surviving handoff Markdown files; the
earlier `/tmp/enmestador-handoff-2026-05-26.md` had disappeared. Its relevant
content was reconstructed from the conversation and saved as:

```text
docs/handoffs/2026-05-26-bookmark-notes-delta-imported-from-tmp.md
```

Future handoffs should be saved under:

```text
docs/handoffs/
```

## Recommended Next Steps

1. Run a normal pipeline delta after reviewing whether to limit the run:

   ```bash
   docker compose run --rm --build \
     -e LI_GOTO_TIMEOUT=180 \
     -e LI_API_TIMEOUT=45 \
     -e LI_DOM_TIMEOUT=120 \
     pipeline
   ```

   Expect X to resurface the partially processed window because its cursor was
   intentionally not advanced after the hung run. Already processed URLs should
   dedupe out.

2. If the full pipeline hangs again after writing most X notes, inspect the last
   processing item and consider adding per-bookmark extraction/enrichment
   timeouts.

3. Make dedupe a first-class pipeline step, not an ad hoc cleanup:
   - Run vault-level canonical URL dedupe after writes.
   - Prefer the most complete note using a transparent score: enrichment,
     JSON sidecar, extracted text, external articles, images, Markdown size.
   - Move losers to a timestamped backup directory and write a report, as done
     manually in `volumes/llm_wiki_seed/dedupe_backup/20260526_115232`.
   - Consider making this idempotent and safe to run independently.

4. Add a first-class "delta only" command/action:
   - It should run the scrape in delta mode without fresh/bootstrap behavior.
   - It should be easy to invoke for one source or both sources.
   - It should have a dry-run/report mode showing: cursor used, scraped count,
     deduped count, would-process count, and output locations.
   - It should never advance cursors before successful processing.

5. Consider updating `scripts/export_raw_bookmarks.py` to support a true
   new-only mode for LinkedIn. The latest raw LinkedIn export used bootstrap DOM
   fallback and created duplicates until vault-level dedupe was run.

6. Re-check auth/cookie resilience end to end:
   - Verify whether the current Patchright persistent-profile approach still
     works reliably for LinkedIn in both visible noVNC and headless pipeline
     modes.
   - Verify whether the isolated X cookie context still works consistently and
     whether `scripts/refresh_x_cookies.py` is still needed separately from
     `auth/cookie_refresher.py`.
   - Audit the remaining docs/code for stale "Playwright" wording versus
     current Patchright behavior.
   - Confirm that profile lock cleanup, `JSESSIONID` CSRF setup, and noVNC auth
     flow all behave after container restarts.

7. Explore autonomous auth refresh with a password manager:
   - The user asked whether, if they provide a password manager and credentials
     for X and LinkedIn, the agent could refresh cookies/sessions alone.
   - Feasible in principle if the password manager has a CLI or connector that
     can supply credentials and any MFA flow is supported or delegated.
   - Do not store passwords, cookie values, API keys, or recovery codes in the
     repo, handoffs, logs, or prompts.
   - Design should prefer: password-manager CLI lookup at runtime, secrets kept
     outside git, explicit allowlist of domains, and an interactive fallback for
     MFA/challenges.
   - For X, decide whether autonomous refresh should update
     `volumes/user_data/x_cookies.txt` or migrate X to another isolated
     persistent profile.
   - For LinkedIn, decide whether autonomous refresh should continue using the
     shared persistent profile in `volumes/user_data` or use a dedicated
     profile with a controlled handoff into the pipeline.

## Sensitive Data

Do not include cookie values, API keys, Telegram bot tokens, or Syncthing API
keys in docs or handoffs.
