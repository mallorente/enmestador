"""Tests for scraper_x.py — mocked Playwright + mocked GraphQL responses."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from models import ScrapeMode, Source
from scrapers.x import (
    ScraperX,
    _bookmark_from_dom_post,
    _bookmark_from_graphql,
    _extract_image_urls,
    _extract_referenced_tweet_urls,
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
        assert bm.published_at is not None
        assert bm.saved_at is None

    def test_handles_missing_text(self) -> None:
        raw = _make_tweet("1", "", "nobody")
        raw["legacy"]["full_text"] = ""
        bm = _bookmark_from_graphql(raw)
        assert bm is not None
        assert bm.post_text is None
        assert "nobody" in bm.title

    def test_uses_note_tweet_text_for_long_posts(self) -> None:
        raw = _make_tweet("2", "Short preview", "writer")
        raw["note_tweet"] = {
            "note_tweet_results": {
                "result": {
                    "text": "Long complete post text that should win over legacy full_text",
                },
            },
        }
        bm = _bookmark_from_graphql(raw)
        assert bm is not None
        assert bm.post_text == "Long complete post text that should win over legacy full_text"

    def test_extracts_graphql_images(self) -> None:
        raw = _make_tweet("3", "Post with image", "photo_user")
        raw["legacy"]["extended_entities"] = {
            "media": [
                {
                    "type": "photo",
                    "media_url_https": "https://pbs.twimg.com/media/photo.jpg",
                }
            ]
        }
        assert _extract_image_urls(raw) == ["https://pbs.twimg.com/media/photo.jpg"]
        bm = _bookmark_from_graphql(raw)
        assert bm is not None
        assert bm.image_urls == ["https://pbs.twimg.com/media/photo.jpg"]

    def test_ignores_unresolved_tco_media_links(self) -> None:
        raw = _make_tweet("4", "Photo https://t.co/media123", "photo_user")
        raw["legacy"]["entities"] = {"urls": []}
        bm = _bookmark_from_graphql(raw)
        assert bm is not None
        assert bm.external_urls is None

    def test_extracts_referenced_tweet_urls_separately(self) -> None:
        raw = _make_tweet("5", "See this https://t.co/ref123", "main_user")
        raw["legacy"]["entities"] = {
            "urls": [
                {
                    "url": "https://t.co/ref123",
                    "expanded_url": "https://twitter.com/other_user/status/123456789",
                }
            ]
        }
        current_url = "https://x.com/main_user/status/5"
        assert _extract_referenced_tweet_urls(raw, current_url) == [
            "https://x.com/other_user/status/123456789"
        ]

        bm = _bookmark_from_graphql(raw)
        assert bm is not None
        assert bm.external_urls is None
        assert bm.referenced_tweet_urls == ["https://x.com/other_user/status/123456789"]

    def test_dom_post_to_bookmark(self) -> None:
        bm = _bookmark_from_dom_post({
            "url": "https://x.com/alice/status/123?foo=bar",
            "text": "Rendered tweet text",
            "author": "alice",
            "published_at": "2026-05-13T01:00:00.000Z",
            "external_urls": ["https://example.com/article"],
            "referenced_tweet_urls": ["https://twitter.com/bob/status/999"],
            "image_urls": ["https://pbs.twimg.com/media/rendered.jpg"],
        })
        assert bm is not None
        assert str(bm.url) == "https://x.com/alice/status/123"
        assert bm.post_text == "Rendered tweet text"
        assert bm.published_at is not None
        assert bm.external_urls == ["https://example.com/article"]
        assert bm.referenced_tweet_urls == ["https://x.com/bob/status/999"]
        assert bm.image_urls == ["https://pbs.twimg.com/media/rendered.jpg"]

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
                mock_response.status = 200
                mock_response.text = AsyncMock(return_value=body)
                callback(mock_response)

        mock_page.on.side_effect = _capture_callback

        import asyncio
        results = asyncio.run(scraper.scrape(ScrapeMode.BOOTSTRAP))
        assert len(results) == 3
        assert all(bm.source == Source.X for bm in results)
        assert scraper.saw_authenticated_bookmarks_endpoint is True

    def test_bootstrap_with_processed_skip(self, mock_page: AsyncMock, tmp_state: Path) -> None:
        """Bootstrap: skip URLs already in processed_urls store."""
        from pipeline.state import ProcessedUrlStore

        store = ProcessedUrlStore(tmp_state)
        store.add("https://x.com/testuser/status/0", "x")

        tweets = [_make_tweet("0", "Already processed"), _make_tweet("1", "New tweet")]
        body = _make_graphql_body(tweets, cursor=None)

        bookmarks_raw, _ = _parse_graphql_response(body)
        assert len(bookmarks_raw) == 2

        # Verify skip logic
        processed = store.load()
        assert "https://x.com/testuser/status/0" in processed

    def test_can_disable_processed_url_skip(self, mock_page: AsyncMock, tmp_state: Path) -> None:
        """Fresh historical runs can include URLs already in processed_urls."""
        from pipeline.state import ProcessedUrlStore

        store = ProcessedUrlStore(tmp_state)
        store.add("https://x.com/testuser/status/0", "x")

        tweets = [_make_tweet("0", "Already processed"), _make_tweet("1", "New tweet")]
        body = _make_graphql_body(tweets, cursor=None)
        scraper = ScraperX(mock_page, tmp_state, skip_processed=False)

        def _capture_callback(*args, **kwargs):
            if args[0] == "response":
                callback = args[1]
                mock_response = MagicMock()
                mock_response.url = "https://x.com/i/api/graphql/abc123/Bookmarks"
                mock_response.text = AsyncMock(return_value=body)
                callback(mock_response)

        mock_page.on.side_effect = _capture_callback

        import asyncio
        results = asyncio.run(scraper.scrape(ScrapeMode.BOOTSTRAP))
        assert len(results) == 2
        assert {str(result.url) for result in results} == {
            "https://x.com/testuser/status/0",
            "https://x.com/testuser/status/1",
        }

    def test_bootstrap_empty(self, mock_page: AsyncMock, tmp_state: Path) -> None:
        """Bootstrap: empty bookmarks returns empty list."""
        body = _make_graphql_body_empty()
        bookmarks_raw, cursor = _parse_graphql_response(body)
        assert bookmarks_raw == []
        assert cursor is None

    def test_api_empty_uses_dom_fallback(self, tmp_state: Path) -> None:
        """If API capture yields no bookmarks, scrape rendered tweets from DOM."""
        import asyncio

        mock_page = AsyncMock()
        mock_page.goto = AsyncMock()
        mock_page.wait_for_timeout = AsyncMock()
        mock_page.add_init_script = AsyncMock()
        mock_page.on = MagicMock()
        mock_page.url = "https://x.com/i/bookmarks"
        mock_page.title = AsyncMock(return_value="Bookmarks / X")
        mock_page.keyboard.press = AsyncMock()

        dom_posts = [{
            "url": "https://x.com/alice/status/123",
            "text": "Rendered tweet text",
            "author": "alice",
            "published_at": "2026-05-13T01:00:00.000Z",
            "external_urls": [],
        }]

        async def _evaluate(script, *args, **kwargs):
            src = str(script)
            if "__x_responses" in src:
                return []
            if "document.body ? document.body.innerText.length" in src:
                return 100
            if "querySelectorAll('article" in src:
                return dom_posts
            return None

        mock_page.evaluate = AsyncMock(side_effect=_evaluate)
        scraper = ScraperX(mock_page, tmp_state, max_bookmarks=5, skip_processed=False)

        results = asyncio.run(scraper.scrape(ScrapeMode.BOOTSTRAP))
        assert len(results) == 1
        assert str(results[0].url) == "https://x.com/alice/status/123"
        assert results[0].post_text == "Rendered tweet text"
        assert results[0].published_at is not None

    def test_bookmarks_403_does_not_count_as_authenticated(self, mock_page: AsyncMock, tmp_state: Path) -> None:
        """A failed Bookmarks endpoint should still allow the auth health check to run."""
        body = _make_graphql_body_empty()
        scraper = ScraperX(mock_page, tmp_state)

        def _capture_callback(*args, **kwargs):
            if args[0] == "response":
                callback = args[1]
                mock_response = MagicMock()
                mock_response.url = "https://x.com/i/api/graphql/abc123/Bookmarks"
                mock_response.status = 403
                mock_response.text = AsyncMock(return_value=body)
                callback(mock_response)

        mock_page.on.side_effect = _capture_callback

        import asyncio
        asyncio.run(scraper.scrape(ScrapeMode.BOOTSTRAP))

        assert scraper.bookmarks_endpoint_statuses == [403]
        assert scraper.saw_authenticated_bookmarks_endpoint is False

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
        from pipeline.state import CursorsStore

        store = CursorsStore(tmp_state)
        store.save(Source.X, "saved_cursor==", "delta")

        data = store.get(Source.X)
        assert data["cursor"] == "saved_cursor=="
        assert data["mode"] == "delta"

    def test_delta_fallback_to_bootstrap_when_no_cursor(self, tmp_state: Path) -> None:
        """Delta mode with no saved cursor: falls back to bootstrap."""
        from pipeline.state import CursorsStore

        store = CursorsStore(tmp_state)
        data = store.get(Source.X)
        assert data == {}  # No cursor saved

    def test_cursor_save_after_scrape(self, tmp_state: Path) -> None:
        """Verify cursor is saved after a scrape run."""
        from pipeline.state import CursorsStore

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


def test_process_body_stops_after_known_frontier(tmp_path: Path) -> None:
    scraper = ScraperX(
        MagicMock(),
        tmp_path,
        known_urls={
            "https://x.com/known/status/2",
            "https://x.com/known/status/3",
        },
        stop_after_known=2,
    )
    body = _make_graphql_body([
        _make_tweet("1", "New", "new"),
        _make_tweet("2", "Known 1", "known"),
        _make_tweet("3", "Known 2", "known"),
        _make_tweet("4", "Too far", "new"),
    ])
    results = []

    scraper._process_body(body, results, set(), {})

    assert [str(bookmark.url) for bookmark in results] == ["https://x.com/new/status/1"]
    assert scraper.boundary.matched is True
