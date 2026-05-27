# Handoff: Delta Frontier, Real 2-Bookmark Run, Obsidian Vault

Date: 2026-05-26
Repo: `/home/miguel/enmestador`

## Suggested Skills

- `diagnose`: use for any LinkedIn/X scraper mismatch, auth regression, or delta
  count that does not match the user’s manual saves.
- `handoff`: use again at the end of future long sessions, saving under
  `docs/handoffs/`, not `/tmp`.
- `tdd`: use before changing frontier, cursor, writer, or scraper behavior.

## User Intent For Next Session

Continue from the new delta-frontier work. The user asked how to add a new
Obsidian vault and then requested another delta test. The immediate remaining
issue is that after processing the 1 new X bookmark and 1 new LinkedIn bookmark,
a later dry-run still reports 1 LinkedIn candidate.

## Important Context

Obsidian-facing vault path:

```text
volumes/llm_wiki_seed/vault
```

Bookmark note paths:

```text
volumes/llm_wiki_seed/vault/bookmarks/x
volumes/llm_wiki_seed/vault/bookmarks/linkedin
```

User should open `LLM Wiki Seed/vault` as the Obsidian vault, not the raw
`LLM Wiki Seed` sync root.

Do not include cookie values, API keys, Telegram bot tokens, Syncthing API keys,
or raw secret env values in docs or chat.

## Changes Made In This Session

### First-Class Vault Dedupe

Added:

```text
pipeline/dedupe.py
tests/test_dedupe.py
```

Behavior:

- Reads Markdown frontmatter `url`.
- Groups notes by canonical URL.
- Keeps the richest note using score: enrichment, JSON sidecar, extracted
  content/articles/images, Markdown size, mtime.
- Moves loser `.md` and matching `.json` sidecars into timestamped
  `dedupe_backup`.
- Writes `dedupe_report.json`.

`main.py` only runs this dedupe automatically for the production vault notes
layout:

```text
.../vault/bookmarks
```

This avoids moving files from test/custom output directories.

### Delta CLI Improvements

Updated `main.py` with:

```bash
python main.py --source x|linkedin|both
python main.py --delta-only
python main.py --dry-run
```

Dry-run reports:

- sources
- cursor_used
- scraped
- deduped
- would_process
- frontier known counts
- output_dir
- state_dir

Dry-run does not write notes, processed URLs, cursor updates, frontier updates,
dedupe reports, or notifications.

### Delta Frontier / Known Sequence Stop

Added:

```text
pipeline/frontier.py
tests/test_frontier.py
```

Updated:

```text
config.py
main.py
scrapers/x.py
scrapers/linkedin.py
tests/test_scraper_x.py
tests/test_scraper_linkedin.py
```

Config:

```text
DELTA_STOP_AFTER_KNOWN=2
DELTA_FRONTIER_SIZE=20
```

For manual verification, commands used `DELTA_STOP_AFTER_KNOWN=1`.

How it works:

- Builds known URL sets from `processed_urls.json`, existing Markdown notes in
  source-specific vault folders, and `bookmark_frontiers.json`.
- Scrapers observe the saved/bookmark feed from the top.
- They add only unknown URLs as candidates.
- They stop when they hit `N` consecutive known URLs.
- Real successful runs save recent observed URLs to:

```text
volumes/state/bookmark_frontiers.json
```

This fixes the previous LinkedIn behavior where DOM fallback returned many old
saved posts as candidates.

### Test Cleanup

Updated:

```text
tests/test_5x5_live.py
```

It now clears `test_output` before asserting exact file counts. This test used a
stable repo-local ignored output directory, so repeated runs had accumulated
files.

## Verification Run

Full suite:

```bash
python3 -m pytest -q
```

Latest result:

```text
269 passed, 3 warnings
```

Warnings are existing AsyncMock warnings in E2E tests.

## Real Delta Run Completed

The user manually saved 1 new item in X and 1 new item in LinkedIn.

Dry-run before real processing:

```bash
docker compose run --rm --build \
  -e HEADLESS=true \
  -e DELTA_STOP_AFTER_KNOWN=1 \
  pipeline python main.py --delta-only --dry-run --source both
```

