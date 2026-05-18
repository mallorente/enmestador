# Tasks: PKM Automated Ingestion Pipeline

## Review Workload Forecast

| Field | Value |
|-------|-------|
| Estimated changed lines | ~2,280 |
| 400-line budget risk | High |
| Chained PRs recommended | Yes |
| Suggested split | PR 1 → PR 2 → PR 3 → PR 4 → PR 5 → PR 6 → PR 7 → PR 8 |
| Delivery strategy | auto-chain |
| Chain strategy | stacked-to-main |

Decision needed before apply: No
Chained PRs recommended: Yes
Chain strategy: stacked-to-main
400-line budget risk: High

### Suggested Work Units

| Unit | Goal | PR | Base | Deps |
|------|------|----|------|------|
| 1 | Models & Foundation | PR 1 | main | — |
| 2 | State & Writer | PR 2 | main | PR 1 |
| 3 | Notifier | PR 3 | main | PR 1 |
| 4 | Auth & X Scraper | PR 4 | main | PR 1, PR 2 |
| 5 | LinkedIn Scraper & Web Extractor | PR 5 | main | PR 1, PR 2, PR 4 |
| 6 | LLM Processor | PR 6 | main | PR 1 |
| 7 | Main Orchestrator | PR 7 | main | PR 1–6 |
| 8 | Docker | PR 8 | main | PR 1–7 |

## Phase 1: Foundation (PR 1, ~250 lines)

- [x] 1.1 `models.py`: all Pydantic models (`Source`, `ScrapeMode`, `Bookmark`, `ExtractedContent`, `Enrichment`, `EnrichedBookmark`, `DeadLetter`, `PipelineResult`)
- [x] 1.2 `requirements.txt` (pydantic, playwright, trafilatura, httpx, pyyaml, python-dotenv) + `requirements-dev.txt` (pytest, pytest-asyncio, ruff)
- [x] 1.3 `pytest.ini` (asyncio_mode=auto) + `conftest.py` (tmp_state_dir fixture)
- [x] 1.4 `tests/test_models.py`: validation, JSON round-trip, invalid URL rejection
- [x] 1.5 `ruff check .` — zero errors

## Phase 2: State & Writer (PR 2, ~350 lines)

- [x] 2.1 `state.py`: `CursorsStore`, `ProcessedUrlStore`, `DeadLetterWriter`, `LockFile` (acquire/stale-check >4h/release)
- [x] 2.2 `writer.py`: filename sanitizer → YAML frontmatter (title, source, url, saved, tags, model) → append-on-collision
- [x] 2.3 `tests/test_state.py`: empty-state bootstrap, stale lock, dead-letter append, processed-URL dedup
- [x] 2.4 `tests/test_writer.py`: frontmatter validation, reserved-char sanitization, collision append with separator

## Phase 3: Notifier (PR 3, ~150 lines)

- [x] 3.1 `notifier.py`: `async send(domain, error_summary)` → Telegram Bot API POST; silent fail on API error/token invalid
- [x] 3.2 `tests/test_notifier.py`: mock Telegram API; verify alert format (domain+timestamp), silent fail on 5xx, token-error disable

## Phase 4: Auth & X Scraper (PR 4, ~400 lines)

- [x] 4.1 `auth_manager.py`: `ensure_browser(user_data_dir)` → `launch_persistent_context` with headless env flag
- [x] 4.2 `scraper_x.py`: intercept GraphQL `Bookmarks` → extract text/author/permalink/created_at → cursor pagination (max 500) → skip processed
- [x] 4.3 `tests/test_scraper_x.py`: mock GraphQL for bootstrap, delta resume, empty, duplicate; verify cursor save on exit

## Phase 5: LinkedIn Scraper & Web Extractor (PR 5, ~400 lines)

- [x] 5.1 `scraper_linkedin.py`: API interception (60s timeout) → DOM scroll fallback → extract post text/author/url/created_at → skip processed
- [x] 5.2 `web_extractor.py`: `fetch(url, 30s)` → `trafilatura.extract()` → `ExtractedContent` or None
- [x] 5.3 `tests/test_scraper_linkedin.py`: mock API success, API-empty→DOM fallback, empty saved-posts
- [x] 5.4 `tests/test_web_extractor.py`: mock trafilatura success/empty/timeout/DNS-failure; verify None on failure

## Phase 6: LLM Processor (PR 6, ~350 lines)

- [x] 6.1 `llm_processor.py`: prompt builder → DeepSeek V4 (3 retries, exp backoff 1s→2s→4s) → flash fallback → qwen fallback → parse SUMMARY/TAKEAWAY/TAGS; rate limiter 1 req/sec
- [x] 6.2 `tests/test_llm_processor.py`: mock all success, all-models-fail, backoff timing, rate limiter, prompt includes article+post

## Phase 7: Main Orchestrator (PR 7, ~300 lines)

- [x] 7.1 `main.py`: lock → auth → scrape X → scrape LinkedIn → deduplicate → extract → enrich → write → notify → update cursors/processed → release lock
- [x] 7.2 Per-bookmark try/except → dead letter → continue (no pipeline abort)
- [x] 7.3 `tests/test_main.py`: e2e mocks all modules; stale lock detection; cron overlap prevention; single-failure survival; crash lock cleanup

## Phase 8: Docker (PR 8, ~80 lines)

- [x] 8.1 `Dockerfile`: python:3.11-slim-bookworm, playwright chromium deps, CMD python main.py
- [x] 8.2 `docker-compose.yml`: pipeline service + volumes (user_data, state, obsidian_output)
- [x] 8.3 `.env.example`: all env vars from design (LLM_BASE_URL, LLM_API_KEY, TELEGRAM_BOT_TOKEN, etc.)
- [x] 8.4 Verify `docker compose build` succeeds
