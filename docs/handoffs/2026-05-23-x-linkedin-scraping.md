# Enmestador Handoff - 2026-05-23

## Suggested Skills

- `diagnose`: use for any future X/LinkedIn scraper regression. The failure modes are often browser/auth/profile related, not parser-only.
- `handoff`: use again before ending a long debugging session.

## Context

Project: `/home/miguel/enmestador`.

The user works on a remote machine (`miguel@rohan`) with no directly usable GUI. Do not tell them to open a browser locally on the server. For interactive auth/debugging, use the Docker noVNC service on port `6080`, normally via SSH tunnel:

```bash
ssh -L 6080:localhost:6080 <remote-host>
```

Then open locally:

```text
http://localhost:6080
```

## Current State

The project scrapes saved/bookmarked posts from X and LinkedIn and writes Markdown notes.

As of this handoff, the user reported that the latest fix "parece que funciona".

Important verified test result:

```bash
python3 -m pytest -q
```

Older result:

```text
245 passed, 3 warnings
```

Latest result after adding image extraction, JSON sidecars, external clipping,
internal X media filtering, and referenced tweet handling:

```text
256 passed, 3 warnings
```

The warnings are AsyncMock-related test warnings in E2E tests, not observed production failures.

## Key Diagnosis

LinkedIn and X need different browser/auth strategies.

LinkedIn:

- Works with Patchright + persistent Chromium profile in `volumes/user_data`.
- API saved-post endpoints did not reliably return saved posts.
- DOM fallback is the working path.
- The scraper must scroll aggressively to load more saved posts.

X:

- After moving to the shared persistent profile for LinkedIn, X could get stuck on a black screen with the X logo.
- The page loaded `https://x.com/i/bookmarks` with document status `200`, but the body was effectively empty/no useful articles.
- A request to `https://api.x.com/1.1/account/settings.json?...` returned `429`, but the user correctly pushed back that this was unlikely to be the whole explanation because X worked in older versions and there had been an 8-hour idle period.
- The important clue: older X worked via exported cookies, not via the persistent browser profile.
- There was no useful `x.com` entry in browser settings/profile, but `volumes/user_data/x_cookies.txt` exists and contains the needed X cookies (`auth_token`, `ct0`, `twid`). Do not record cookie values.

Resolution:

- Keep LinkedIn on the Patchright persistent profile.
- Isolate X into its own clean browser context and inject cookies from `volumes/user_data/x_cookies.txt`.
- This is implemented in `auth/x_manager.py`.

## Important Code Changes

New file:

- `auth/x_manager.py`: launches a clean Patchright Chromium context for X and injects Netscape-format cookies from `volumes/user_data/x_cookies.txt`.

Updated:

- `main.py`
  - Imports `XAuthManager`.
  - Creates `x_auth` separately from `AuthManager`.
  - Scrapes X with `x_auth.context`.
  - Scrapes LinkedIn with `auth.context`.
  - Uses the X context for X thread extraction and the LinkedIn context for LinkedIn extraction.
  - X health-check now uses `x_auth.context`, not the LinkedIn persistent context.

- `scrapers/x.py`
  - Added JS fetch/XHR response interception similar in spirit to LinkedIn.
  - Added DOM fallback if GraphQL/API extraction does not produce bookmarks.
  - Added support for `note_tweet` long-form text.
  - Parses `published_at` from GraphQL `legacy.created_at` and DOM `time[datetime]`.
  - Captures recent HTTP/API errors for diagnostics.
  - Extracts image URLs from GraphQL media entities and DOM-rendered tweet images.
  - Separates outbound web links from linked tweets:
    - external web links go to `external_urls` and are clipped.
    - linked tweets go to `referenced_tweet_urls` and are extracted with `extract_x_thread`.
    - X/Twitter media (`photo`, `video`, `pic.x.com`, `pbs.twimg.com`) is not treated as an external article.
    - unresolved raw `t.co` links are not clipped because they may be internal media and cannot be classified safely without an expanded URL.

- `scrapers/linkedin.py`
  - DOM fallback extracts saved posts.
  - Aggressive scroll loads more saved posts.
  - Extracts real post content rather than mostly author/profile text.
  - Parses approximate `published_at` from visible relative times where possible.
  - Extracts image URLs from API/DOM/Playwright content where possible.

- `models.py`
  - `Bookmark` now includes:
    - `published_at`
    - `saved_at`
    - `retrieved_at`
    - `external_urls`
    - `referenced_tweet_urls`
    - `image_urls`
  - `ExtractedContent` now includes:
    - `external_urls`
    - `referenced_tweet_urls`
    - `external_articles`
    - `image_urls`
  - `ExternalArticle` now includes:
    - `url`
    - `text`
    - `image_urls`
    - `extraction_method`

