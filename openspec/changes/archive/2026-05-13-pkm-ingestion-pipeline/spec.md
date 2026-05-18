# PKM Automated Ingestion Pipeline — Specification

## Context

Greenfield Python 3.11+ pipeline. Extracts bookmarks from X.com (GraphQL interception) and LinkedIn (API interception + DOM fallback), enriches via DeepSeek LLM, and writes Obsidian-ready Markdown. Dockerized, cron-scheduled, sequential async execution.

---

## Domain: x-bookmark-scrape

### Purpose
Extract saved bookmarks from X.com by intercepting GraphQL responses, with cursor-based pagination and incremental delta support.

### Requirements

| ID | Requirement | Strength |
|---|---|---|
| XB-01 | The system MUST intercept GraphQL `Bookmarks` network responses via Playwright. | MUST |
| XB-02 | The system MUST extract tweet text, author handle, permalink, and created-at from each bookmark. | MUST |
| XB-03 | The system MUST support cursor-based pagination, extracting the next cursor from each response. | MUST |
| XB-04 | The system MUST persist the last successful cursor to `state/cursors.json`. | MUST |
| XB-05 | The system MUST skip bookmarks whose canonical URL exists in `state/processed_urls.json`. | MUST |
| XB-06 | The system MUST use a Playwright persistent profile from `volumes/user_data/x_profile/`. | MUST |
| XB-07 | The system SHOULD limit pagination to 500 bookmarks per run to respect rate limits. | SHOULD |

### Scenarios

#### Scenario: Bootstrap first run
- GIVEN no `state/cursors.json` exists
- WHEN the scraper executes
- THEN it starts from the newest bookmarks and persists the final cursor to `state/cursors.json`

#### Scenario: Incremental delta run
- GIVEN `state/cursors.json` contains a valid cursor
- WHEN the scraper executes
- THEN it resumes from that cursor and stops when it encounters a previously processed URL

#### Scenario: Duplicate detection
- GIVEN a bookmark whose URL is in `state/processed_urls.json`
- WHEN the scraper encounters it
- THEN it skips extraction and continues to the next bookmark

#### Scenario: Empty bookmarks
- GIVEN the user has zero saved bookmarks
- WHEN the scraper executes
- THEN it returns an empty list and does not modify `state/cursors.json`

### Input/Output Contracts

| Input | Type | Source |
|---|---|---|
| `user_data_dir` | Directory path | `volumes/user_data/x_profile/` |
| `cursor_file` | File path | `state/cursors.json` |
| `processed_urls` | Set of strings | `state/processed_urls.json` |

| Output | Type | Destination |
|---|---|---|
| `bookmarks` | List[`XBookmark`] | `llm-note-enrichment` domain |
| `next_cursor` | String or null | `state/cursors.json` |

### Error Handling

| Error | Handling |
|---|---|
| Auth failure (401/403) | Log ERROR, notify via Telegram, abort scraper domain (not pipeline) |
| GraphQL schema change | Log WARNING, skip item, write to dead letter |
| Rate limit (429) | Exponential backoff (1s → 2s → 4s), max 3 retries |
| Network timeout | Retry once, then skip + dead letter |

### Dependencies
- `auth_manager` — Playwright context injection
- `models` — `XBookmark` Pydantic model

---

## Domain: linkedin-bookmark-scrape

### Purpose
Extract saved posts from LinkedIn via API response interception, falling back to DOM scraping when interception fails.

### Requirements

| ID | Requirement | Strength |
|---|---|---|
| LB-01 | The system MUST intercept LinkedIn API responses for saved posts. | MUST |
| LB-02 | The system MUST fallback to DOM scraping if API interception yields zero posts after 60 seconds. | MUST |
| LB-03 | The system MUST extract post text, author name, post URL, and created-at. | MUST |
| LB-04 | The system MUST skip posts whose URL exists in `state/processed_urls.json`. | MUST |
| LB-05 | The system MUST use a Playwright persistent profile from `volumes/user_data/linkedin_profile/`. | MUST |

### Scenarios

#### Scenario: API interception success
- GIVEN a valid LinkedIn session in `user_data_dir`
- WHEN the saved-posts page loads
- THEN the system intercepts the API response and extracts all posts without DOM traversal

