"""Writer module for the PKM ingestion pipeline.

Writes enriched bookmarks as Markdown files with YAML frontmatter
organized into subdirectories by source. Handles filename sanitization
and collision detection (append-on-collision within same source).
"""

import re
from pathlib import Path

import yaml

from models import EnrichedBookmark

# Characters safe for filenames (ASCII alphanumeric, hyphen, underscore, space)
_SAFE_FILENAME_RE = re.compile(r"[^a-zA-Z0-9 _-]")

# YAML frontmatter keys in order
_FRONTMATTER_KEYS = ["title", "source", "url", "saved", "tags", "model", "external_urls"]

# Separator for collision appends
_COLLAPSE_SEPARATOR = "\n\n---\n\n"


def _sanitize_filename(title: str) -> str:
    """Convert a title to a filesystem-safe filename.

    - Lowercase
    - Replace spaces/underscores with hyphens
    - Remove non-ASCII-safe characters
    - Collapse multiple hyphens
    - Trim leading/trailing hyphens
    - Append .md extension
    """
    name = title.lower()
    name = _SAFE_FILENAME_RE.sub("", name)
    name = re.sub(r"[\s_]+", "-", name)
    name = re.sub(r"-{2,}", "-", name)
    name = name.strip("-")
    if not name:
        name = "untitled"
    return name + ".md"


def _build_frontmatter(enriched: EnrichedBookmark) -> str:
    """Build YAML frontmatter from an enriched bookmark."""
    bookmark = enriched.bookmark
    fm: dict[str, str | list[str]] = {
        "title": bookmark.title,
        "source": bookmark.source.value,
        "url": str(bookmark.url),
    }
    if bookmark.saved_at:
        fm["saved"] = bookmark.saved_at.isoformat()
    if enriched.enrichment:
        fm["tags"] = enriched.enrichment.tags
        fm["model"] = enriched.enrichment.model_used
    else:
        fm["tags"] = []
        fm["model"] = "none"
    if bookmark.external_urls:
        fm["external_urls"] = bookmark.external_urls

    return yaml.dump(fm, default_flow_style=False, sort_keys=False, allow_unicode=True)


def _build_body(enriched: EnrichedBookmark) -> str:
    """Build the Markdown body from an enriched bookmark."""
    lines: list[str] = []
    content = enriched.content

    if enriched.enrichment:
        lines.append("## Summary\n")
        for bullet in enriched.enrichment.summary_bullets:
            lines.append(f"- {bullet}")
        lines.append("")
        lines.append(f"## Takeaway\n\n{enriched.enrichment.takeaway}\n")

    # Original post text (always shown)
    if content.post_text:
        lines.append("## Original\n")
        lines.append(content.post_text)
        lines.append("")

    # External article text (from web extraction, NOT from X.com error pages)
    if content.full_text and content.extraction_method == "trafilatura":
        lines.append("## Article Text\n")
        lines.append(content.full_text)
        lines.append("")

    # External articles (clipped content from tweet links)
    if content.external_articles:
        for article in content.external_articles:
            lines.append(f"## Article: {article.url}\n")
            if article.text:
                lines.append(article.text[:8000])
                lines.append("")
            else:
                lines.append("(Extraction failed — see Links section)\n")

    # External links section (for tweets with links to external content)
    # Shown only when there are no extracted articles to avoid duplication
    elif enriched.bookmark.external_urls:
        lines.append("## Links\n")
        for url in enriched.bookmark.external_urls:
            lines.append(f"- {url}")
        lines.append("")

    return "\n".join(lines)


class Writer:
    """Write enriched bookmarks as Obsidian-ready Markdown files."""

    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir

    def write(self, enriched: EnrichedBookmark) -> Path:
        """Write an enriched bookmark to a .md file.

        Returns the path to the written file.
        Appends to existing file on collision within same source.
        """
        source = enriched.bookmark.source.value
        source_dir = self.output_dir / source
        source_dir.mkdir(parents=True, exist_ok=True)
        filename = _sanitize_filename(enriched.bookmark.title)
        filepath = source_dir / filename

        frontmatter = _build_frontmatter(enriched)
        body = _build_body(enriched)
        content = f"---\n{frontmatter}---\n\n{body}"

        if filepath.exists():
            # Collision: append with separator
            existing = filepath.read_text(encoding="utf-8")
            new_content = f"{existing}{_COLLAPSE_SEPARATOR}{content}"
            filepath.write_text(new_content, encoding="utf-8")
        else:
            filepath.write_text(content, encoding="utf-8")

        return filepath