- `pipeline/writer.py`
  - Writes timestamp/link/media metadata into Markdown frontmatter:
    - `published`
    - `saved`
    - `retrieved`
    - `external_urls`
    - `referenced_tweet_urls`
    - `image_urls`
  - Writes Markdown sections:
    - `## Original`
    - `## Thread` for complete X thread extraction.
    - `## Images` for post/thread images.
    - `## Article: ...` for clipped outbound URLs.
    - `## Referenced Tweet: ...` for linked tweet/thread extraction.
  - Writes a `.json` sidecar next to every `.md`, containing the full `EnrichedBookmark` object.

- `docker-compose.yml` and `Dockerfile`
  - Added auth/noVNC support.
  - Auth service uses TigerVNC + noVNC on `6080`.

- `tests/conftest.py`
  - Autouse mock for `main.XAuthManager` so orchestrator tests do not launch a real browser.

## Commands That Matter

Run a small Docker pipeline test:

```bash
docker compose build pipeline
docker compose run --rm -e MAX_BOOKMARKS=5 -e X_API_TIMEOUT=20 -e X_DOM_TIMEOUT=60 pipeline
```

Preferred isolated 5x5 verification command, so it does not touch the real run
state/output:

```bash
docker compose build pipeline
docker compose run --rm \
  -e MAX_BOOKMARKS=5 \
  -e X_API_TIMEOUT=20 \
  -e X_DOM_TIMEOUT=60 \
  -e LI_DOM_TIMEOUT=60 \
  -e STATE_DIR=/app/volumes/state/test_run_referenced_tweets \
  -e OUTPUT_DIR=/app/volumes/obsidian_output/test_run_referenced_tweets \
  pipeline
```

Interactive auth service:

```bash
AUTH_PLATFORM=x docker compose --profile auth up --build --force-recreate auth
```

For LinkedIn:

```bash
AUTH_PLATFORM=linkedin docker compose --profile auth up --build --force-recreate auth
```

If the profile lock appears (`SingletonLock`, etc.), the code now attempts cleanup in the auth manager/cookie refresher, but stale Docker containers can still hold the profile. Prefer stopping the auth service before running the pipeline.

## Known Constraints

- Do not rely on a local GUI in Codex or on the remote server.
- noVNC path should be `http://localhost:6080` after the tunnel. A previous 404 was fixed by symlinking noVNC `index.html` to `vnc.html`.
- The browser profile in `volumes/user_data` may contain files owned by `nobody:nogroup` because of Docker. Avoid destructive cleanup unless the user explicitly approves.
- Do not print or save full cookie values in notes.

## If X Breaks Again

First check:

- Does `volumes/user_data/x_cookies.txt` still exist?
- Does it still contain `auth_token`, `ct0`, `twid`?
- Is the pipeline using `XAuthManager` and not `AuthManager` for X?
- Is the failure happening before DOM content appears? If yes, it is likely auth/browser/profile, not post parsing.

Then run:

```bash
docker compose run --rm -e MAX_BOOKMARKS=5 -e X_API_TIMEOUT=20 -e X_DOM_TIMEOUT=60 pipeline
```

Review scraper logs for:

- API response interception count.
- DOM fallback extraction count.
- Recent HTTP errors.

## X Link Semantics

The correct behavior for X links is:

- `t.co` with `expanded_url` pointing to a normal external site: store in `external_urls` and clip it into `external_articles`.
- `t.co` with `expanded_url` pointing to `x.com/.../status/...` or `twitter.com/.../status/...`: normalize to `https://x.com/.../status/...`, store in `referenced_tweet_urls`, and extract it with `extract_x_thread`.
- Raw `t.co` in text without an `expanded_url`: do not clip. It may be media/internal X content.
- X media (`/photo`, `/video`, `pic.x.com`, `pbs.twimg.com`) should be represented through `image_urls` where possible, not as an external article.

In Markdown:

- outbound clipped URLs appear as `## Article: <url>`.
- linked tweets appear as `## Referenced Tweet: <url>`.

In JSON:

- outbound clipped URLs are in `bookmark.external_urls`, `content.external_urls`, and `content.external_articles`.
- linked tweets are in `bookmark.referenced_tweet_urls`, `content.referenced_tweet_urls`, and the extracted referenced tweet/thread content is also stored in `content.external_articles` with `extraction_method: "playwright_x_thread"`.

Latest real Docker checks:

- `test_run_5x5_media_filter`: 5 X + 5 LinkedIn, processed 10, enriched 10, dead 0. Confirmed internal X media no longer produced failed external article clips.
- `test_run_referenced_tweets`: 5 X + 5 LinkedIn, processed 10, enriched 10, dead 0. This sample had external GitHub links and clipped them correctly; it did not contain linked tweets, so `referenced_tweet_urls` was `null` in that real sample. The linked-tweet behavior is covered by unit tests.

## If LinkedIn Misses Posts

Likely issue: scrolling did not reach all saved posts. The current fix uses aggressive scroll-to-bottom and keyboard `End`. Look at `scrapers/linkedin.py` DOM fallback/scroll loop before changing auth.

## Worktree Note

There are many uncommitted changes in the worktree from this debugging session. Do not revert them casually.
