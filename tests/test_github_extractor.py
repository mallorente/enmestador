"""Tests for extractors/github.py."""

from unittest.mock import AsyncMock, patch

import pytest

from extractors.github import extract_github_repos, fetch_repo, parse_repo_urls


class TestParseRepoUrls:
    def test_extracts_owner_repo(self):
        assert parse_repo_urls(["https://github.com/tinyfish-io/bigset"]) == [
            ("tinyfish-io", "bigset", "https://github.com/tinyfish-io/bigset")
        ]

    def test_strips_git_suffix(self):
        out = parse_repo_urls(["https://github.com/SWE-agent/SWE-agent.git"])
        assert out == [("SWE-agent", "SWE-agent", "https://github.com/SWE-agent/SWE-agent")]

    def test_dedupes_case_insensitive(self):
        out = parse_repo_urls([
            "https://github.com/a/b",
            "http://github.com/A/B",
            "https://github.com/a/b/issues/1",
        ])
        assert out == [("a", "b", "https://github.com/a/b")]

    def test_skips_non_repo_owners(self):
        assert parse_repo_urls(["https://github.com/sponsors/someone"]) == []

    def test_ignores_non_github(self):
        assert parse_repo_urls(["https://example.com/a/b", "https://x.com/foo"]) == []

    def test_handles_none_and_empty(self):
        assert parse_repo_urls(None) == []
        assert parse_repo_urls([]) == []


class TestFetchRepo:
    @pytest.mark.asyncio
    async def test_builds_repo_analysis(self):
        import base64

        readme_b64 = base64.b64encode(b"# Title\nSome readme body").decode()

        async def fake_gh(path):
            if path == "repos/o/r":
                return {
                    "full_name": "o/r",
                    "description": "a tool",
                    "stargazers_count": 42,
                    "language": "Python",
                    "topics": ["ai", "cli"],
                    "pushed_at": "2026-06-01T00:00:00Z",
                }
            if path == "repos/o/r/readme":
                return {"content": readme_b64}
            return None

        with patch("extractors.github._gh_api", side_effect=fake_gh):
            repo = await fetch_repo("o", "r", "https://github.com/o/r")

        assert repo is not None
        assert repo.full_name == "o/r"
        assert repo.stars == 42
        assert repo.language == "Python"
        assert repo.topics == ["ai", "cli"]
        assert "Some readme body" in repo.readme_excerpt

    @pytest.mark.asyncio
    async def test_returns_none_when_repo_unreachable(self):
        with patch("extractors.github._gh_api", AsyncMock(return_value=None)):
            repo = await fetch_repo("o", "r", "https://github.com/o/r")
        assert repo is None

    @pytest.mark.asyncio
    async def test_extract_filters_unreachable(self):
        async def fake_fetch(owner, repo, url):
            return None if repo == "bad" else AsyncMock(full_name=f"{owner}/{repo}")

        with patch("extractors.github.fetch_repo", side_effect=fake_fetch):
            out = await extract_github_repos([
                "https://github.com/o/good",
                "https://github.com/o/bad",
            ])
        assert len(out) == 1
