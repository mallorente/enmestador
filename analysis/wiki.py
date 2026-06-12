"""Build the field-organized wiki from the enriched vault.

This is the *trusted writer* in the anti-prompt-injection design: it consumes
ONLY the structured fields the isolated reader produced (summary, takeaway,
tags, field, repo evaluation) and renders Markdown deterministically. Raw,
untrusted post/README text never reaches this stage, so an injection in the
source can at worst produce a slightly off summary — never an action.
"""

import json
import logging
import re
from dataclasses import dataclass, field as dc_field
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class WikiNote:
    note_id: str
    title: str
    url: str
    source: str
    field: str
    tags: list[str]
    takeaway: str
    summary_bullets: list[str]
    repo_evaluation: str | None
    repo_links: list[str] = dc_field(default_factory=list)


def _slug(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-") or "campo"


def load_wiki_notes(bookmarks_dir: Path) -> list[WikiNote]:
    """Load structured WikiNotes from every .json sidecar (untrusted text excluded)."""
    notes: list[WikiNote] = []
    for path in sorted(bookmarks_dir.rglob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        bookmark = data.get("bookmark") or {}
        enrichment = data.get("enrichment") or {}
        content = data.get("content") or {}
        repo_links = [r.get("url") for r in (content.get("repo_analyses") or []) if r.get("url")]
        notes.append(WikiNote(
            note_id=path.stem,
            title=" ".join((bookmark.get("title") or path.stem).split()),
            url=bookmark.get("url") or "",
            source=bookmark.get("source") or "",
            field=(enrichment.get("field") or "").strip() or "Unclassified",
            tags=[str(t) for t in (enrichment.get("tags") or [])],
            takeaway=enrichment.get("takeaway") or "",
            summary_bullets=[str(b) for b in (enrichment.get("summary_bullets") or [])],
            repo_evaluation=enrichment.get("repo_evaluation"),
            repo_links=repo_links,
        ))
    return notes


def _catch_all(canonical: list[str]) -> str:
    for c in canonical:
        if "otros" in c.lower() or "other" in c.lower():
            return c
    return canonical[-1] if canonical else "Other"


def consolidate_fields(
    notes: list[WikiNote], canonical: list[str], min_emergent: int = 2
) -> dict[str, list[WikiNote]]:
    """Group notes by field; fold rare non-canonical fields into the catch-all.

    Canonical fields are always kept. A model-proposed field survives only if it
    has at least ``min_emergent`` notes; otherwise its notes move to the catch-all.
    """
    catch_all = _catch_all(canonical)
    counts: dict[str, int] = {}
    for n in notes:
        counts[n.field] = counts.get(n.field, 0) + 1

    groups: dict[str, list[WikiNote]] = {c: [] for c in canonical}
    for n in notes:
        target = n.field
        if target not in canonical and counts[target] < min_emergent:
            target = catch_all
        groups.setdefault(target, []).append(n)
    return {f: ns for f, ns in groups.items() if ns}


def render_field_page(field_name: str, notes: list[WikiNote]) -> str:
    out = [f"# {field_name}", "", f"> {len(notes)} notes.", ""]
    for n in sorted(notes, key=lambda x: x.title.lower()):
        link = f"[{n.title}]({n.url})" if n.url else n.title
        out.append(f"## {link}")
        out.append(f"*source: {n.source}*")
        out.append("")
        for b in n.summary_bullets:
            out.append(f"- {b}")
        if n.takeaway:
            out.append(f"\n**Takeaway:** {n.takeaway}")
        if n.tags:
            out.append(f"\n**Tags:** {', '.join(n.tags)}")
        if n.repo_evaluation:
            repos = ", ".join(n.repo_links) if n.repo_links else ""
            out.append(f"\n**Repo{' (' + repos + ')' if repos else ''}:** {n.repo_evaluation}")
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def render_index(groups: dict[str, list[WikiNote]]) -> str:
    total = sum(len(ns) for ns in groups.values())
    out = [
        "# Wiki — Knowledge map by field",
        "",
        f"> {total} notes in {len(groups)} fields. "
        "Each note is filed under the life-area it is most useful for.",
        "",
    ]
    for fname in sorted(groups, key=lambda f: -len(groups[f])):
        out.append(f"- [{fname}](./{_slug(fname)}.md) — {len(groups[fname])} notes")
    return "\n".join(out) + "\n"


def build_wiki(bookmarks_dir: Path, out_dir: Path, canonical: list[str]) -> Path:
    """Write the field-organized wiki to out_dir. Returns out_dir/index.md."""
    notes = load_wiki_notes(bookmarks_dir)
    if not notes:
        raise ValueError(f"No notes found under {bookmarks_dir}")
    groups = consolidate_fields(notes, canonical)

    out_dir.mkdir(parents=True, exist_ok=True)
    for fname, fnotes in groups.items():
        (out_dir / f"{_slug(fname)}.md").write_text(
            render_field_page(fname, fnotes), encoding="utf-8"
        )
    index = out_dir / "index.md"
    index.write_text(render_index(groups), encoding="utf-8")
    logger.info("Wrote wiki (%d notes, %d fields) to %s", len(notes), len(groups), out_dir)
    return index
