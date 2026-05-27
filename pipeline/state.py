"""State management for the PKM ingestion pipeline.

Handles cursor persistence, processed URL tracking, dead-letter logging,
and pipeline lock file with atomic writes and corruption recovery.
"""

import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
import re
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse, unquote

from config import LOCK_STALE_HOURS
from models import DeadLetter, Source

TRACKING_PARAMS = frozenset({
    "utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term",
    "fbclid", "gclid", "irclickid", "ref", "ref_src", "ref_url", "source",
})

LOCK_STALE_SECONDS = int(LOCK_STALE_HOURS * 60 * 60)


def normalize_url(url: str) -> str:
    parsed = urlparse(url)
    linkedin_url = _normalize_linkedin_url(url, parsed)
    if linkedin_url:
        return linkedin_url

    path = parsed.path.rstrip("/")
    if path == "":
        path = "/"
    query_pairs = parse_qs(parsed.query, keep_blank_values=True)
    filtered = {k: v for k, v in query_pairs.items() if k not in TRACKING_PARAMS}
    new_query = urlencode(filtered, doseq=True)
    return urlunparse((
        parsed.scheme,
        parsed.hostname.lower() if parsed.hostname else parsed.netloc.lower(),
        path,
        parsed.params,
        new_query,
        "",
    ))


def _normalize_linkedin_url(url: str, parsed) -> str | None:
    """Canonicalize LinkedIn post/article URL variants to a stable update URL."""
    hostname = (parsed.hostname or parsed.netloc).lower()
    if hostname not in {"linkedin.com", "www.linkedin.com"}:
        return None

    decoded_url = unquote(url)
    urn_match = re.search(r"urn:li:(activity|article):(\d+)", decoded_url)
    if urn_match:
        return (
            "https://www.linkedin.com/feed/update/"
            f"urn:li:{urn_match.group(1)}:{urn_match.group(2)}"
        )

    decoded_path = unquote(parsed.path)
    activity_match = re.search(r"(?:activity-|/activity/|/activity:)(\d+)", decoded_path)
    if activity_match:
        return f"https://www.linkedin.com/feed/update/urn:li:activity:{activity_match.group(1)}"

    return None


def _atomic_write(path: Path, data: str) -> None:
    """Write data to a file atomically using a temp file + rename."""
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(data, encoding="utf-8")
    os.replace(str(tmp_path), str(path))


def _read_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    """Read a JSON file, returning default on missing/corrupted files."""
    if not path.exists():
        return default
    try:
        raw = path.read_text(encoding="utf-8")
        return json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return default


class CursorsStore:
    """Persist and retrieve scrape cursors per source.

    Schema:
    {
        "x": {"cursor": "...", "mode": "delta", "last_run": "..."},
        "linkedin": {"cursor": "...", "mode": "delta", "last_run": "..."}
    }
    """

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self.file = state_dir / "cursors.json"

    def get(self, source: Source) -> dict[str, Any]:
        """Return cursor data for a source, or empty dict if not found."""
        data = _read_json(self.file, {})
        return data.get(source.value, {})

    def save(self, source: Source, cursor: str | None, mode: str) -> None:
        """Save cursor data for a source with current timestamp."""
        data = _read_json(self.file, {})
        entry: dict[str, Any] = {
            "cursor": cursor,
            "mode": mode,
            "last_run": datetime.now(UTC).isoformat(),
        }
        data[source.value] = entry
        self.state_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write(self.file, json.dumps(data, indent=2))


class ProcessedUrlStore:
    """Track processed URLs to avoid re-processing duplicates.

    Schema:
    {
        "urls": {
            "https://...": {"first_seen": "...", "source": "x"}
        }
    }
    """

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self.file = state_dir / "processed_urls.json"

    def load(self) -> dict[str, dict[str, str]]:
        """Return the dict of processed URLs with normalized keys."""
        data = _read_json(self.file, {})
        raw = data.get("urls", {})
        return {normalize_url(k): v for k, v in raw.items()}

    def contains(self, url: str) -> bool:
        """Check if a URL has already been processed."""
        return normalize_url(url) in self.load()

    def add(self, url: str, source: str) -> None:
        """Record a URL as processed."""
        normalized = normalize_url(url)
        data = _read_json(self.file, {})
        if "urls" not in data:
            data["urls"] = {}
        if normalized not in data["urls"]:
            data["urls"][normalized] = {
                "first_seen": datetime.now(UTC).isoformat(),
                "source": source,
            }
            self.state_dir.mkdir(parents=True, exist_ok=True)
            _atomic_write(self.file, json.dumps(data, indent=2))

    def add_many(self, urls: dict[str, str]) -> None:
        """Record multiple URLs at once. Keys are URLs, values are sources."""
        data = _read_json(self.file, {})
        if "urls" not in data:
            data["urls"] = {}
        now = datetime.now(UTC).isoformat()
        for url, source in urls.items():
            normalized = normalize_url(url)
            if normalized not in data["urls"]:
                data["urls"][normalized] = {"first_seen": now, "source": source}
        if urls:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            _atomic_write(self.file, json.dumps(data, indent=2))


class DeadLetterWriter:
    """Append failed items to a JSON Lines file.

    Each line is a JSON-serialized DeadLetter.
    """

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self.file = state_dir / "dead_letter.jsonl"

    def append(self, dead_letter: DeadLetter) -> None:
        """Append a failed item to the dead letter file."""
        self.state_dir.mkdir(parents=True, exist_ok=True)
        line = dead_letter.model_dump_json()
        with open(self.file, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def read_all(self) -> list[dict[str, Any]]:
        """Read all dead letter entries. Returns empty list if file missing."""
        if not self.file.exists():
            return []
        entries: list[dict[str, Any]] = []
        for line in self.file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return entries


class LockFile:
    """Pipeline lock file to prevent concurrent cron runs.

    Lock is stale if older than 4 hours.
    """

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self.file = state_dir / "pipeline.lock"

    def is_locked(self) -> bool:
        """Return True if lock exists and is NOT stale."""
        if not self.file.exists():
            return False
        age = time.time() - self.file.stat().st_mtime
        return age < LOCK_STALE_SECONDS

    def acquire(self) -> bool:
        """Create the lock file. Returns False if already locked (non-stale)."""
        if self.is_locked():
            return False
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.file.write_text(
            json.dumps({"pid": os.getpid(), "acquired_at": datetime.now(UTC).isoformat()}),
            encoding="utf-8",
        )
        return True

    def release(self) -> None:
        """Remove the lock file."""
        if self.file.exists():
            self.file.unlink()
