"""GitHub repository reader.

When a bookmark references a GitHub repo (in the post body or the author's first
comment), this fetches the repo's metadata and README via the `gh` CLI so the
note can carry a real evaluation of the repo, not just the link.
"""

import asyncio
import base64
import json
import logging
import re

from models import RepoAnalysis

logger = logging.getLogger(__name__)

_GH_RE = re.compile(r"https?://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)")
README_MAX_CHARS = 6000
GH_TIMEOUT = 30.0

# github.com/<first-segment> values that are not repo owners.
_NON_REPO_OWNERS = {
    "sponsors", "about", "topics", "collections", "marketplace", "features",
    "settings", "notifications", "explore", "orgs", "apps", "login", "join",
}
# <owner>/<second-segment> values that are not repositories.
_NON_REPO_NAMES = {"sponsors", "followers", "following", "stars", "repositories"}


def parse_repo_urls(urls: list[str] | None) -> list[tuple[str, str, str]]:
    """Return de-duplicated (owner, repo, canonical_url) tuples from a URL list."""
    out: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str]] = set()
    for raw in urls or []:
        m = _GH_RE.match(raw.strip())
        if not m:
            continue
        owner, repo = m.group(1), m.group(2)
        repo = repo[:-4] if repo.endswith(".git") else repo
        if owner.lower() in _NON_REPO_OWNERS or repo.lower() in _NON_REPO_NAMES:
            continue
        key = (owner.lower(), repo.lower())
        if key in seen:
            continue
        seen.add(key)
        out.append((owner, repo, f"https://github.com/{owner}/{repo}"))
    return out


async def _gh_api(path: str) -> dict | None:
    """Call `gh api <path>` and return parsed JSON, or None on any failure."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "gh", "api", path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        logger.warning("gh CLI not found; skipping GitHub repo analysis")
        return None
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=GH_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        logger.warning("gh api %s timed out", path)
        return None
    if proc.returncode != 0:
        logger.info("gh api %s failed: %s", path, err.decode(errors="replace")[:200])
        return None
    try:
        return json.loads(out.decode(errors="replace"))
    except json.JSONDecodeError:
        return None


async def fetch_repo(owner: str, repo: str, url: str) -> RepoAnalysis | None:
    """Fetch metadata + README excerpt for one repo. None if unreachable."""
    meta = await _gh_api(f"repos/{owner}/{repo}")
    if meta is None:
        return None

    readme_excerpt = None
    readme = await _gh_api(f"repos/{owner}/{repo}/readme")
    if readme and readme.get("content"):
        try:
            decoded = base64.b64decode(readme["content"]).decode("utf-8", "replace")
            readme_excerpt = decoded.strip()[:README_MAX_CHARS]
        except (ValueError, TypeError):
            readme_excerpt = None

    return RepoAnalysis(
        url=url,
        full_name=meta.get("full_name") or f"{owner}/{repo}",
        description=meta.get("description"),
        stars=meta.get("stargazers_count"),
        language=meta.get("language"),
        topics=meta.get("topics") or None,
        pushed_at=meta.get("pushed_at"),
        readme_excerpt=readme_excerpt,
    )


async def extract_github_repos(
    urls: list[str] | None, limit: int = 3
) -> list[RepoAnalysis]:
    """Resolve GitHub repo URLs to RepoAnalysis objects (skips unreachable ones)."""
    repos = parse_repo_urls(urls)[:limit]
    if not repos:
        return []
    results = await asyncio.gather(*(fetch_repo(o, r, u) for o, r, u in repos))
    return [r for r in results if r is not None]
