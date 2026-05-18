"""Tests for the state management module."""

import json
import os
import time
from pathlib import Path

from models import Bookmark, DeadLetter, Source
from state import (
    CursorsStore,
    DeadLetterWriter,
    LockFile,
    ProcessedUrlStore,
    _atomic_write,
    _read_json,
    normalize_url,
)

# --- _atomic_write ---


def test_atomic_write_creates_file(tmp_path: Path) -> None:
    target = tmp_path / "test.json"
    _atomic_write(target, '{"key": "value"}')
    assert target.exists()
    assert target.read_text() == '{"key": "value"}'


def test_atomic_write_overwrites(tmp_path: Path) -> None:
    target = tmp_path / "test.json"
    _atomic_write(target, "first")
    _atomic_write(target, "second")
    assert target.read_text() == "second"


# --- _read_json ---


def test_read_json_missing_file_returns_default(tmp_path: Path) -> None:
    result = _read_json(tmp_path / "nonexistent.json", {"fallback": True})
    assert result == {"fallback": True}


def test_read_json_valid_file(tmp_path: Path) -> None:
    path = tmp_path / "data.json"
    path.write_text('{"key": "value"}')
    result = _read_json(path, {})
    assert result == {"key": "value"}


def test_read_json_corrupted_returns_default(tmp_path: Path) -> None:
    path = tmp_path / "data.json"
    path.write_text("{invalid json!!!")
    result = _read_json(path, {"recovered": True})
    assert result == {"recovered": True}


# --- CursorsStore ---


def test_cursors_get_empty_state(tmp_state_dir: Path) -> None:
    store = CursorsStore(tmp_state_dir)
    assert store.get(Source.X) == {}


def test_cursors_save_and_get(tmp_state_dir: Path) -> None:
    store = CursorsStore(tmp_state_dir)
    store.save(Source.X, "cursor_abc==", "delta")
    data = store.get(Source.X)
    assert data["cursor"] == "cursor_abc=="
    assert data["mode"] == "delta"
    assert "last_run" in data


def test_cursors_save_creates_file(tmp_state_dir: Path) -> None:
    store = CursorsStore(tmp_state_dir)
    store.save(Source.X, "cursor_1", "delta")
    assert (tmp_state_dir / "cursors.json").exists()


def test_cursors_save_multiple_sources(tmp_state_dir: Path) -> None:
    store = CursorsStore(tmp_state_dir)
    store.save(Source.X, "x_cursor", "delta")
    store.save(Source.LINKEDIN, "li_cursor", "bootstrap")
    assert store.get(Source.X)["cursor"] == "x_cursor"
    assert store.get(Source.LINKEDIN)["cursor"] == "li_cursor"


def test_cursors_save_overwrites(tmp_state_dir: Path) -> None:
    store = CursorsStore(tmp_state_dir)
    store.save(Source.X, "old", "delta")
    store.save(Source.X, "new", "delta")
    assert store.get(Source.X)["cursor"] == "new"


def test_cursors_save_null_cursor(tmp_state_dir: Path) -> None:
    """Bootstrap mode may have no cursor."""
    store = CursorsStore(tmp_state_dir)
    store.save(Source.X, None, "bootstrap")
    assert store.get(Source.X)["cursor"] is None


def test_cursors_corrupted_file_recovers(tmp_state_dir: Path) -> None:
    path = tmp_state_dir / "cursors.json"
    path.write_text("{bad json")
    store = CursorsStore(tmp_state_dir)
    # Should recover and save cleanly
    store.save(Source.X, "recovered", "delta")
    data = store.get(Source.X)
    assert data["cursor"] == "recovered"


# --- ProcessedUrlStore ---


def test_processed_urls_empty_state(tmp_state_dir: Path) -> None:
    store = ProcessedUrlStore(tmp_state_dir)
    assert store.load() == {}
    assert not store.contains("https://example.com")


def test_processed_urls_add_and_contains(tmp_state_dir: Path) -> None:
    store = ProcessedUrlStore(tmp_state_dir)
    store.add("https://example.com/article", "x")
    assert store.contains("https://example.com/article")


def test_processed_urls_add_many(tmp_state_dir: Path) -> None:
    store = ProcessedUrlStore(tmp_state_dir)
    urls = {
        "https://a.com": "x",
        "https://b.com": "linkedin",
    }
    store.add_many(urls)
    assert store.contains("https://a.com")
    assert store.contains("https://b.com")


def test_processed_urls_dedup_add(tmp_state_dir: Path) -> None:
    """Adding the same URL twice should not duplicate the entry."""
    store = ProcessedUrlStore(tmp_state_dir)
    store.add("https://example.com", "x")
    store.add("https://example.com", "linkedin")
    data = store.load()
    assert len(data) == 1
    assert data[normalize_url("https://example.com")]["source"] == "x"


def test_processed_urls_dedup_add_many(tmp_state_dir: Path) -> None:
    """add_many should not overwrite existing entries."""
    store = ProcessedUrlStore(tmp_state_dir)
    store.add("https://a.com", "x")
    store.add_many({"https://a.com": "linkedin", "https://b.com": "x"})
    data = store.load()
    assert data[normalize_url("https://a.com")]["source"] == "x"
    assert normalize_url("https://b.com") in data


