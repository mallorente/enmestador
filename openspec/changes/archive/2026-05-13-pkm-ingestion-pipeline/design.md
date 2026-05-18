# Design: PKM Automated Ingestion Pipeline

## Technical Approach

Sequential async pipeline. `main.py` orchestrates `AuthManager → Scraper(X|LinkedIn) → WebExtractor → LLMProcessor → Writer → Notifier`. Each stage yields to the next; any single-bookmark failure is caught, logged, and moved to dead letter without aborting the pipeline. Cursor-based pagination with bootstrap-massive + incremental-delta mode, gated by `state/cursors.json`. Lock file (`state/pipeline.lock`) prevents cron overlap. Rate limit: 1 req/sec to LLM with 3-retry exponential backoff per model before fallback.

## Architecture Decisions

| Decision | Option A | Option B | Choice | Rationale |
|----------|----------|----------|--------|-----------|
| Auth persistence | Cookie files | Playwright `user_data_dir` | **Playwright profiles** | Survives cookie expiration; cross-platform; no crypto to manage |
| LLM fallback chain | Single model + retry | DeepSeek V4 → flash → qwen | **Triple fallback** | Covers API downtime and rate limits; qwen is free-tier insurance |
| Web extraction | `trafilatura` | `readability-lxml` + custom | **trafilatura** | Battle-tested; handles encoding/charset noise; JS deferred to v2 |
| State persistence | SQLite | JSON files in mounted volume | **JSON files** | Human-readable, debuggable, zero dependencies; volume < 100K entries |
| Output structure | Nested dirs by source/date | Flat directory | **Flat directory** | Obsidian-friendly; prevents path explosion; YAML frontmatter carries metadata |

## Data Flow

```
cron ──→ main.py (acquire lock)
              │
              ▼
        AuthManager.ensure_browser()
              │
              ▼
   ScraperX.scrape(mode) ──→ ScraperLinkedIn.scrape(mode)
         │        │                    │        │
         ▼        ▼                    ▼        ▼
    [Bookmark list]              [Bookmark list]
         │                             │
         └──────┬──────────────────────┘
                ▼
         Deduplication (canonical URL)
                │
                ▼
         WebExtractor.extract(url)
                │  ┌── trafilatura success → full_text
                │  └── trafilatura fail → post_text only + WARNING
                ▼
         LLMProcessor.enrich(bookmark)
                │  ┌── deepseek-v4 → flash → qwen
                │  └── all fail → skip + dead_letter
                ▼
         Writer.write_note(bookmark, enrichment)
                ▼
         Notifier.send_summary()  ← only on critical failures
                ▼
         main.py (update cursors, release lock)
```

## File Changes

| File | Action | Description |
|------|--------|-------------|
| `main.py` | Create | Pipeline orchestrator: lock, loop, error boundary per bookmark |
| `auth_manager.py` | Create | Playwright context factory with `user_data_dir` |
| `scraper_x.py` | Create | X.com GraphQL interception, cursor pagination |
| `scraper_linkedin.py` | Create | LinkedIn API interception + DOM fallback |
| `web_extractor.py` | Create | `trafilatura` fetch + extract wrapper |
| `llm_processor.py` | Create | DeepSeek API client with fallback chain |
| `writer.py` | Create | Obsidian Markdown generator |
| `notifier.py` | Create | Telegram Bot API dispatch |
| `models.py` | Create | Pydantic models for all data structures |
| `Dockerfile` | Create | `python:3.11-slim-bookworm` single-exec |
| `docker-compose.yml` | Create | Three volume mounts |
| `requirements.txt` | Create | Runtime dependencies |
| `requirements-dev.txt` | Create | `pytest`, `pytest-asyncio`, `ruff` |
| `tests/` | Create | Mirrors `automations-somex/` structure |

## Interfaces / Contracts

### Pydantic Models (`models.py`)

```python
from pydantic import BaseModel, HttpUrl
from datetime import datetime
from enum import Enum

class Source(str, Enum):
    X = "x"
    LINKEDIN = "linkedin"

class ScrapeMode(str, Enum):
    BOOTSTRAP = "bootstrap"
    DELTA = "delta"

class Bookmark(BaseModel):
    source: Source
    url: HttpUrl
    title: str
    post_text: str | None = None
    saved_at: datetime | None = None

class ExtractedContent(BaseModel):
    url: HttpUrl
    full_text: str | None = None
    post_text: str | None = None
    extraction_method: str  # "trafilatura" | "post_only"
    extracted_at: datetime

class Enrichment(BaseModel):
    summary_bullets: list[str]  # exactly 3
    takeaway: str
    tags: list[str]  # Libre v1 freeform
    model_used: str
    tokens: int

class EnrichedBookmark(BaseModel):
    bookmark: Bookmark
    content: ExtractedContent
    enrichment: Enrichment | None  # None if all LLMs failed

class DeadLetter(BaseModel):
    bookmark: Bookmark
    error: str
    stage: str  # "extraction" | "llm" | "write"
    timestamp: datetime

class PipelineResult(BaseModel):
    processed: int
    enriched: int
    dead_letter: int
    new_cursor_x: str | None
    new_cursor_linkedin: str | None
```

### State File Schemas

**`state/cursors.json`**:
```json
{
  "$schema": "cursors",
  "x": { "cursor": "abc123==", "mode": "delta", "last_run": "2026-05-13T02:00:00Z" },
  "linkedin": { "cursor": "xyz789==", "mode": "delta", "last_run": "2026-05-13T02:00:00Z" }
}
```

**`state/processed_urls.json`**:
```json
{
  "$schema": "processed",
  "urls": {
    "https://example.com/article": { "first_seen": "2026-05-10T...", "source": "x" }
  }
}
```

