"""Tests for analysis/interest_map.py."""

import json
from pathlib import Path

import pytest

from analysis.interest_map import (
    Cluster,
    NoteRecord,
    _assign_orphans,
    _parse_clusters,
    load_notes,
    render_markdown,
)


def _write_note(dir_: Path, stem: str, title: str, tags: list[str], takeaway: str) -> None:
    dir_.mkdir(parents=True, exist_ok=True)
    payload = {
        "bookmark": {"title": title, "url": f"https://x.com/{stem}"},
        "content": {},
        "enrichment": {"tags": tags, "takeaway": takeaway},
    }
    (dir_ / f"{stem}.json").write_text(json.dumps(payload), encoding="utf-8")


class TestLoadNotes:
    def test_loads_records(self, tmp_path: Path):
        _write_note(tmp_path / "linkedin", "n1", "Rust async", ["rust"], "use tokio")
        _write_note(tmp_path / "x", "n2", "LLM agents", ["ai", "agents"], "agents win")
        notes = load_notes(tmp_path)
        assert {n.note_id for n in notes} == {"n1", "n2"}
        n1 = next(n for n in notes if n.note_id == "n1")
        assert n1.title == "Rust async"
        assert n1.tags == ["rust"]
        assert n1.takeaway == "use tokio"

    def test_skips_bad_json(self, tmp_path: Path):
        (tmp_path).mkdir(parents=True, exist_ok=True)
        (tmp_path / "bad.json").write_text("{not json", encoding="utf-8")
        _write_note(tmp_path, "ok", "Title", [], "")
        notes = load_notes(tmp_path)
        assert [n.note_id for n in notes] == ["ok"]


class TestParseClusters:
    def test_parses_array(self):
        raw = (
            'Here:\n[{"name": "AI", "emoji": "🤖", "description": "d", "note_ids": [0, 1]},'
            ' {"name": "Rust", "emoji": "🦀", "description": "d2", "note_ids": [2]}]'
        )
        clusters = _parse_clusters(raw, n_notes=3)
        assert [c.name for c in clusters] == ["AI", "Rust"]
        assert clusters[0].note_ids == [0, 1]

    def test_drops_out_of_range_ids(self):
        raw = '[{"name": "A", "emoji": "x", "description": "", "note_ids": [0, 9, -1]}]'
        clusters = _parse_clusters(raw, n_notes=2)
        assert clusters[0].note_ids == [0]

    def test_raises_without_array(self):
        with pytest.raises(ValueError):
            _parse_clusters("no json here", n_notes=1)


class TestAssignOrphans:
    def test_adds_catch_all_for_missing(self):
        clusters = [Cluster(name="A", emoji="x", description="", note_ids=[0])]
        out = _assign_orphans(clusters, n_notes=3)
        assert out[-1].name == "Other"
        assert out[-1].note_ids == [1, 2]

    def test_no_catch_all_when_complete(self):
        clusters = [Cluster(name="A", emoji="x", description="", note_ids=[0, 1])]
        out = _assign_orphans(clusters, n_notes=2)
        assert all(c.name != "Other" for c in out)


class TestRenderMarkdown:
    def test_renders_markmap_and_index(self):
        notes = [
            NoteRecord("n1", "Rust async runtimes", "u1", ["rust"], "use tokio"),
            NoteRecord("n2", "LLM agents", "u2", ["ai"], "agents win"),
        ]
        clusters = [
            Cluster("Rust", "🦀", "Systems stuff", [0]),
            Cluster("AI", "🤖", "Agents stuff", [1]),
        ]
        md = render_markdown(clusters, notes)
        assert "```markmap" in md
        assert "# My interests" in md
        assert "## 🦀 Rust" in md
        assert "## Clusters and their notes" in md
        assert "### 🤖 AI (1 notes)" in md
        assert "[[n1|Rust async runtimes]] — use tokio" in md
        assert "[[n2|LLM agents]] — agents win" in md
