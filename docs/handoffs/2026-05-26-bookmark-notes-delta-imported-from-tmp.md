# Imported Handoff: Bookmark Notes + Delta Run

Date: 2026-05-26

This preserves the useful content from the `/tmp/enmestador-handoff-2026-05-26.md`
handoff that was read earlier in the session. The original `/tmp` file is no
longer present, so future sessions should use this repo-local copy.

## Original Next Session Goal

Change the pipeline/export flow so bookmarks are written as separate Markdown
notes by default, then run a test execution that processes only new bookmarks.

The user noticed the synced root contains aggregate files (`all_bookmarks.json`,
JSONL, raw folders) and wants the actual usable notes as separate `.md` files.
A clean Obsidian view had been created manually at:

```text
volumes/llm_wiki_seed/vault/
```

The follow-up work was to make that behavior first-class.

## State Captured In Original Handoff

Fresh bookmark scrape/export had completed:

- X: `468` fresh bookmarks
- LinkedIn: `79` fresh bookmarks
- Combined: `547`

Main seed path:

```text
/home/miguel/enmestador/volumes/llm_wiki_seed
```

Clean Obsidian-facing path:

```text
/home/miguel/enmestador/volumes/llm_wiki_seed/vault
```

Expected note locations:

```text
vault/bookmarks/x/
vault/bookmarks/linkedin/
```

Syncthing shares the parent folder:

- Host path: `/home/miguel/enmestador/volumes/llm_wiki_seed`
- Container path: `/data/llm_wiki_seed`
- Folder label: `LLM Wiki Seed`

The user should open `LLM Wiki Seed/vault` as the Obsidian vault, not the raw
sync root.

## Original Recommended Work

1. Inspect writer/export behavior:
   - `pipeline/writer.py`
   - `scripts/export_raw_bookmarks.py`
   - `main.py`
   - writer/model/scraper tests
2. Make per-bookmark Markdown first-class under:

   ```text
   volumes/llm_wiki_seed/
     vault/
       bookmarks/
         x/
         linkedin/
   ```

3. Keep raw machine-readable exports separate, e.g.:

   ```text
   volumes/llm_wiki_seed/raw/YYYY-MM-DD/
   ```

4. Avoid appending multiple bookmarks to one `.md` on filename collision.
5. Prefer deterministic unique filenames including source ID/status ID or a
   stable hash.
6. Run a delta/new-only test using existing state only after confirming
   `volumes/state`.

## Useful Context

- `main.py --fresh-run` skips dedup and uses bootstrap.
- Normal `python3 main.py` uses cursors/processed state and should only process
  new bookmarks.
- `volumes/syncthing_config/config.xml` contains a Syncthing API key; do not
  paste it into public docs.