Result:

```text
scraped=2
would_process=2
```

Real run:

```bash
docker compose run --rm --build \
  -e HEADLESS=true \
  -e DELTA_STOP_AFTER_KNOWN=1 \
  pipeline python main.py --delta-only --source both
```

Final result:

```json
{"processed":2,"enriched":2,"dead_letter":0,"new_cursor_x":"HBas2MDh85a/4zMAAA==","new_cursor_linkedin":null}
```

Created notes:

```text
volumes/llm_wiki_seed/vault/bookmarks/x/folks-when-you-write-skills-ask-your-agent-to-be-token-efficient-relax-grammer-i-see-too-many-skills-that-write-book-2058917897590673525.md
volumes/llm_wiki_seed/vault/bookmarks/linkedin/hier-ist-der-post-kraftvoll-provokant-revolutionrai-is-dead-long-live-rievery-ai-system-you-use-today-is-built-7464554351244558336.md
```

Matching JSON sidecars were also written.

Updated state:

```text
volumes/state/cursors.json
volumes/state/processed_urls.json
volumes/state/bookmark_frontiers.json
```

`bookmark_frontiers.json` now contains recent URLs for both X and LinkedIn. Do
not paste unrelated secrets from state/config files.

## Latest Follow-Up Delta Test

After the real run, the user said "prueba de nuevo". A dry-run was executed:

```bash
docker compose run --rm --build \
  -e HEADLESS=true \
  -e DELTA_STOP_AFTER_KNOWN=1 \
  pipeline python main.py --delta-only --dry-run --source both
```

Result:

```json
{
  "scraped": 1,
  "deduped": 0,
  "would_process": 1,
  "cursor_used": {
    "x": "HBas2MDh85a/4zMAAA==",
    "linkedin": null
  }
}
```

Observed:

- X returned `0` candidates and stopped on known frontier.
- LinkedIn returned `1` candidate and stopped on known frontier.

Interpretation:

- X is behaving correctly after the real run.
- LinkedIn still sees 1 post as unknown. It may be a genuinely newly saved
  LinkedIn item, or the same saved post may be surfacing with a URL shape that
  is not canonicalized to the already-written note/processed URL.

## Recommended Next Steps

1. Inspect the exact LinkedIn candidate from the last dry-run.
   - Add temporary logging of candidate URL/title before returning from
     `_dom_fallback`, or run a small script against `ScraperLinkedIn` to print
     candidates without processing.
   - Compare it to `processed_urls.json` and note frontmatter URLs.

2. If it is the same item under a different URL shape, fix LinkedIn URL
   canonicalization.
   - Likely normalize LinkedIn post URLs to `urn:li:activity:<id>` or canonical
     `/feed/update/urn:li:activity:<id>` consistently before processed/frontier
     checks and writer filenames.

3. If it is actually another new saved item, run the real delta again with:

   ```bash
   docker compose run --rm --build \
     -e HEADLESS=true \
     -e DELTA_STOP_AFTER_KNOWN=1 \
     pipeline python main.py --delta-only --source both
   ```

4. Consider changing default `DELTA_STOP_AFTER_KNOWN` from `2` to `1` only after
   deciding how conservative the daily cron should be. For manual tests, `1`
   worked well. For unattended runs, `2` is safer.

5. Obsidian setup reminder for the user:
   - Open Obsidian.
   - `Manage vaults...`
   - `Open folder as vault`
   - Select `LLM Wiki Seed/vault`.
   - Confirm `bookmarks/x` and `bookmarks/linkedin` are visible.

## Worktree Note

There are many pre-existing uncommitted changes from earlier sessions. Do not
revert them casually. New files from this session include:

```text
pipeline/dedupe.py
pipeline/frontier.py
tests/test_dedupe.py
tests/test_frontier.py
docs/handoffs/2026-05-26-delta-frontier-obsidian-handoff.md
```

Relevant modified files from this session include:

```text
main.py
config.py
scrapers/x.py
scrapers/linkedin.py
tests/test_5x5_live.py
tests/test_main.py
tests/test_scraper_x.py
tests/test_scraper_linkedin.py
```