#### Scenario: DOM fallback
- GIVEN API interception yields zero posts after 60s
- WHEN the fallback triggers
- THEN it scrolls the saved-posts feed and extracts posts from DOM elements

#### Scenario: LinkedIn empty state
- GIVEN the user has no saved posts
- WHEN the scraper executes
- THEN it returns an empty list and does not raise an error

### Input/Output Contracts

| Input | Type | Source |
|---|---|---|
| `user_data_dir` | Directory path | `volumes/user_data/linkedin_profile/` |
| `processed_urls` | Set of strings | `state/processed_urls.json` |

| Output | Type | Destination |
|---|---|---|
| `posts` | List[`LinkedInPost`] | `llm-note-enrichment` domain |

### Error Handling

| Error | Handling |
|---|---|
| Auth failure | Log ERROR, notify via Telegram, abort scraper domain |
| API schema change | WARNING, skip + dead letter |
| DOM fallback timeout after 5 min | WARNING, abort scraper domain |
| Network timeout | Retry once, then skip |

### Dependencies
- `auth_manager` — Playwright context injection
- `models` — `LinkedInPost` Pydantic model

---

## Domain: web-article-extract

### Purpose
Fetch and extract clean article text from URLs shared in bookmarks using `trafilatura`.

### Requirements

| ID | Requirement | Strength |
|---|---|---|
| WE-01 | The system MUST fetch the raw HTML for each bookmark URL. | MUST |
| WE-02 | The system MUST extract article title and body text via `trafilatura`, and SHOULD extract publication date as best-effort (fallback to extraction timestamp). | MUST / SHOULD |
| WE-03 | The system SHOULD warn when `trafilatura` returns empty content on JS-heavy pages. | SHOULD |
| WE-04 | The system MUST return `None` for extraction failures instead of crashing. | MUST |
| WE-05 | The system MUST respect a 30-second timeout per URL fetch. | MUST |

### Scenarios

#### Scenario: Standard article extraction
- GIVEN a bookmark linking to a static HTML article
- WHEN `trafilatura` processes the URL
- THEN it returns the title, body, and publication date (if available) and passes the result to the LLM processor

#### Scenario: JS-heavy page warning
- GIVEN a bookmark linking to a JS-rendered page
- WHEN `trafilatura` returns empty content
- THEN it logs a WARNING and proceeds with post text only (no article enrichment)

#### Scenario: Timeout
- GIVEN a URL that does not respond within 30 seconds
- WHEN the fetch times out
- THEN it returns `None` and logs a WARNING

### Input/Output Contracts

| Input | Type | Source |
|---|---|---|
| `url` | String | `x-bookmark-scrape` or `linkedin-bookmark-scrape` |

| Output | Type | Destination |
|---|---|---|
| `article` | `Article` or `None` | `llm-note-enrichment` |

### Error Handling

| Error | Handling |
|---|---|
| DNS failure / 404 | Log WARNING, return `None` |
| Timeout (>30s) | Log WARNING, return `None` |
| Empty trafilatura result | Log WARNING, return `None` |
| Redirect chain >5 | Log WARNING, return `None` |

### Dependencies
- `trafilatura` (external library)
- `models` — `Article` Pydantic model

---

## Domain: llm-note-enrichment

### Purpose
Enrich bookmark data via DeepSeek LLM to generate summaries, takeaways, and tags.

### Requirements

| ID | Requirement | Strength |
|---|---|---|
| LE-01 | The system MUST send a structured prompt to DeepSeek V4 Pro. | MUST |
| LE-02 | The system MUST generate a 3-bullet summary, 1-sentence takeaway, and free-form tags. | MUST |
| LE-03 | The system MUST fallback to `deepseek-v4-flash` if V4 Pro fails after 3 retries. | MUST |
| LE-04 | The system MUST fallback to `qwen3.6-plus` if flash fails after 3 retries. | MUST |
| LE-05 | The system MUST rate-limit requests to 1 req/sec. | MUST |
| LE-06 | The system MUST apply exponential backoff (1s → 2s → 4s) on 429/5xx errors. | MUST |
| LE-07 | The system MUST include the original post text and extracted article text (if any) in the prompt. | MUST |
| LE-08 | The system MUST return raw text if all models fail, preserving the bookmark for manual review. | MUST |

