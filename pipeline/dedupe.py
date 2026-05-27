"""Vault-level Markdown note deduplication.

Deduplicates bookmark notes by canonical URL, keeps the richest note, and moves
duplicate Markdown/JSON sidecars into a timestamped backup directory.
"""

import json
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from pipeline.state import normalize_url


@dataclass(frozen=True)
class DedupeResult:
    """Summary of a dedupe pass."""

    scanned: int
    duplicate_groups: int
    moved: int
    backup_dir: Path | None
    report_path: Path | None


@dataclass(frozen=True)
class _NoteCandidate:
    path: Path
    url: str
    normalized_url: str
    score: tuple[int, int, int, int, int]


def dedupe_vault_notes(notes_dir: Path, backup_root: Path | None = None) -> DedupeResult:
    """Move duplicate bookmark notes out of the vault.

    Duplicate groups are built from the note frontmatter `url`. The kept note is
    selected by a transparent richness score:

    1. Has enrichment.
    2. Has JSON sidecar.
    3. Extracted text/article/image richness from sidecar.
    4. Markdown file size.
    5. Newest modified time as final tie-breaker.
    """
    if not notes_dir.exists():
        return DedupeResult(
            scanned=0,
            duplicate_groups=0,
            moved=0,
            backup_dir=None,
            report_path=None,
        )

    candidates = _collect_candidates(notes_dir)
    groups: dict[str, list[_NoteCandidate]] = {}
    for candidate in candidates:
        groups.setdefault(candidate.normalized_url, []).append(candidate)

    duplicate_groups = {url: notes for url, notes in groups.items() if len(notes) > 1}
    if not duplicate_groups:
        return DedupeResult(
            scanned=len(candidates),
            duplicate_groups=0,
            moved=0,
            backup_dir=None,
            report_path=None,
        )

    if backup_root is None:
        backup_root = _default_backup_root(notes_dir)
    backup_dir = backup_root / datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    backup_dir.mkdir(parents=True, exist_ok=True)

    report: dict[str, Any] = {
        "deduped_at": datetime.now(UTC).isoformat(),
        "notes_dir": str(notes_dir),
        "backup_dir": str(backup_dir),
        "groups": [],
    }
    moved = 0

    for normalized_url, notes in sorted(duplicate_groups.items()):
        keep = max(notes, key=lambda candidate: candidate.score)
        losers = [candidate for candidate in notes if candidate.path != keep.path]
        group_report = {
            "url": normalized_url,
            "kept": str(keep.path),
            "kept_score": keep.score,
            "moved": [],
        }
        for loser in losers:
            moved_path = _move_note_with_sidecar(loser.path, notes_dir, backup_dir)
            group_report["moved"].append({
                "from": str(loser.path),
                "to": str(moved_path),
                "score": loser.score,
            })
            moved += 1
        report["groups"].append(group_report)

    report_path = backup_dir / "dedupe_report.json"
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    return DedupeResult(
        scanned=len(candidates),
        duplicate_groups=len(duplicate_groups),
        moved=moved,
        backup_dir=backup_dir,
        report_path=report_path,
    )


def _collect_candidates(notes_dir: Path) -> list[_NoteCandidate]:
    candidates: list[_NoteCandidate] = []
    for path in sorted(notes_dir.rglob("*.md")):
        frontmatter = _read_frontmatter(path)
        url = frontmatter.get("url")
        if not isinstance(url, str) or not url.strip():
            continue
        candidates.append(
            _NoteCandidate(
                path=path,
                url=url,
                normalized_url=normalize_url(url),
                score=_score_note(path),
            )
        )
    return candidates


def _default_backup_root(notes_dir: Path) -> Path:
    """Return the default backup root for a notes directory.

    Production notes live at `.../vault/bookmarks`, where backups belong beside
    `vault`. For arbitrary test/custom output directories, keep backups beside
    the notes directory instead of walking above the workspace.
    """
    if notes_dir.name == "bookmarks" and notes_dir.parent.name == "vault":
        return notes_dir.parent.parent / "dedupe_backup"
    return notes_dir.parent / "dedupe_backup"


def _read_frontmatter(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return {}
    if not text.startswith("---\n"):
        return {}
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}
    data = yaml.safe_load(parts[1]) or {}
    return data if isinstance(data, dict) else {}


def _score_note(path: Path) -> tuple[int, int, int, int, int]:
    sidecar = path.with_suffix(".json")
    has_sidecar = int(sidecar.exists())
    markdown_size = path.stat().st_size
    modified = int(path.stat().st_mtime)
    has_enrichment = 0
    content_richness = 0

    if sidecar.exists():
        try:
            data = json.loads(sidecar.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            data = {}
        enrichment = data.get("enrichment") if isinstance(data, dict) else None
        has_enrichment = int(bool(enrichment))
        content = data.get("content") if isinstance(data, dict) else {}
        if isinstance(content, dict):
            content_richness += len(content.get("full_text") or "")
            content_richness += len(content.get("post_text") or "")
            content_richness += 500 * len(content.get("external_articles") or [])
            content_richness += 100 * len(content.get("image_urls") or [])

    return (has_enrichment, has_sidecar, content_richness, markdown_size, modified)


def _move_note_with_sidecar(path: Path, notes_dir: Path, backup_dir: Path) -> Path:
    relative = path.relative_to(notes_dir)
    destination = backup_dir / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(path), str(destination))

    sidecar = path.with_suffix(".json")
    if sidecar.exists():
        sidecar_destination = destination.with_suffix(".json")
        shutil.move(str(sidecar), str(sidecar_destination))

    return destination
