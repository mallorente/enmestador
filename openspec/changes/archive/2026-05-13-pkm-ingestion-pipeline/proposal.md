# Proposal: PKM Automated Ingestion Pipeline

## Intent

Build a headless, containerized pipeline that extracts saved bookmarks from X.com and LinkedIn, enriches them via LLM (DeepSeek V4 Pro), and writes structured Markdown into an Obsidian vault. Eliminates manual copy-paste and preserves knowledge with consistent metadata.

## Scope

### In Scope
- X.com bookmark scraping via GraphQL interception with cursor-based pagination
- LinkedIn bookmark scraping via API interception + DOM fallback
- Web article extraction with `trafilatura`
- LLM enrichment (3-bullet summary, 1-sentence takeaway, tags) with fallback chain
- Markdown writer with YAML frontmatter to flat output directory
- Telegram alerts for critical failures
- Docker containerization with cron scheduling and lock-file concurrency guard
- `pytest` + `ruff` test suite from day 1

### Out of Scope
- JS-heavy page rendering (trafilatura warnings only)
- Parallel scraping (sequential v1)
- OpenClaw notifications, tag vocabulary normalizer, auto cookie refresh
- `setup_auth.py` headful login helper

## Capabilities

### New Capabilities
- `x-bookmark-scrape`: GraphQL interceptor, cursor pagination, bootstrap + delta modes
- `linkedin-bookmark-scrape`: API interceptor with DOM fallback
- `web-article-extract`: Fetch and extract article text via trafilatura
- `llm-note-enrichment`: DeepSeek V4 Pro processing with flash/qwen fallback
- `obsidian-note-writer`: Markdown generation with YAML frontmatter
- `telegram-notify`: Critical alert dispatch

### Modified Capabilities
- None

## Approach

Sequential async Python pipeline orchestrated by `main.py`. Playwright persistent `user_data_dir` handles auth. State (cursors, processed URLs, dead letter) stored as JSON in mounted volume. Rate limiting: 1 req/sec to LLM, exponential backoff, 3 retries per model before fallback. Lock file prevents cron overlap.

## Affected Areas

| Area | Impact | Description |
|------|--------|-------------|
| `automations-somex/` | New | Entire greenfield project |
| `Dockerfile` | New | `python:3.11-slim-bookworm` single-execution image |
| `docker-compose.yml` | New | Volumes for `user_data/`, `state/`, `obsidian_output/` |
| `tests/` | New | pytest suite for extractors, processor, writer |

## Risks

| Risk | Likelihood | Mitigation |
|------|------------|------------|
| X/LinkedIn auth expiration | Med | Telegram alert + manual refresh workflow |
| GraphQL schema changes | Med | WARNING logs, skip + dead letter, do not abort |
| LLM rate limits / downtime | Low | Triple fallback chain (deepseek-v4 → flash → qwen) |
| trafilatura fails on JS sites | High | WARNING + process post text only |

## Rollback Plan

Stop cron job. Revert Docker image to previous tag. State files (`processed_urls.json`, cursors) are version-controlled or backed up on host; restore to pre-run snapshot to re-process safely.

## Dependencies

- Debian host `rohan` with Docker + cron
- Telegram bot token
- DeepSeek API key
- Valid Playwright browser profiles in `user_data/`

## Success Criteria

- [ ] Pipeline runs end-to-end nightly without manual intervention
- [ ] Dead letter file remains empty under normal operations
- [ ] All generated Markdown files contain valid YAML frontmatter and 3-bullet summary
- [ ] pytest suite passes with >80% coverage on core modules
- [ ] Zero pipeline aborts due to single bookmark failure

## Effort Estimate

~5-7 days: 2 days scaffolding + Docker, 2 days scrapers + extractor, 1 day LLM processor + writer, 1-2 days tests + integration.