### Scenarios

#### Scenario: Successful enrichment
- GIVEN a bookmark with post text and article text
- WHEN the LLM processor calls DeepSeek V4 Pro
- THEN it receives a 3-bullet summary, takeaway, and tags and passes the enriched note to the writer

#### Scenario: Model fallback chain
- GIVEN DeepSeek V4 Pro returns 5xx on 3 retries
- WHEN the fallback triggers
- THEN it calls `deepseek-v4-flash` and if that also fails 3 times it calls `qwen3.6-plus`

#### Scenario: Total model failure
- GIVEN all three models fail after 3 retries each
- WHEN enrichment completes
- THEN it returns the original text with empty summary/takeaway/tags and logs a WARNING

#### Scenario: Rate limiting
- GIVEN 10 bookmarks queued for enrichment
- WHEN processing begins
- THEN requests are spaced at ≥1 second intervals

### Input/Output Contracts

| Input | Type | Source |
|---|---|---|
| `bookmark` | `XBookmark` or `LinkedInPost` | Scrapers |
| `article` | `Article` or `None` | `web-article-extract` |

| Output | Type | Destination |
|---|---|---|
| `enriched_note` | `EnrichedNote` | `obsidian-note-writer` |

### Error Handling

| Error | Handling |
|---|---|
| 429 Rate Limit | Exponential backoff, retry up to 3 times |
| 5xx Server Error | Retry 3 times, then fallback to next model |
| Timeout (>60s) | Retry once, then fallback |
| Invalid JSON response | Log WARNING, retry once |
| All models fail | Return raw text, log WARNING |

### Dependencies
- `deepseek` API client (external)
- `models` — `EnrichedNote` Pydantic model
- `notifier` — for total model failure alerts

---

## Domain: obsidian-note-writer

### Purpose
Write enriched bookmarks as Markdown files with YAML frontmatter to a flat output directory.

### Requirements

| ID | Requirement | Strength |
|---|---|---|
| OW-01 | The system MUST write each enriched bookmark as a separate `.md` file. | MUST |
| OW-02 | The system MUST include YAML frontmatter with source URL, date, platform, and tags. | MUST |
| OW-03 | The system MUST sanitize filenames to be filesystem-safe (ASCII, no reserved chars). | MUST |
| OW-04 | The system MUST append the original post text and extracted article text under an "Original" heading. | MUST |
| OW-05 | The system MUST use a flat directory structure in `volumes/obsidian_output/`. | MUST |
| OW-06 | The system MUST append to existing files rather than overwrite when a filename collision occurs. | MUST |

### Scenarios

#### Scenario: Successful note creation
- GIVEN an enriched note with summary, takeaway, tags, and source URL
- WHEN the writer executes
- THEN it creates a `.md` file in `volumes/obsidian_output/` containing valid YAML frontmatter and Markdown body

#### Scenario: Filename collision
- GIVEN two bookmarks that would produce the same sanitized filename
- WHEN the writer processes the second
- THEN it appends the content to the existing file and adds a separator comment

### Input/Output Contracts

| Input | Type | Source |
|---|---|---|
| `enriched_note` | `EnrichedNote` | `llm-note-enrichment` |
| `output_dir` | Directory path | `volumes/obsidian_output/` |

| Output | Type | Destination |
|---|---|---|
| `.md` file | Markdown | `volumes/obsidian_output/` |

### Error Handling

| Error | Handling |
|---|---|
| Disk full | Log ERROR, notify Telegram, abort writer domain |
| Permission denied | Log ERROR, notify Telegram, abort writer domain |
| Filename collision | Append to existing file (not overwrite) |

### Dependencies
- `models` — `EnrichedNote` Pydantic model

---

## Domain: telegram-notify

### Purpose
Send critical alerts to a Telegram bot when pipeline domains fail or encounter unrecoverable errors.

### Requirements

