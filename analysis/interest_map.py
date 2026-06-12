"""Generate an "interest map" from the enriched vault.

Reads the JSON sidecars enmestador writes, clusters the notes by theme with the
LLM, and renders a single Markdown file with a Markmap mind-map plus a per-cluster
index that links back to each note. Open the file in Obsidian (with the Markmap
plugin) to get the radial map of your interests.
"""

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from pipeline.llm import LLMProcessor

logger = logging.getLogger(__name__)

TITLE_MAX = 70


@dataclass
class NoteRecord:
    """One enriched note, distilled to what the map needs."""

    note_id: str  # filename stem → used for Obsidian [[wiki-links]]
    title: str
    url: str
    tags: list[str]
    takeaway: str


@dataclass
class Cluster:
    """A thematic group of notes."""

    name: str
    emoji: str
    description: str
    note_ids: list[int] = field(default_factory=list)  # indices into the notes list


def _short_title(title: str) -> str:
    title = " ".join(title.split())
    return title[:TITLE_MAX] + "…" if len(title) > TITLE_MAX else title


def load_notes(bookmarks_dir: Path) -> list[NoteRecord]:
    """Load NoteRecords from every .json sidecar under the bookmarks directory."""
    notes: list[NoteRecord] = []
    for path in sorted(bookmarks_dir.rglob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            logger.warning("Skipping unreadable note: %s", path)
            continue
        bookmark = data.get("bookmark") or {}
        enrichment = data.get("enrichment") or {}
        title = bookmark.get("title") or path.stem
        notes.append(NoteRecord(
            note_id=path.stem,
            title=" ".join(title.split()),
            url=bookmark.get("url") or "",
            tags=[str(t) for t in (enrichment.get("tags") or [])],
            takeaway=enrichment.get("takeaway") or "",
        ))
    return notes


def _build_cluster_prompt(notes: list[NoteRecord]) -> str:
    lines = [
        "You organize a personal knowledge base into a map of interests.",
        "Below is a numbered list of notes (title, tags, takeaway).",
        "Group them into 4-12 coherent thematic clusters. Every note must belong",
        "to exactly one cluster. Name each cluster in the user's apparent language.",
        "",
        "Respond with ONLY a JSON array (no prose, no fences). Each element:",
        '  {"name": str, "emoji": one emoji, "description": one sentence,'
        ' "note_ids": [int, ...]}',
        "where note_ids are the indices of the notes in that cluster.",
        "",
        "Notes:",
    ]
    for i, n in enumerate(notes):
        tags = ", ".join(n.tags) if n.tags else "—"
        lines.append(f"[{i}] {n.title} | tags: {tags} | {n.takeaway}")
    return "\n".join(lines)


def _parse_clusters(raw: str, n_notes: int) -> list[Cluster]:
    start = raw.find("[")
    end = raw.rfind("]")
    if start == -1 or end <= start:
        raise ValueError("No JSON array found in clustering response")
    items = json.loads(raw[start : end + 1])
    clusters: list[Cluster] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        ids = [i for i in item.get("note_ids", []) if isinstance(i, int) and 0 <= i < n_notes]
        if not ids:
            continue
        clusters.append(Cluster(
            name=str(item.get("name") or "Untitled").strip(),
            emoji=str(item.get("emoji") or "📌").strip(),
            description=str(item.get("description") or "").strip(),
            note_ids=ids,
        ))
    return clusters


def _assign_orphans(clusters: list[Cluster], n_notes: int) -> list[Cluster]:
    """Put any note the LLM dropped into a catch-all cluster."""
    seen = {i for c in clusters for i in c.note_ids}
    missing = [i for i in range(n_notes) if i not in seen]
    if missing:
        clusters.append(Cluster(
            name="Other", emoji="🗂️",
            description="Notes without a clear theme.",
            note_ids=missing,
        ))
    return clusters


def _markmap_outline(clusters: list[Cluster], notes: list[NoteRecord]) -> str:
    """The Markmap markdown outline shared by the Markdown and HTML renderers."""
    lines = ["# My interests"]
    for c in clusters:
        lines.append(f"## {c.emoji} {c.name}")
        for i in c.note_ids:
            lines.append(f"- {_short_title(notes[i].title)}")
    return "\n".join(lines)


def render_markdown(clusters: list[Cluster], notes: list[NoteRecord]) -> str:
    """Render the Markmap mind-map plus the per-cluster index."""
    out: list[str] = [
        "# Interest map",
        "",
        f"> {len(notes)} notes in {len(clusters)} clusters. "
        "Open this file in Obsidian with the Markmap plugin to see the map.",
        "",
        "```markmap",
        _markmap_outline(clusters, notes),
        "```",
        "",
    ]
    out.append("## Clusters and their notes")
    out.append("")
    for c in clusters:
        out.append(f"### {c.emoji} {c.name} ({len(c.note_ids)} notes)")
        if c.description:
            out.append(c.description)
        out.append("")
        for i in c.note_ids:
            n = notes[i]
            line = f"- [[{n.note_id}|{n.title}]]"
            if n.takeaway:
                line += f" — {n.takeaway}"
            out.append(line)
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def render_html(clusters: list[Cluster], notes: list[NoteRecord]) -> str:
    """Render a self-contained HTML page: explanation + radial Markmap graph."""
    outline = _markmap_outline(clusters, notes)
    index_sections = []
    for c in clusters:
        items = "\n".join(
            f"<li><b>{_short_title(notes[i].title)}</b>"
            f"<span>{notes[i].takeaway}</span></li>"
            for i in c.note_ids
        )
        index_sections.append(
            f"<section><h3>{c.emoji} {c.name} "
            f"<em>({len(c.note_ids)} notes)</em></h3>"
            f"<p class='desc'>{c.description}</p><ul>{items}</ul></section>"
        )
    return _HTML_TEMPLATE.format(
        n_notes=len(notes),
        n_clusters=len(clusters),
        outline=outline,
        index="\n".join(index_sections),
    )


async def cluster_notes(notes: list[NoteRecord], llm: LLMProcessor) -> list[Cluster]:
    """Cluster notes via the LLM and ensure every note lands in a cluster."""
    raw = await llm.complete(_build_cluster_prompt(notes))
    return _assign_orphans(_parse_clusters(raw, len(notes)), len(notes))


async def generate_interest_map(
    bookmarks_dir: Path,
    output_path: Path,
    llm: LLMProcessor | None = None,
    html_path: Path | None = None,
) -> Path:
    """Build the interest map (Markdown, and optionally HTML). Returns output_path."""
    notes = load_notes(bookmarks_dir)
    if not notes:
        raise ValueError(f"No notes found under {bookmarks_dir}")

    llm = llm or LLMProcessor()
    clusters = await cluster_notes(notes, llm)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(render_markdown(clusters, notes), encoding="utf-8")
    logger.info("Wrote interest map (%d notes, %d clusters) to %s",
                len(notes), len(clusters), output_path)

    if html_path is not None:
        html_path.parent.mkdir(parents=True, exist_ok=True)
        html_path.write_text(render_html(clusters, notes), encoding="utf-8")
        logger.info("Wrote interest map HTML to %s", html_path)
    return output_path


_HTML_TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Interest map — enmestador</title>
<style>
  :root {{ color-scheme: dark; }}
  body {{ margin: 0; font-family: -apple-system, system-ui, sans-serif;
         background: #0d1117; color: #e6edf3; }}
  header {{ padding: 2rem 2rem 1rem; max-width: 60rem; margin: 0 auto; }}
  h1 {{ margin: 0 0 .3rem; font-size: 1.8rem; }}
  header p {{ color: #9da7b3; line-height: 1.5; }}
  .map {{ height: 78vh; border-top: 1px solid #21262d; border-bottom: 1px solid #21262d;
         background: #0d1117; }}
  .map > svg {{ width: 100%; height: 100%; display: block; }}
  .clusters {{ max-width: 60rem; margin: 0 auto; padding: 1.5rem 2rem 4rem; }}
  .clusters section {{ margin-bottom: 1.5rem; }}
  .clusters h3 {{ margin: 0 0 .2rem; }}
  .clusters em {{ color: #7d8590; font-style: normal; font-weight: normal; }}
  .desc {{ color: #9da7b3; margin: 0 0 .6rem; }}
  .clusters ul {{ list-style: none; padding: 0; margin: 0; }}
  .clusters li {{ padding: .4rem 0; border-bottom: 1px solid #161b22; }}
  .clusters li span {{ display: block; color: #7d8590; font-size: .9rem; }}
</style></head>
<body>
<header>
  <h1>enmestador · Interest map</h1>
  <p>An overview of your enriched vault: {n_notes} notes grouped into {n_clusters}
  thematic clusters from their titles, tags and takeaways. The graph is
  interactive — drag, zoom and fold branches.</p>
</header>
<div class="map markmap"><script type="text/template">
{outline}
</script></div>
<div class="clusters">
<h2>Clusters and their notes</h2>
{index}
</div>
<script src="https://cdn.jsdelivr.net/npm/markmap-autoloader@0.18"></script>
</body></html>
"""