**`state/dead_letter.jsonl`** (JSON Lines):
```
{"url":"https://...","error":"Timeout","stage":"extraction","timestamp":"..."}
```

### YAML Frontmatter

```yaml
---
title: "Article Title"
source: "x"
url: "https://example.com/article"
saved: 2026-05-13T01:30:00Z
tags: [llm, architecture, python]
model: deepseek-v4-pro
---
```

### LLM Prompt Template

```
You are a knowledge curator. Summarize this article in exactly 3 bullets
that capture its key insights, followed by 1 takeaway sentence, and 3-5
lowercase tags.

Article:
Title: {title}
URL: {url}
{content}

Respond in this EXACT format:
SUMMARY:
- bullet 1
- bullet 2
- bullet 3
TAKEAWAY: one sentence
TAGS: tag1, tag2, tag3
```

### Function Signatures (key modules)

```python
# auth_manager.py
class AuthManager:
    async def ensure_browser(self) -> BrowserContext: ...
    async def close(self) -> None: ...

# scraper_x.py
class ScraperX:
    async def scrape(self, mode: ScrapeMode, cursor: str | None) -> list[Bookmark]: ...

# scraper_linkedin.py
class ScraperLinkedIn:
    async def scrape(self, mode: ScrapeMode, cursor: str | None) -> list[Bookmark]: ...

# web_extractor.py
class WebExtractor:
    async def extract(self, url: str) -> ExtractedContent: ...

# llm_processor.py
class LLMProcessor:
    async def enrich(self, content: ExtractedContent) -> Enrichment | None: ...
    async def _try_model(self, model: str, prompt: str) -> str | None: ...

# writer.py
class Writer:
    def write(self, enriched: EnrichedBookmark) -> Path: ...

# notifier.py
class Notifier:
    async def send(self, message: str) -> bool: ...
    async def send_summary(self, result: PipelineResult) -> None: ...
```

## Error Handling Strategy

| Module | Failure | Strategy |
|--------|---------|----------|
| `auth_manager` | Profile corrupt / browser crash | Raise — abort pipeline (unrecoverable) |
| `scraper_x` | GraphQL schema change | Log WARNING, skip, continue next bookmark |
| `scraper_linkedin` | API rate limit | Exponential backoff 3x, then DOM fallback |
| `web_extractor` | trafilatura empty / timeout | Fall back to `post_text`, flag `extraction_method: post_only` |
| `llm_processor` | Model timeout/error | Retry 3x (1s→2s→4s), fallback to next model; all fail → `Enrichment=None`, dead letter |
| `writer` | Disk full / permission | Log CRITICAL, send Telegram alert, abort |
| `notifier` | Telegram API down | Log ERROR, continue — notification is best-effort |
| `main.py` | Lock file stale (>2h) | Delete stale lock, acquire fresh |

## Testing Strategy

| Module | Unit Tests | Integration Tests |
|--------|-----------|-------------------|
| `models.py` | Pydantic validation, JSON round-trip | — |
| `auth_manager.py` | — (requires browser) | Playwright fixture, real chromium |
| `scraper_x.py` | Mocked GraphQL responses, cursor logic | `pytest-recording` VCR for real API snapshots |
| `scraper_linkedin.py` | Mocked API responses, DOM parsing | VCR snapshots |
| `web_extractor.py` | Mocked `trafilatura` returns, fallback logic | Real HTTP fetch against static test page |
| `llm_processor.py` | Mocked DeepSeek API, fallback chain, prompt builder | Real API call (1 test), VCR recorded |
| `writer.py` | YAML frontmatter validation, file output | Write to `tmp_path`, verify frontmatter |
| `notifier.py` | Mocked Telegram API | — |
| `main.py` | Lock file logic, deduplication | End-to-end with all mocks, one real flow |

**Coverage goal**: >80% on core modules (`scraper_x`, `llm_processor`, `writer`).
**Test isolation**: `tmp_path` for writer output. No shared state between tests.

## Docker Layout

```
automations-somex/
├── Dockerfile          # FROM python:3.11-slim-bookworm, pip install, COPY, CMD ["python", "main.py"]
├── docker-compose.yml  # services: pipeline, volumes: [user_data, state, obsidian_output]
├── volumes/
│   ├── user_data/      # 0700, uid 1000. Playwright persistent profiles
│   ├── state/          # 0755. cursors.json, processed_urls.json, dead_letter.jsonl, pipeline.lock
│   └── obsidian_output/# 0755. Output .md files, mounted to host Obsidian vault
└── tests/              # NOT in image. Run via docker-compose run test or pip install -r requirements-dev.txt
```

**Permissions**: Container runs as `uid=1000`. Host volumes `chown 1000:1000` before first run.
**Cron entry** (host): `0 2 * * * cd /opt/automations-somex && docker compose run --rm pipeline`

## Environment Variables (`.env` template)

```ini
DEEPSEEK_API_KEY=sk-...
DEEPSEEK_V4_MODEL=deepseek-chat
DEEPSEEK_FLASH_MODEL=deepseek-chat
QWEN_MODEL=qwen-turbo
QWEN_API_KEY=sk-...
TELEGRAM_BOT_TOKEN=123456:ABC-DEF
TELEGRAM_CHAT_ID=-1001234567890
LLM_RATE_LIMIT=1.0
PLAYWRIGHT_HEADLESS=true
STATE_DIR=/state
OUTPUT_DIR=/obsidian_output
USER_DATA_DIR=/user_data
```

## Open Questions

- [ ] DeepSeek V4 Pro exact model ID (provisionally `deepseek-chat`)
- [ ] X.com GraphQL endpoint URL (investigate during implementation)
- [ ] LinkedIn saves endpoint (may require cookie from browser profile)