| ID | Requirement | Strength |
|---|---|---|
| TN-01 | The system MUST send a Telegram message when any scraper domain fails authentication. | MUST |
| TN-02 | The system MUST send a Telegram message when the writer domain encounters a disk/permission error. | MUST |
| TN-03 | The system MUST send a Telegram message when all LLM models fail for a run. | MUST |
| TN-04 | The system MUST include the domain name, error summary, and timestamp in the alert. | MUST |
| TN-05 | The system SHOULD NOT send alerts for single-item skips, warnings, or dead-letter entries. | SHOULD NOT |
| TN-06 | The system MUST fail silently if the Telegram API is unavailable (do not abort pipeline). | MUST |

### Scenarios

#### Scenario: Auth failure alert
- GIVEN the X scraper encounters a 403 auth error
- WHEN the error is handled
- THEN a Telegram alert is dispatched including "X scraper: Authentication failed"

#### Scenario: Silent skip
- GIVEN a single bookmark is skipped due to GraphQL schema change
- WHEN the skip is handled
- THEN no Telegram alert is sent and the item is written to the dead letter file

### Input/Output Contracts

| Input | Type | Source |
|---|---|---|
| `alert_payload` | Dict[str, str] | Any failing domain |

| Output | Type | Destination |
|---|---|---|
| Telegram message | HTTP POST | Telegram Bot API |

### Error Handling

| Error | Handling |
|---|---|
| Telegram API 5xx | Log WARNING, do not retry |
| Invalid bot token | Log ERROR once, disable notifier for run |
| Network timeout | Log WARNING, continue pipeline |

### Dependencies
- Telegram Bot API (external)

---

## Domain: main-orchestrator

### Purpose
Coordinate the sequential async pipeline, manage cron lock file, and ensure no single failure aborts the entire run.

### Requirements

| ID | Requirement | Strength |
|---|---|---|
| MO-01 | The system MUST implement an asyncio event loop that runs domains sequentially. | MUST |
| MO-02 | The system MUST check for a lock file at `state/pipeline.lock` before starting. | MUST |
| MO-03 | The system MUST exit immediately if the lock file exists and is younger than 4 hours. | MUST |
| MO-04 | The system MUST create the lock file at start and remove it on exit (success or failure). | MUST |
| MO-05 | The system MUST never abort the entire pipeline due to a single bookmark failure. | MUST |
| MO-06 | The system MUST write failed items to `state/dead_letter.jsonl` (JSON Lines format) with error context. | MUST |
| MO-07 | The system MUST append successfully processed URLs to `state/processed_urls.json`. | MUST |
| MO-08 | The system MUST log the start and end of each domain execution. | MUST |

### Scenarios

#### Scenario: Normal nightly run
- GIVEN no lock file exists
- WHEN the orchestrator starts
- THEN it creates the lock file, runs X scraper → LinkedIn scraper → web extractor → LLM processor → writer, and removes the lock file on completion

#### Scenario: Cron overlap prevention
- GIVEN `state/pipeline.lock` exists and is 10 minutes old
- WHEN a second cron job starts
- THEN it logs "Pipeline already running" and exits with code 0

#### Scenario: Single failure survival
- GIVEN 5 bookmarks where the 3rd causes an exception
- WHEN the pipeline processes them
- THEN the 3rd is written to `state/dead_letter.json` and the 4th and 5th continue processing

#### Scenario: Lock file cleanup on crash
- GIVEN the pipeline crashes unexpectedly
- WHEN the next run starts after >4 hours
- THEN it treats the stale lock as invalid, creates a new lock file, and proceeds

### Input/Output Contracts

| Input | Type | Source |
|---|---|---|
| `lock_file` | File path | `state/pipeline.lock` |
| `processed_urls_file` | File path | `state/processed_urls.json` |
| `dead_letter_file` | File path | `state/dead_letter.jsonl` |

| Output | Type | Destination |
|---|---|---|
| `processed_urls` | JSON append | `state/processed_urls.json` |
| `dead_letter` | JSON append | `state/dead_letter.json` |
| `.md` files | Markdown | `volumes/obsidian_output/` |

### Error Handling

| Error | Handling |
|---|---|
| Uncaught exception in domain | Log ERROR, notify Telegram, continue to next domain |
| Lock file orphaned (>4h) | Ignore stale lock, proceed |
| State file corruption | Log ERROR, start with empty state, notify Telegram |

### Dependencies
- All domain modules
- `notifier` — for critical failures
