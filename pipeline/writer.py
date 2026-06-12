"""Writer module for the PKM ingestion pipeline.

Writes enriched bookmarks as Markdown files with YAML frontmatter and JSON
sidecars organized into subdirectories by source. Handles filename sanitization
and collision detection.
"""

import re
from hashlib import sha1
from pathlib import Path

import yaml

from models import Bookmark, EnrichedBookmark, Source

# Characters safe for filenames (ASCII alphanumeric, hyphen, underscore, space)
_SAFE_FILENAME_RE = re.compile(r"[^a-zA-Z0-9 _-]")

# YAML frontmatter keys in order
_FRONTMATTER_KEYS = [
    "title",
    "source",
    "url",
    "author",
    "published",
    "saved",
    "retrieved",
    "tags",
    "model",
    "external_urls",
    "referenced_tweet_urls",
    "image_urls",
]

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


def _bookmark_id(bookmark: Bookmark) -> str:
    """Return a stable, filename-safe identifier for one source bookmark."""
    url = str(bookmark.url).rstrip("/")
    if bookmark.source == Source.X:
        match = re.search(r"/status/(\d+)", url)
        if match:
            return match.group(1)
    if bookmark.source == Source.LINKEDIN:
        match = re.search(r"(?:activity:|activity-)(\d+)", url)
        if match:
            return match.group(1)

    return sha1(url.encode("utf-8")).hexdigest()[:10]


def _bookmark_filename(bookmark: Bookmark) -> str:
    """Build the Markdown filename for a bookmark note."""
    stem = _sanitize_filename(bookmark.title).removesuffix(".md")
    return f"{stem}-{_bookmark_id(bookmark)}.md"


def _build_frontmatter(enriched: EnrichedBookmark) -> str:
    """Build YAML frontmatter from an enriched bookmark."""
    bookmark = enriched.bookmark
    fm: dict[str, str | list[str]] = {
        "title": bookmark.title,
        "source": bookmark.source.value,
        "url": str(bookmark.url),
    }
    if bookmark.author:
        fm["author"] = bookmark.author
    if bookmark.published_at:
        fm["published"] = bookmark.published_at.isoformat()
    if bookmark.saved_at:
        fm["saved"] = bookmark.saved_at.isoformat()
    fm["retrieved"] = bookmark.retrieved_at.isoformat()
    if enriched.enrichment:
        fm["tags"] = enriched.enrichment.tags
        fm["model"] = enriched.enrichment.model_used
    else:
        fm["tags"] = []
        fm["model"] = "none"
    if bookmark.external_urls:
        fm["external_urls"] = bookmark.external_urls
    if bookmark.referenced_tweet_urls:
        fm["referenced_tweet_urls"] = bookmark.referenced_tweet_urls
    image_urls = bookmark.image_urls or enriched.content.image_urls
    if image_urls:
        fm["image_urls"] = image_urls

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

    # Referenced GitHub repos: factual metadata + the LLM's evaluation.
    if content.repo_analyses:
        lines.append("## Referenced repos\n")
        for repo in content.repo_analyses:
            meta = []
            if repo.stars is not None:
                meta.append(f"★{repo.stars}")
            if repo.language:
                meta.append(repo.language)
            suffix = f" — {' · '.join(meta)}" if meta else ""
            lines.append(f"### [{repo.full_name}]({repo.url}){suffix}")
            if repo.description:
                lines.append(f"> {repo.description}")
            if repo.topics:
                lines.append(f"*topics: {', '.join(repo.topics)}*")
            lines.append("")
        evaluation = enriched.enrichment.repo_evaluation if enriched.enrichment else None
        if evaluation:
            lines.append(f"**Evaluation:** {evaluation}\n")

    # Author's first comment (often where they drop the repo link)
    if enriched.bookmark.author_comment:
        lines.append("## Author comment\n")
        lines.append(enriched.bookmark.author_comment)
        lines.append("")

    # Original post text (always shown)
    if content.post_text:
        lines.append("## Original\n")
        lines.append(content.post_text)
        lines.append("")

    # Full extracted post/thread/article text.
    if content.full_text and content.full_text.strip() != (content.post_text or "").strip():
        if content.extraction_method == "playwright_x_thread":
            heading = "## Thread"
        elif content.extraction_method in {"trafilatura", "playwright", "playwright_linkedin"}:
            heading = "## Article Text"
        else:
            heading = "## Extracted Text"
        lines.append(f"{heading}\n")
        lines.append(content.full_text)
        lines.append("")

    image_urls = enriched.bookmark.image_urls or content.image_urls
    if image_urls:
        lines.append("## Images\n")
        for url in image_urls:
            lines.append(f"![]({url})")
        lines.append("")

    # External articles (clipped content from tweet links)
    if content.external_articles:
        for article in content.external_articles:
            if article.extraction_method == "playwright_x_thread":
                heading = "Referenced Tweet"
            else:
                heading = "Article"
            lines.append(f"## {heading}: {article.url}\n")
            if article.text:
                lines.append(article.text[:8000])
                lines.append("")
                if article.image_urls:
                    lines.append("### Images\n")
                    for url in article.image_urls:
                        lines.append(f"![]({url})")
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
    """Write enriched bookmarks as Obsidian-ready Markdown plus JSON sidecars."""

    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir

    def write(self, enriched: EnrichedBookmark) -> Path:
        """Write an enriched bookmark to a .md file and a matching .json file.

        Returns the path to the written file.
        Each source bookmark gets its own deterministic note file.
        """
        source = enriched.bookmark.source.value
        source_dir = self.output_dir / source
        source_dir.mkdir(parents=True, exist_ok=True)
        filename = _bookmark_filename(enriched.bookmark)
        filepath = source_dir / filename

        frontmatter = _build_frontmatter(enriched)
        body = _build_body(enriched)
        content = f"---\n{frontmatter}---\n\n{body}"

        filepath.write_text(content, encoding="utf-8")

        json_path = filepath.with_suffix(".json")
        json_path.write_text(enriched.model_dump_json(indent=2), encoding="utf-8")

        return filepath
