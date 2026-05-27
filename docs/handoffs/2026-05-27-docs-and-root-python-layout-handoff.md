# Handoff: Docs Reorg And Root Python Layout

Date: 2026-05-27
Repo: `/home/miguel/enmestador`

## Suggested Skills

- `improve-codebase-architecture`: use before moving root Python modules or reshaping package layout.
- `tdd`: use if moving imports or extracting `main.py` into a package/module.
- `diagnose`: use if scheduler, auth, or scraper behavior changes after any import/package refactor.
- `handoff`: use again after any long architecture/refactor session; keep repo handoffs in `docs/handoffs/`.

## Current Documentation State

The README has been deliberately simplified into an intro and navigation page.
Detailed docs now live in:

```text
docs/README.md
docs/operations.md
docs/llm-wiki.md
docs/recent-changes.md
```

The old long implementation plan was moved from:

```text
docs/superpowers/plans/2026-05-19-linkedin-session-longevity.md
```

to:

```text
docs/plans/2026-05-19-linkedin-session-longevity.md
```

`docs/superpowers/` was removed because it did not describe a project concept.

`docs/handoffs/` remains historical continuity, not canonical operation docs.
Use `docs/operations.md` for current operating instructions.

## Root Python File Review

There are currently five tracked Python files at repo root:

```text
config.py
healthcheck.py
main.py
models.py
scheduler.py
```

Assessment:

- `main.py`, `scheduler.py`, and `healthcheck.py` are acceptable at root because
  they are entrypoints used by Docker and humans.
- `config.py` and `models.py` are less ideal at root because they are shared
  domain/config modules, not entrypoints.
- `main.py` is also carrying too much implementation detail: CLI, orchestration,
  auth health checks, processing, cursor updates, dedupe, and helpers.

Do not move these casually. Many imports and tests currently depend on:

```python
from models import ...
from config import ...
from main import run_pipeline
```

## Recommended Future Refactor

If the project gets a package-layout cleanup, prefer a deliberate commit that
creates an `enmestador/` package and leaves root wrappers for CLI compatibility.

Target shape:

```text
enmestador/
  __init__.py
  config.py
  models.py
  app.py              # pipeline orchestration moved out of root main.py
  auth/
  scrapers/
  extractors/
  pipeline/

main.py               # thin wrapper: from enmestador.app import main
scheduler.py          # thin wrapper or moved with Docker updated
healthcheck.py        # thin wrapper or moved with Docker updated
```

Potential staged approach:

1. Move `models.py` and `config.py` into a package.
2. Update imports and tests.
3. Move orchestration from `main.py` into `enmestador/app.py` or
   `pipeline/runner.py`.
4. Keep `main.py` as a compatibility wrapper for Docker/CLI.
5. Run the full test suite and a Docker dry-run.

The deeper architectural improvement is not merely moving files. The important
module seam is a smaller orchestration interface, likely:

```python
run_pipeline(...)
```

with the implementation details behind a deeper module such as
`pipeline/runner.py` or `enmestador/app.py`.

## File Necessity Review

No tracked runtime artifacts were found. Runtime/local artifacts are ignored:

```text
volumes/
test_output/
test_venv/
test_venv_new/
__pycache__/
.pytest_cache/
```

`dedupe_backup/` was added to `.gitignore` because a root-level backup directory
exists locally from an older run and should never be committed if it later
contains files.

Files that may look redundant but should remain for now:

- `scripts/refresh_x_cookies.py`: still needed because X uses exported
  `x_cookies.txt`.
- `scripts/export_raw_bookmarks.py`: one-shot/raw export utility for LLM Wiki
  seed work.
- `auth/setup_linkedin_session.py`: legacy one-time cookie import utility. It is
  not the main path anymore, but it may still be useful for importing old
  LinkedIn cookie exports.

## Verification

Only documentation and `.gitignore` were changed in this pass.

Ran:

```text
git diff --check
```

No whitespace errors.

## Next Recommended Step

Commit the documentation reorganization and this handoff. Do not attempt the
package-layout refactor in the same commit; it should be separate and tested
with the full suite plus Docker dry-run.
