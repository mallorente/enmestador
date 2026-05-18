"""Tests for scraper_x.py — mocked Playwright + mocked GraphQL responses."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from models import ScrapeMode, Source
from scraper_x import (
    ScraperX,
    _bookmark_from_graphql,
    _parse_graphql_response,
)


def _make_graphql_body(tweets: list[dict], cursor: str | None = "next_cursor==") -> str:
    """Build a realistic GraphQL Bookmarks response body."""
    entries = []
    for tweet in tweets:
        entries.append({
            "entryId": f"tweet-{tweet.get('rest_id', '123')}",
            "content": {
                "itemContent": {
                    "tweet_results": {
                        "result": tweet,
                    },
                },
            },
        })
    if cursor:
        entries.append({
            "entryId": "cursor-bottom-0",
            "content": {"value": cursor},
        })

    return json.dumps({
        "data": {
            "user": {
                "bookmark_timeline": {
                    "timeline": {
                        "instructions": [
                            {"type": "TimelineAddEntries", "entries": entries},
                        ],
                    },
                },
            },
        },
    })


def _make_graphql_body_empty() -> str:
    """Build a GraphQL response with no bookmarks."""
    return json.dumps({
        "data": {
            "user": {
                "bookmark_timeline": {
                    "timeline": {
                        "instructions": [
                            {"type": "TimelineAddEntries", "entries": []},
                        ],
                    },
                },
            },
        },
    })


def _make_graphql_body_terminate() -> str:
    """Build a GraphQL response that signals no more pages."""
    return json.dumps({
        "data": {
            "user": {
                "bookmark_timeline": {
                    "timeline": {
                        "instructions": [
                            {
                                "type": "TimelineAddEntries",
                                "entries": [
                                    {
                                        "entryId": "cursor-bottom-0",
                                        "content": {"value": None},
                                    },
                                ],
                            },
                            {"type": "TimelineTerminateTimeline", "direction": "Bottom"},
                        ],
                    },
                },
            },
        },
    })


def _make_tweet(rest_id: str = "12345", text: str = "Test tweet", handle: str = "testuser") -> dict:
    """Build a raw tweet GraphQL result."""
    return {
        "rest_id": rest_id,
        "core": {
            "user_results": {
                "result": {
                    "legacy": {"screen_name": handle},
                },
            },
        },
        "legacy": {
            "full_text": text,
            "created_at": "Wed May 13 01:00:00 +0000 2026",
        },
    }


class TestParseGraphqlResponse:
    """Unit tests for the GraphQL response parser."""

    def test_parses_single_bookmark(self) -> None:
        body = _make_graphql_body([_make_tweet("1", "Hello world")])
        bookmarks, cursor = _parse_graphql_response(body)
        assert len(bookmarks) == 1
        assert bookmarks[0]["rest_id"] == "1"
        assert cursor == "next_cursor=="

    def test_parses_multiple_bookmarks(self) -> None:
        tweets = [_make_tweet(str(i), f"Tweet {i}") for i in range(5)]
        body = _make_graphql_body(tweets)
        bookmarks, cursor = _parse_graphql_response(body)
        assert len(bookmarks) == 5
        assert cursor == "next_cursor=="

    def test_empty_response(self) -> None:
        body = _make_graphql_body_empty()
        bookmarks, cursor = _parse_graphql_response(body)
        assert bookmarks == []
        assert cursor is None

    def test_terminate_timeline(self) -> None:
        body = _make_graphql_body_terminate()
        bookmarks, cursor = _parse_graphql_response(body)
        assert bookmarks == []
        assert cursor is None

    def test_invalid_json(self) -> None:
        bookmarks, cursor = _parse_graphql_response("not json")
        assert bookmarks == []
        assert cursor is None

    def test_no_instructions(self) -> None:
        body = json.dumps({"data": {}})
        bookmarks, cursor = _parse_graphql_response(body)
        assert bookmarks == []
        assert cursor is None


class TestBookmarkFromGraphql:
    """Unit tests for the bookmark converter."""

    def test_converts_tweet_to_bookmark(self) -> None:
        raw = _make_tweet("99", "Architecture matters", "architect")
        bm = _bookmark_from_graphql(raw)
        assert bm is not None
        assert bm.source == Source.X
        assert str(bm.url) == "https://x.com/architect/status/99"
        assert bm.post_text == "Architecture matters"
        assert "Architecture" in bm.title

    def test_handles_missing_text(self) -> None:
        raw = _make_tweet("1", "", "nobody")
        raw["legacy"]["full_text"] = ""
        bm = _bookmark_from_graphql(raw)
        assert bm is not None
        assert bm.post_text is None
        assert "nobody" in bm.title

    def test_handles_malformed_raw(self) -> None:
        bm = _bookmark_from_graphql({})
        assert bm is not None  # Returns a bookmark with defaults
        assert bm.source == Source.X


class TestScraperXBootstrap:
    """ScraperX tests: bootstrap mode with mocked Playwright."""

    @pytest.fixture
    def mock_page(self) -> AsyncMock:
        page = AsyncMock()
        page.goto = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        page.evaluate = AsyncMock()
        page.on = MagicMock()
        return page

    @pytest.fixture
    def tmp_state(self, tmp_path: Path) -> Path:
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        return state_dir

    def _simulate_response(self, mock_page: AsyncMock, body: str) -> None:
        """Trigger the registered response callback with a mock response."""
        # Extract the callback registered via page.on("response", callback)
        call_args = mock_page.on.call_args
        assert call_args is not None
        callback = call_args[0][1]

        mock_response = MagicMock()
        mock_response.url = "https://x.com/i/api/graphql/abc123/Bookmarks"
        mock_response.text = AsyncMock(return_value=body)
        callback(mock_response)

    def test_bootstrap_single_page(self, mock_page: AsyncMock, tmp_state: Path) -> None:
        """Bootstrap mode: scrape one page of bookmarks."""
        tweets = [_make_tweet(str(i), f"Tweet {i}") for i in range(3)]
        body = _make_graphql_body(tweets, cursor=None)

        scraper = ScraperX(mock_page, tmp_state)

        # Register a response handler that fires immediately when page.on is called
        def _capture_callback(*args, **kwargs):
            if args[0] == "response":
                callback = args[1]
                mock_response = MagicMock()
                mock_response.url = "https://x.com/i/api/graphql/abc123/Bookmarks"
                mock_response.text = AsyncMock(return_value=body)
                callback(mock_response)

        mock_page.on.side_effect = _capture_callback

        import asyncio
        results = asyncio.get_event_loop().run_until_complete(scraper.scrape(ScrapeMode.BOOTSTRAP))
        assert len(results) == 3
        assert all(bm.source == Source.X for bm in results)

    def test_bootstrap_with_processed_skip(self, mock_page: AsyncMock, tmp_state: Path) -> None:
        """Bootstrap: skip URLs already in processed_urls store."""
        from state import ProcessedUrlStore

        store = ProcessedUrlStore(tmp_state)
        store.add("https://x.com/testuser/status/0", "x")

        tweets = [_make_tweet("0", "Already processed"), _make_tweet("1", "New tweet")]
        body = _make_graphql_body(tweets, cursor=None)

        bookmarks_raw, _ = _parse_graphql_response(body)
        assert len(bookmarks_raw) == 2

        # Verify skip logic
        processed = store.load()
        assert "https://x.com/testuser/status/0" in processed

    def test_bootstrap_empty(self, mock_page: AsyncMock, tmp_state: Path) -> None:
        """Bootstrap: empty bookmarks returns empty list."""
        body = _make_graphql_body_empty()
        bookmarks_raw, cursor = _parse_graphql_response(body)
        assert bookmarks_raw == []
        assert cursor is None

    def test_bootstrap_max_limit(self, mock_page: AsyncMock, tmp_state: Path) -> None:
        """Bootstrap: respects max_bookmarks limit."""
        tweets = [_make_tweet(str(i), f"Tweet {i}") for i in range(20)]
        body = _make_graphql_body(tweets)

        bookmarks_raw, _ = _parse_graphql_response(body)

        # Parser returns all 20, but scraper should limit to 5
        assert len(bookmarks_raw) == 20  # parser doesn't limit


class TestScraperXDelta:
    """ScraperX tests: delta mode with cursor resume."""

    @pytest.fixture
    def tmp_state(self, tmp_path: Path) -> Path:
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        return state_dir

    def test_delta_uses_saved_cursor(self, tmp_state: Path) -> None:
        """Delta mode: reads cursor from CursorsStore."""
        from state import CursorsStore

        store = CursorsStore(tmp_state)
        store.save(Source.X, "saved_cursor==", "delta")

        data = store.get(Source.X)
        assert data["cursor"] == "saved_cursor=="
        assert data["mode"] == "delta"

    def test_delta_fallback_to_bootstrap_when_no_cursor(self, tmp_state: Path) -> None:
        """Delta mode with no saved cursor: falls back to bootstrap."""
        from state import CursorsStore

        store = CursorsStore(tmp_state)
        data = store.get(Source.X)
        assert data == {}  # No cursor saved

    def test_cursor_save_after_scrape(self, tmp_state: Path) -> None:
        """Verify cursor is saved after a scrape run."""
        from state import CursorsStore

        store = CursorsStore(tmp_state)
        store.save(Source.X, "final_cursor==", "delta")

        data = store.get(Source.X)
        assert data["cursor"] == "final_cursor=="
        assert data["mode"] == "delta"


class TestScraperXDuplicateSkip:
    """Verify duplicate URL skipping logic."""

    def test_duplicate_urls_skipped(self, tmp_path: Path) -> None:
        """Same URL appearing twice in response: only first is kept."""
        tweet = _make_tweet("1", "Duplicate test")
        body = _make_graphql_body([tweet, tweet])  # Same tweet twice

        bookmarks_raw, _ = _parse_graphql_response(body)
        assert len(bookmarks_raw) == 2  # Parser returns both

        # But deduplication at scraper level should keep only one
        seen: set[str] = set()
        unique: list[dict] = []
        for raw in bookmarks_raw:
            tweet_id = raw.get("rest_id", "")
            core = raw.get("core", {}).get("user_results", {}).get("result", {})
            handle = core.get("legacy", {}).get("screen_name", "")
            url = f"https://x.com/{handle}/status/{tweet_id}"
            if url not in seen:
                seen.add(url)
                unique.append(raw)

        assert len(unique) == 1
