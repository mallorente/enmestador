"""Tests for analysis/wiki.py."""

import json
from pathlib import Path

from analysis.wiki import (
    WikiNote,
    build_wiki,
    consolidate_fields,
    load_wiki_notes,
    render_field_page,
)

CANONICAL = ["Home Automation", "Personal Agents", "Legal Tech", "Other Tech"]


def _note(field: str, title: str = "T") -> WikiNote:
    return WikiNote(
        note_id="n", title=title, url="https://x/1", source="linkedin",
        field=field, tags=["t"], takeaway="tk", summary_bullets=["a", "b", "c"],
        repo_evaluation=None,
    )


class TestConsolidate:
    def test_keeps_canonical(self):
        notes = [_note("Legal Tech"), _note("Personal Agents")]
        groups = consolidate_fields(notes, CANONICAL)
        assert set(groups) == {"Legal Tech", "Personal Agents"}

    def test_folds_singleton_non_canonical_into_catch_all(self):
        notes = [_note("Legal Tech"), _note("rag-infra")]
        groups = consolidate_fields(notes, CANONICAL)
        assert "rag-infra" not in groups
        assert any(n.field == "rag-infra" for n in groups["Other Tech"])

    def test_keeps_emergent_field_with_enough_notes(self):
        notes = [_note("Robotics"), _note("Robotics")]
        groups = consolidate_fields(notes, CANONICAL, min_emergent=2)
        assert "Robotics" in groups


class TestRenderAndBuild:
    def test_render_field_page(self):
        page = render_field_page("Legal Tech", [_note("Legal Tech", "My Note")])
        assert "# Legal Tech" in page
        assert "## [My Note](https://x/1)" in page
        assert "**Takeaway:** tk" in page

    def test_build_wiki_writes_index_and_pages(self, tmp_path: Path):
        bm = tmp_path / "bookmarks" / "linkedin"
        bm.mkdir(parents=True)
        (bm / "a.json").write_text(json.dumps({
            "bookmark": {"title": "A", "url": "https://x/a", "source": "linkedin"},
            "content": {},
            "enrichment": {"field": "Legal Tech", "tags": ["t"], "takeaway": "tk",
                           "summary_bullets": ["a", "b", "c"]},
        }), encoding="utf-8")
        out = tmp_path / "wiki"
        index = build_wiki(tmp_path / "bookmarks", out, CANONICAL)
        assert index.exists()
        assert (out / "legal-tech.md").exists()
        assert "Legal Tech" in index.read_text(encoding="utf-8")

    def test_load_wiki_notes_reads_repo_links(self, tmp_path: Path):
        bm = tmp_path / "techscout"
        bm.mkdir(parents=True)
        (bm / "r.json").write_text(json.dumps({
            "bookmark": {"title": "R", "url": "https://github.com/o/r", "source": "techscout"},
            "content": {"repo_analyses": [{"url": "https://github.com/o/r", "full_name": "o/r"}]},
            "enrichment": {"field": "Other Tech", "tags": [], "takeaway": "",
                           "summary_bullets": [], "repo_evaluation": "good"},
        }), encoding="utf-8")
        notes = load_wiki_notes(tmp_path)
        assert notes[0].repo_links == ["https://github.com/o/r"]
        assert notes[0].repo_evaluation == "good"