def test_processed_urls_creates_file(tmp_state_dir: Path) -> None:
    store = ProcessedUrlStore(tmp_state_dir)
    store.add("https://example.com", "x")
    assert (tmp_state_dir / "processed_urls.json").exists()


def test_processed_urls_corrupted_file_recovers(tmp_state_dir: Path) -> None:
    path = tmp_state_dir / "processed_urls.json"
    path.write_text("{corrupted")
    store = ProcessedUrlStore(tmp_state_dir)
    store.add("https://example.com", "x")
    assert store.contains("https://example.com")


# --- DeadLetterWriter ---


def test_dead_letter_append(tmp_state_dir: Path) -> None:
    writer = DeadLetterWriter(tmp_state_dir)
    bookmark = Bookmark(
        source=Source.X,
        url="https://example.com/failed",
        title="Failed Article",
    )
    dl = DeadLetter(bookmark=bookmark, error="Timeout", stage="extraction")
    writer.append(dl)
    entries = writer.read_all()
    assert len(entries) == 1
    assert entries[0]["error"] == "Timeout"
    assert entries[0]["stage"] == "extraction"


def test_dead_letter_append_multiple(tmp_state_dir: Path) -> None:
    writer = DeadLetterWriter(tmp_state_dir)
    bookmark = Bookmark(
        source=Source.X,
        url="https://example.com/1",
        title="Failed 1",
    )
    dl1 = DeadLetter(bookmark=bookmark, error="Error 1", stage="llm")
    dl2 = DeadLetter(bookmark=bookmark, error="Error 2", stage="write")
    writer.append(dl1)
    writer.append(dl2)
    entries = writer.read_all()
    assert len(entries) == 2
    assert entries[0]["error"] == "Error 1"
    assert entries[1]["error"] == "Error 2"


def test_dead_letter_read_empty(tmp_state_dir: Path) -> None:
    writer = DeadLetterWriter(tmp_state_dir)
    assert writer.read_all() == []


def test_dead_letter_corrupted_lines_skipped(tmp_state_dir: Path) -> None:
    path = tmp_state_dir / "dead_letter.jsonl"
    path.write_text('{"error": "good"}\n{bad json\n{"error": "also_good"}\n')
    writer = DeadLetterWriter(tmp_state_dir)
    entries = writer.read_all()
    assert len(entries) == 2
    assert entries[0]["error"] == "good"
    assert entries[1]["error"] == "also_good"


# --- LockFile ---


def test_lock_not_locked_when_no_file(tmp_state_dir: Path) -> None:
    lock = LockFile(tmp_state_dir)
    assert not lock.is_locked()


def test_lock_acquire_and_release(tmp_state_dir: Path) -> None:
    lock = LockFile(tmp_state_dir)
    assert lock.acquire() is True
    assert lock.is_locked()
    lock.release()
    assert not lock.is_locked()


def test_lock_acquire_returns_false_when_locked(tmp_state_dir: Path) -> None:
    lock = LockFile(tmp_state_dir)
    assert lock.acquire() is True
    assert lock.acquire() is False
    lock.release()


def test_lock_stale_after_4_hours(tmp_state_dir: Path) -> None:
    lock = LockFile(tmp_state_dir)
    lock.acquire()
    # Simulate stale by backdating the file
    stale_time = time.time() - (5 * 60 * 60)  # 5 hours ago
    os.utime(lock.file, (stale_time, stale_time))
    assert not lock.is_locked()  # Stale lock is "not locked"
    # Should be able to acquire again
    assert lock.acquire() is True
    lock.release()


def test_lock_file_content(tmp_state_dir: Path) -> None:
    lock = LockFile(tmp_state_dir)
    lock.acquire()
    content = json.loads(lock.file.read_text())
    assert "pid" in content
    assert "acquired_at" in content
    lock.release()


# --- normalize_url ---


def test_normalize_url_strips_trailing_slash():
    assert normalize_url("https://example.com/article/") == "https://example.com/article"


def test_normalize_url_strips_utm_params():
    assert normalize_url("https://example.com/article?utm_source=twitter") == "https://example.com/article"


def test_normalize_url_strips_fragment():
    assert normalize_url("https://example.com/article#section") == "https://example.com/article"


def test_normalize_url_lowercases_host():
    assert normalize_url("https://EXAMPLE.COM/Article") == "https://example.com/Article"


def test_normalize_url_preserves_legitimate_params():
    assert normalize_url("https://example.com/search?q=python&page=2") == "https://example.com/search?q=python&page=2"


def test_normalize_url_mixed_params():
    assert normalize_url("https://example.com/art?utm_source=x&q=python") == "https://example.com/art?q=python"


def test_normalize_url_preserves_path():
    assert normalize_url("https://example.com/a/b/c") == "https://example.com/a/b/c"


def test_processed_urls_normalized_dedup(tmp_state_dir: Path) -> None:
    store = ProcessedUrlStore(tmp_state_dir)
    store.add("https://example.com/article/", "x")
    assert store.contains("https://example.com/article")
    assert store.contains("https://example.com/article?utm_source=twitter")
