"""Known-bookmark frontier helpers for delta scraping."""

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from models import Source
from pipeline.state import normalize_url


@dataclass
class KnownSequenceBoundary:
    """Track whether a scraper has reached a known saved-bookmark sequence."""

    known_urls: set[str]
    stop_after_known: int
    recent_limit: int
    recent_urls: list[str] = field(default_factory=list)
    consecutive_known: int = 0
    matched: bool = False

    def observe(self, url: str) -> bool:
        """Record one observed URL and return True when the boundary is reached."""
        normalized = normalize_url(url)
        if normalized in self.recent_urls:
            return self.matched

        self.recent_urls.append(normalized)
        if len(self.recent_urls) > self.recent_limit:
            self.recent_urls = self.recent_urls[-self.recent_limit:]

        if self.stop_after_known <= 0:
            return False

        if normalized in self.known_urls:
            self.consecutive_known += 1
            if self.consecutive_known >= self.stop_after_known:
                self.matched = True
        else:
            self.consecutive_known = 0

        return self.matched

    def is_known(self, url: str) -> bool:
        """Return True if a URL is already known from state or vault notes."""
        return normalize_url(url) in self.known_urls


class BookmarkFrontierStore:
    """Persist the latest observed saved-bookmark URLs per source."""

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self.file = state_dir / "bookmark_frontiers.json"

    def load_known(self, source: Source) -> set[str]:
        if not self.file.exists():
            return set()
        try:
            data = json.loads(self.file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return set()
        urls = data.get(source.value, {}).get("recent_urls", [])
        return {normalize_url(url) for url in urls if isinstance(url, str)}

    def save(self, source: Source, urls: list[str]) -> None:
        data: dict[str, Any] = {}
        if self.file.exists():
            try:
                data = json.loads(self.file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                data = {}
        self.state_dir.mkdir(parents=True, exist_ok=True)
        data[source.value] = {
            "updated_at": datetime.now(UTC).isoformat(),
            "recent_urls": [normalize_url(url) for url in urls],
        }
        self.file.write_text(json.dumps(data, indent=2), encoding="utf-8")


def collect_vault_note_urls(notes_dir: Path) -> set[str]:
    """Return normalized bookmark URLs already present in Markdown notes."""
    urls: set[str] = set()
    if not notes_dir.exists():
        return urls
    for path in notes_dir.rglob("*.md"):
        frontmatter = _read_frontmatter(path)
        url = frontmatter.get("url")
        if isinstance(url, str) and url.strip():
            urls.add(normalize_url(url))
    return urls


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
