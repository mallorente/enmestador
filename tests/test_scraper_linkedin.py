"""Tests for scraper_linkedin.py — mocked Playwright + mocked API responses."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from models import ScrapeMode, Source
from scrapers.linkedin import (
    LINKEDIN_API_PATTERNS,
    LINKEDIN_VOYAGER_PATTERN,
    ScraperLinkedIn,
    _bookmark_from_linkedin_post,
    _extract_post_from_element,
    _parse_linkedin_api_response,
)


def _make_linkedin_api_body(posts: list[dict], next_cursor: str | None = "cursor_abc") -> str:
    """Build a realistic LinkedIn API response body."""
    elements = []
    for post in posts:
        elements.append({
            "content": {
                "data": {
                    "url": post.get("url", "https://www.linkedin.com/feed/update/abc123"),
                    "text": post.get("text", "Test post content"),
                    "author": {
                        "name": post.get("author", "Test Author"),
                    },
                    "createdAt": post.get("created_at", "2026-05-13T01:00:00Z"),
                },
            },
        })

    return json.dumps({
        "data": {
            "elements": elements,
            "paging": {
                "nextCursor": next_cursor,
            },
        },
    })


def _make_linkedin_api_body_empty() -> str:
    """Build a LinkedIn API response with no posts."""
    return json.dumps({
        "data": {
            "elements": [],
            "paging": {},
        },
    })


def _make_linkedin_api_body_flat(posts: list[dict]) -> str:
    """Build a LinkedIn API response with flat elements (no nested data)."""
    elements = []
    for post in posts:
        elements.append({
            "url": post.get("url", "https://www.linkedin.com/feed/update/xyz"),
            "text": post.get("text", "Flat post"),
            "author": post.get("author", "Flat Author"),
        })

    return json.dumps({
        "elements": elements,
    })


class TestParseLinkedinApiResponse:
    """Unit tests for the LinkedIn API response parser."""

    def test_parses_single_post(self) -> None:
        body = _make_linkedin_api_body([{"text": "Hello LinkedIn"}])
        posts, cursor = _parse_linkedin_api_response(body)
        assert len(posts) == 1
        assert posts[0]["text"] == "Hello LinkedIn"
        assert cursor == "cursor_abc"

    def test_parses_multiple_posts(self) -> None:
        posts_data = [{"text": f"Post {i}"} for i in range(5)]
        body = _make_linkedin_api_body(posts_data)
        posts, cursor = _parse_linkedin_api_response(body)
        assert len(posts) == 5
        assert cursor == "cursor_abc"

    def test_empty_response(self) -> None:
        body = _make_linkedin_api_body_empty()
        posts, cursor = _parse_linkedin_api_response(body)
        assert posts == []
        assert cursor is None

    def test_flat_elements_structure(self) -> None:
        """Handle flat elements without nested data wrapper."""
        body = _make_linkedin_api_body_flat([{"text": "Flat post"}])
        posts, cursor = _parse_linkedin_api_response(body)
        assert len(posts) == 1
        assert posts[0]["text"] == "Flat post"

    def test_invalid_json(self) -> None:
        posts, cursor = _parse_linkedin_api_response("not json")
        assert posts == []
        assert cursor is None

    def test_no_data_key(self) -> None:
        body = json.dumps({"something": "else"})
        posts, cursor = _parse_linkedin_api_response(body)
        assert posts == []
        assert cursor is None


class TestExtractPostFromElement:
    """Unit tests for individual element extraction."""

    def test_extracts_full_element(self) -> None:
        element = {
            "content": {
                "data": {
                    "url": "/feed/update/123",
                    "text": "Great article on architecture",
                    "author": {"name": "Jane Doe"},
                    "createdAt": "2026-05-13T01:00:00Z",
                },
            },
        }
        result = _extract_post_from_element(element)
        assert result is not None
        assert result["url"] == "https://www.linkedin.com/feed/update/123"
        assert result["text"] == "Great article on architecture"
        assert result["author"] == "Jane Doe"

    def test_normalizes_relative_url(self) -> None:
        element = {
            "content": {
                "data": {
                    "url": "/feed/update/456",
                    "text": "Test",
                },
            },
        }
        result = _extract_post_from_element(element)
        assert result is not None
        assert result["url"].startswith("https://www.linkedin.com")

    def test_returns_none_without_url(self) -> None:
        element = {
            "content": {
                "data": {
                    "text": "No URL here",
                },
            },
        }
        result = _extract_post_from_element(element)
        assert result is None

    def test_handles_author_first_last_name(self) -> None:
        element = {
            "content": {
                "data": {
                    "url": "/feed/update/789",
                    "text": "Test",
                    "author": {"firstName": "John", "lastName": "Smith"},
                },
            },
        }
        result = _extract_post_from_element(element)
        assert result is not None
        assert result["author"] == "John Smith"

    def test_handles_public_identifier_fallback(self) -> None:
        element = {
            "content": {
                "data": {
                    "url": "/feed/update/100",
                    "text": "Test",
                    "author": {"publicIdentifier": "john-doe"},
                },
            },
        }
        result = _extract_post_from_element(element)
        assert result is not None
        assert result["author"] == "john-doe"


class TestBookmarkFromLinkedinPost:
    """Unit tests for the bookmark converter."""

    def test_converts_post_to_bookmark(self) -> None:
        raw = {
            "url": "https://www.linkedin.com/feed/update/abc123",
            "text": "Architecture matters more than frameworks",
            "author": "Arch Guru",
            "created_at": "2026-05-13T01:00:00Z",
        }
        bm = _bookmark_from_linkedin_post(raw)
        assert bm is not None
        assert bm.source == Source.LINKEDIN
        assert str(bm.url) == "https://www.linkedin.com/feed/update/abc123"
        assert bm.post_text == "Architecture matters more than frameworks"
        assert "Architecture" in bm.title

    def test_handles_missing_text(self) -> None:
        raw = {
            "url": "https://www.linkedin.com/feed/update/xyz",
            "text": "",
            "author": "Nobody",
        }
        bm = _bookmark_from_linkedin_post(raw)
        assert bm is not None
        assert bm.post_text is None
        assert "Nobody" in bm.title

    def test_handles_no_url(self) -> None:
        bm = _bookmark_from_linkedin_post({"text": "No URL"})
        assert bm is None

    def test_parses_timestamp(self) -> None:
        raw = {
            "url": "https://www.linkedin.com/feed/update/123",
            "text": "Timestamped",
            "created_at": "2026-05-13T01:00:00Z",
        }
        bm = _bookmark_from_linkedin_post(raw)
        assert bm is not None
        assert bm.saved_at is not None

    def test_parses_unix_timestamp(self) -> None:
        raw = {
            "url": "https://www.linkedin.com/feed/update/456",
            "text": "Unix time",
            "created_at": 1747094400000,  # May 13, 2025 in ms
        }
        bm = _bookmark_from_linkedin_post(raw)
        assert bm is not None
        assert bm.saved_at is not None


def _make_js_drain_side_effect(body: str | None = None):
    """Return an evaluate side_effect that simulates JS-captured LinkedIn API responses.

    Returns the captured body on the first drain call, [] on subsequent drains,
    and None for scroll/other evaluate calls.
    """
    call_count = {"n": 0}

    async def _side_effect(script, *args, **kwargs):
        if "__li_responses" in str(script):
            if body is not None and call_count["n"] == 0:
                call_count["n"] += 1
                return [{"url": "https://www.linkedin.com/voyager/api/savedItems", "body": body, "status": 200}]
            return []
        return None

    return _side_effect


class TestScraperLinkedInApiSuccess:
    """ScraperLinkedIn tests: API interception success path."""

    @pytest.fixture
    def tmp_state(self, tmp_path: Path) -> Path:
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        return state_dir

    def _make_mock_page(self, body: str) -> AsyncMock:
        page = AsyncMock()
        page.goto = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        page.on = MagicMock()
        page.evaluate = AsyncMock(side_effect=_make_js_drain_side_effect(body))
        return page

    def test_api_interception_success(self, tmp_state: Path) -> None:
        """API interception captures posts successfully."""
        import asyncio

        posts_data = [
            {"text": "Post 1", "url": "https://www.linkedin.com/feed/update/1"},
            {"text": "Post 2", "url": "https://www.linkedin.com/feed/update/2"},
        ]
        body = _make_linkedin_api_body(posts_data, next_cursor=None)
        scraper = ScraperLinkedIn(self._make_mock_page(body), tmp_state)

        results = asyncio.run(scraper.scrape(ScrapeMode.BOOTSTRAP))
        assert len(results) == 2
        assert all(bm.source == Source.LINKEDIN for bm in results)

    def test_api_interception_empty_triggers_fallback(self, tmp_state: Path) -> None:
        """Empty API response triggers DOM fallback (simulated)."""
        import asyncio

        mock_page = AsyncMock()
        mock_page.goto = AsyncMock()
        mock_page.wait_for_timeout = AsyncMock()
        mock_page.on = MagicMock()

        async def _evaluate(script, *args, **kwargs):
            if "__li_responses" in str(script):
                return []  # No JS-captured posts → triggers DOM fallback
            return None  # Scroll and DOM extract calls → _extract_posts_from_dom returns []

        mock_page.evaluate = AsyncMock(side_effect=_evaluate)
        scraper = ScraperLinkedIn(mock_page, tmp_state)

        results = asyncio.run(scraper.scrape(ScrapeMode.BOOTSTRAP))
        assert results == []

    def test_skip_processed_urls(self, tmp_state: Path) -> None:
        """API interception: skip URLs already in processed_urls store."""
        import asyncio
        from pipeline.state import ProcessedUrlStore

        store = ProcessedUrlStore(tmp_state)
        store.add("https://www.linkedin.com/feed/update/1", "linkedin")

        posts_data = [
            {"text": "Already seen", "url": "https://www.linkedin.com/feed/update/1"},
            {"text": "New post", "url": "https://www.linkedin.com/feed/update/2"},
        ]
        body = _make_linkedin_api_body(posts_data, next_cursor=None)
        scraper = ScraperLinkedIn(self._make_mock_page(body), tmp_state)

        results = asyncio.run(scraper.scrape(ScrapeMode.BOOTSTRAP))
        assert len(results) == 1
        assert str(results[0].url) == "https://www.linkedin.com/feed/update/2"


class TestScraperLinkedInDomFallback:
    """ScraperLinkedIn tests: DOM fallback path."""

    @pytest.fixture
    def tmp_state(self, tmp_path: Path) -> Path:
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        return state_dir

    def test_dom_fallback_returns_empty_on_evaluate_none(self, tmp_state: Path) -> None:
        """DOM fallback: returns empty list when JS capture and DOM both yield nothing."""
        import asyncio

        mock_page = AsyncMock()
        mock_page.goto = AsyncMock()
        mock_page.wait_for_timeout = AsyncMock()
        mock_page.on = MagicMock()

        async def _evaluate(script, *args, **kwargs):
            if "__li_responses" in str(script):
                return []  # No JS-captured posts → triggers DOM fallback
            return None  # DOM extract → _extract_posts_from_dom returns []

        mock_page.evaluate = AsyncMock(side_effect=_evaluate)
        scraper = ScraperLinkedIn(mock_page, tmp_state)

        results = asyncio.run(scraper.scrape(ScrapeMode.BOOTSTRAP))
        assert results == []

    def test_dom_fallback_parses_dom_posts(self, tmp_state: Path) -> None:
        """DOM fallback: parses posts returned from page.evaluate."""
        import asyncio

        dom_posts = [
            {
                "url": "https://www.linkedin.com/feed/update/dom1",
                "text": "DOM scraped post",
                "author": "DOM Author",
                "created_at": "",
            },
        ]

        mock_page = AsyncMock()
        mock_page.goto = AsyncMock()
        mock_page.wait_for_timeout = AsyncMock()
        mock_page.on = MagicMock()

        async def _evaluate(script, *args, **kwargs):
            if "__li_responses" in str(script):
                return []  # No JS posts → triggers DOM fallback
            if "window.scrollBy" in str(script):
                return None  # Scroll calls
            return dom_posts  # DOM extract calls

        mock_page.evaluate = AsyncMock(side_effect=_evaluate)
        scraper = ScraperLinkedIn(mock_page, tmp_state)

        results = asyncio.run(scraper.scrape(ScrapeMode.BOOTSTRAP))
        assert len(results) == 1
        assert results[0].source == Source.LINKEDIN
        assert results[0].post_text == "DOM scraped post"


class TestApiPatterns:
    """Tests for LinkedIn API URL pattern matching."""

    def test_broad_voyager_pattern(self) -> None:
        """Any URL containing /voyager/ is matched by the broad pattern."""
        assert LINKEDIN_VOYAGER_PATTERN in "https://www.linkedin.com/voyager/api/something"

    def test_specific_patterns_covered(self) -> None:
        """Each specific pattern matches a known LinkedIn API URL."""
        for pattern in LINKEDIN_API_PATTERNS:
            assert pattern in f"https://www.linkedin.com{pattern}"

    def test_saved_items_pattern(self) -> None:
        """The /voyager/api/savedItems pattern matches current saved posts API."""
        url = "https://www.linkedin.com/voyager/api/savedItems?q=owner"
        assert any(pattern in url for pattern in LINKEDIN_API_PATTERNS)

    def test_bookmark_pattern(self) -> None:
        """The /voyager/api/bookmark pattern matches bookmark endpoints."""
        url = "https://www.linkedin.com/voyager/api/bookmarkPosts?start=0"
        assert any(pattern in url for pattern in LINKEDIN_API_PATTERNS)

    def test_voyager_graphql_pattern(self) -> None:
        """The /voyager/api/graphql pattern matches GraphQL endpoints."""
        url = "https://www.linkedin.com/voyager/api/graphql?q=node"
        assert any(pattern in url for pattern in LINKEDIN_API_PATTERNS)


class TestApiDeduplication:
    """Tests for intra-scrape deduplication via _seen_urls."""

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

    def test_dedup_within_api_responses(self, mock_page: AsyncMock, tmp_state: Path) -> None:
        """Duplicate URLs across API responses are deduplicated."""
        import asyncio

        posts_data = [
            {"text": "Same post", "url": "https://www.linkedin.com/feed/update/1"},
            {"text": "Same post again", "url": "https://www.linkedin.com/feed/update/1"},
        ]
        body = _make_linkedin_api_body(posts_data, next_cursor=None)
        mock_page.evaluate = AsyncMock(side_effect=_make_js_drain_side_effect(body))

        scraper = ScraperLinkedIn(mock_page, tmp_state)

        results = asyncio.run(scraper.scrape(ScrapeMode.BOOTSTRAP))
        url_set = {str(r.url) for r in results}
        assert len(url_set) <= 1


class TestSearchDashClusters:
    """Tests for the searchDashClustersByAll response structure."""

    def test_search_dash_clusters_structure(self) -> None:
        """Handle the current GraphQL saved posts structure."""
        body = json.dumps({
            "data": {
                "data": {
                    "searchDashClustersByAll": {
                        "elements": [
                            {
                                "items": [
                                    {
                                        "navigationUrl": "https://www.linkedin.com/feed/update/abc123",
                                        "commentary": {"text": "Great article on testing"},
                                        "author": {"name": "Test Author"},
                                    },
                                ],
                            },
                        ],
                        "metadata": {"paginationToken": "next_page_abc"},
                    },
                },
            },
        })
        posts, cursor = _parse_linkedin_api_response(body)
        assert len(posts) == 1
        assert posts[0]["url"] == "https://www.linkedin.com/feed/update/abc123"
        assert posts[0]["text"] == "Great article on testing"
        assert cursor == "next_page_abc"

    def test_search_dash_clusters_with_element_as_post(self) -> None:
        """Handle elements with no items but direct data."""
        body = json.dumps({
            "data": {
                "data": {
                    "searchDashClustersByAll": {
                        "elements": [
                            {
                                "navigationUrl": "https://www.linkedin.com/feed/update/direct1",
                                "text": "Direct post",
                            },
                        ],
                        "metadata": {"paginationToken": "page2"},
                    },
                },
            },
        })
        posts, cursor = _parse_linkedin_api_response(body)
        assert len(posts) == 1
        assert posts[0]["url"] == "https://www.linkedin.com/feed/update/direct1"
        assert cursor == "page2"


class TestPatchrightImport:
    """Verify patchright is installed and importable."""

    def test_patchright_importable(self) -> None:
        from patchright.async_api import async_playwright, BrowserContext, Page, Response  # noqa: F401


class TestAuthManagerConfig:
    """Verify AuthManager reads SLOW_MO env var and has no cookie injection."""

    def test_slow_mo_defaults_to_zero(self, monkeypatch) -> None:
        monkeypatch.delenv("SLOW_MO", raising=False)
        import importlib
        import auth.manager as am
        importlib.reload(am)
        mgr = am.AuthManager(user_data_dir="/tmp/test_auth_cfg")
        assert mgr._slow_mo == 0

    def test_slow_mo_reads_env_var(self, monkeypatch) -> None:
        monkeypatch.setenv("SLOW_MO", "75")
        import importlib
        import auth.manager as am
        importlib.reload(am)
        mgr = am.AuthManager(user_data_dir="/tmp/test_auth_cfg")
        assert mgr._slow_mo == 75

    def test_no_inject_cookies_method(self) -> None:
        import importlib
        import auth.manager as am
        importlib.reload(am)
        assert not hasattr(am.AuthManager, '_inject_cookies'), (
            "_inject_cookies must be removed — use persistent profile only"
        )

    def test_no_cookie_txt_references(self) -> None:
        import pathlib
        src = pathlib.Path("auth/manager.py").read_text()
        assert "x_cookies.txt" not in src
        assert "li_cookies.txt" not in src
        assert "_inject_cookies" not in src


class TestApiPagination:
    """Tests for LinkedIn API pagination via scrolling."""

    @pytest.fixture
    def tmp_state(self, tmp_path: Path) -> Path:
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        return state_dir

    def _make_page(self, side_effect) -> AsyncMock:
        page = AsyncMock()
        page.goto = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        page.on = MagicMock()
        page.evaluate = AsyncMock(side_effect=side_effect)
        return page

    def test_pagination_stops_at_max_posts(self, tmp_state: Path) -> None:
        """Pagination stops when max_posts is reached."""
        import asyncio

        posts_data = [
            {"text": f"Post {i}", "url": f"https://www.linkedin.com/feed/update/{i}"}
            for i in range(10)
        ]
        body = _make_linkedin_api_body(posts_data, next_cursor=None)
        scraper = ScraperLinkedIn(self._make_page(_make_js_drain_side_effect(body)), tmp_state, max_posts=3)

        results = asyncio.run(scraper.scrape(ScrapeMode.BOOTSTRAP))
        assert len(results) == 3

    def test_pagination_scroll_triggers_more_posts(self, tmp_state: Path) -> None:
        """After initial API capture, scroll triggers more responses."""
        import asyncio

        batch1 = _make_linkedin_api_body(
            [{"text": "Post 1", "url": "https://www.linkedin.com/feed/update/1"},
             {"text": "Post 2", "url": "https://www.linkedin.com/feed/update/2"}],
            next_cursor="page2",
        )
        batch2 = _make_linkedin_api_body(
            [{"text": "Post 3", "url": "https://www.linkedin.com/feed/update/3"},
             {"text": "Post 4", "url": "https://www.linkedin.com/feed/update/4"}],
            next_cursor=None,
        )
        drain_count = {"n": 0}

        async def _evaluate(script, *args, **kwargs):
            if "__li_responses" in str(script):
                drain_count["n"] += 1
                if drain_count["n"] == 1:
                    return [{"url": "https://www.linkedin.com/voyager/api/savedItems", "body": batch1, "status": 200}]
                if drain_count["n"] == 2:
                    return [{"url": "https://www.linkedin.com/voyager/api/savedItems", "body": batch2, "status": 200}]
                return []
            return None

        scraper = ScraperLinkedIn(self._make_page(_evaluate), tmp_state)
        results = asyncio.run(scraper.scrape(ScrapeMode.BOOTSTRAP))
        assert len(results) >= 2

    def test_pagination_stops_when_no_new_posts(self, tmp_state: Path) -> None:
        """Pagination stops after consecutive scrolls with no new posts."""
        import asyncio

        body = _make_linkedin_api_body(
            [{"text": "Only post", "url": "https://www.linkedin.com/feed/update/1"}],
            next_cursor=None,
        )
        scraper = ScraperLinkedIn(self._make_page(_make_js_drain_side_effect(body)), tmp_state)

        results = asyncio.run(scraper.scrape(ScrapeMode.BOOTSTRAP))
        assert len(results) == 1


class TestCookieRefresherNoPlawright:
    """cookie_refresher.py must use patchright, not playwright."""

    def test_no_playwright_import_in_refresher(self) -> None:
        import ast
        import pathlib
        src = pathlib.Path("auth/cookie_refresher.py").read_text()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                module = getattr(node, 'module', '') or ''
                assert 'playwright' not in module, (
                    f"cookie_refresher.py still imports from playwright: {module}"
                )

    def test_no_txt_cookie_export_in_refresher(self) -> None:
        import pathlib
        src = pathlib.Path("auth/cookie_refresher.py").read_text()
        assert "x_cookies.txt" not in src
        assert "li_cookies.txt" not in src
        assert "_playwright_cookies_to_netscape" not in src
        assert "write_text" not in src


class TestNoCookieInjectionAnywhere:
    """Verify cookie injection is fully removed from the codebase."""

    def test_auth_manager_has_no_inject_cookies(self) -> None:
        import importlib
        import auth.manager as am
        importlib.reload(am)
        assert not hasattr(am.AuthManager, '_inject_cookies')

    def test_auth_manager_has_no_cookie_txt_refs(self) -> None:
        import pathlib
        src = pathlib.Path("auth/manager.py").read_text()
        assert "x_cookies.txt" not in src
        assert "li_cookies.txt" not in src
        assert "_inject_cookies" not in src
        assert "load_netscape_cookies" not in src

    def test_cookie_loader_not_imported_in_manager(self) -> None:
        import pathlib
        src = pathlib.Path("auth/manager.py").read_text()
        assert "cookie_loader" not in src


class TestHumanizedScroll:
    """LinkedIn scraper uses variable scroll amounts and delays."""

    def test_human_scroll_ms_in_range(self) -> None:
        from scrapers.linkedin import _human_scroll_ms
        for _ in range(50):
            val = _human_scroll_ms()
            assert 1500 <= val <= 4000, f"_human_scroll_ms() returned {val}, expected 1500-4000"

    def test_human_scroll_px_in_range(self) -> None:
        from scrapers.linkedin import _human_scroll_px
        for _ in range(50):
            val = _human_scroll_px()
            assert 300 <= val <= 900, f"_human_scroll_px() returned {val}, expected 300-900"

    def test_scroll_values_vary(self) -> None:
        """Values should not all be the same — confirms randomness."""
        from scrapers.linkedin import _human_scroll_ms, _human_scroll_px
        ms_vals = [_human_scroll_ms() for _ in range(20)]
        px_vals = [_human_scroll_px() for _ in range(20)]
        assert len(set(ms_vals)) > 1, "All _human_scroll_ms() values identical — not random"
        assert len(set(px_vals)) > 1, "All _human_scroll_px() values identical — not random"
