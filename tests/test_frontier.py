"""Tests for delta frontier helpers."""

from pathlib import Path

from models import Source
from pipeline.frontier import BookmarkFrontierStore, KnownSequenceBoundary, collect_vault_note_urls


def test_known_sequence_boundary_matches_after_configured_streak() -> None:
    boundary = KnownSequenceBoundary(
        known_urls={"https://example.com/old-1", "https://example.com/old-2"},
        stop_after_known=2,
        recent_limit=5,
    )

    assert boundary.observe("https://example.com/new") is False
    assert boundary.observe("https://example.com/old-1") is False
    assert boundary.observe("https://example.com/old-2") is True
    assert boundary.matched is True
    assert boundary.recent_urls == [
        "https://example.com/new",
        "https://example.com/old-1",
        "https://example.com/old-2",
    ]


def test_frontier_store_roundtrips_normalized_urls(tmp_path: Path) -> None:
    store = BookmarkFrontierStore(tmp_path)

    store.save(Source.X, ["https://example.com/post?utm_source=x", "https://example.com/other/"])

    assert store.load_known(Source.X) == {
        "https://example.com/post",
        "https://example.com/other",
    }
    assert store.load_known(Source.LINKEDIN) == set()


def test_collect_vault_note_urls_reads_frontmatter(tmp_path: Path) -> None:
    notes_dir = tmp_path / "vault" / "bookmarks"
    note = notes_dir / "x" / "one.md"
    note.parent.mkdir(parents=True)
    note.write_text(
        "---\ntitle: One\nurl: https://example.com/one?utm_campaign=test\n---\n\nbody",
        encoding="utf-8",
    )

    assert collect_vault_note_urls(notes_dir) == {"https://example.com/one"}
