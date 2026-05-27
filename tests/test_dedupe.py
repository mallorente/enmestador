"""Tests for vault-level note deduplication."""

import json
from pathlib import Path

import yaml

from pipeline.dedupe import _default_backup_root, dedupe_vault_notes


def _write_note(
    path: Path,
    url: str,
    body: str,
    *,
    sidecar: dict | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frontmatter = yaml.dump({"title": path.stem, "source": path.parent.name, "url": url})
    path.write_text(f"---\n{frontmatter}---\n\n{body}", encoding="utf-8")
    if sidecar is not None:
        path.with_suffix(".json").write_text(
            json.dumps(sidecar, indent=2),
            encoding="utf-8",
        )


def test_dedupe_moves_less_rich_duplicate_with_sidecar(tmp_path: Path) -> None:
    notes_dir = tmp_path / "vault" / "bookmarks"
    backup_root = tmp_path / "dedupe_backup"
    url = "https://example.com/article?utm_source=x"
    canonical_url = "https://example.com/article"
    keep = notes_dir / "x" / "rich.md"
    duplicate = notes_dir / "linkedin" / "thin.md"

    _write_note(
        keep,
        url,
        "rich body",
        sidecar={
            "enrichment": {"tags": ["x"]},
            "content": {
                "full_text": "long extracted text",
                "external_articles": [{"url": "https://example.com/a"}],
                "image_urls": ["https://example.com/image.jpg"],
            },
        },
    )
    _write_note(
        duplicate,
        canonical_url,
        "thin",
        sidecar={"enrichment": None, "content": {"post_text": "thin"}},
    )

    result = dedupe_vault_notes(notes_dir, backup_root=backup_root)

    assert result.scanned == 2
    assert result.duplicate_groups == 1
    assert result.moved == 1
    assert keep.exists()
    assert keep.with_suffix(".json").exists()
    assert not duplicate.exists()
    assert not duplicate.with_suffix(".json").exists()

    moved = result.backup_dir / "linkedin" / "thin.md"
    assert moved.exists()
    assert moved.with_suffix(".json").exists()
    assert result.report_path.exists()
    report = json.loads(result.report_path.read_text())
    assert report["groups"][0]["kept"] == str(keep)


def test_dedupe_noops_when_urls_are_unique(tmp_path: Path) -> None:
    notes_dir = tmp_path / "vault" / "bookmarks"
    _write_note(notes_dir / "x" / "one.md", "https://example.com/one", "one")
    _write_note(notes_dir / "linkedin" / "two.md", "https://example.com/two", "two")

    result = dedupe_vault_notes(notes_dir, backup_root=tmp_path / "backup")

    assert result.scanned == 2
    assert result.duplicate_groups == 0
    assert result.moved == 0
    assert result.backup_dir is None


def test_default_backup_root_matches_vault_layout(tmp_path: Path) -> None:
    notes_dir = tmp_path / "volumes" / "llm_wiki_seed" / "vault" / "bookmarks"
    assert _default_backup_root(notes_dir) == tmp_path / "volumes" / "llm_wiki_seed" / "dedupe_backup"


def test_default_backup_root_for_custom_output_stays_near_output(tmp_path: Path) -> None:
    notes_dir = tmp_path / "test_output"
    assert _default_backup_root(notes_dir) == tmp_path / "dedupe_backup"
