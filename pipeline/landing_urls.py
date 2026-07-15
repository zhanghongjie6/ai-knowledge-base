"""Rewrite destination URLs for WeChat-friendly HTML landings.

GitHub pages often fail inside 企微; README.md / jsDelivr raw markdown
renders as code. Prefer official docs sites, then DeepWiki **only when
indexed**, then GitCode Trending **only when the mirror has real content**.
Never use an empty GitCode shell or an unindexed DeepWiki page.
"""

from __future__ import annotations

import logging
import re
import urllib.error
import urllib.request
from typing import Final

logger = logging.getLogger(__name__)

_JSDELIVR_README_RE = re.compile(
    r"^https?://cdn\.jsdelivr\.net/gh/([^/\s]+)/([^@/\s]+)@[^/]+/README\.md$",
    re.IGNORECASE,
)
_GITHUB_REPO_RE = re.compile(
    r"^https?://github\.com/([^/\s]+)/([^/\s#?]+)/?$",
)
_META_DESC_RE = re.compile(
    r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']*)["\']',
    re.IGNORECASE,
)

# DeepWiki SPA shell for unindexed repos is ~34KB; indexed wiki HTML is much larger.
_DEEPWIKI_INDEXED_MIN_BYTES: Final[int] = 80_000
_PROBE_TIMEOUT: Final[float] = 6.0

KNOWN_LANDINGS: Final[dict[str, str]] = {
    "https://github.com/anthropics/claude-code":
        "https://code.claude.com/docs/zh-CN/overview",
    "https://github.com/msitarzewski/agency-agents":
        "https://agencyagents.app/",
    "https://github.com/github/spec-kit":
        "https://gitcode.com/GitHub_Trending/sp/spec-kit",
    "https://github.com/open-webui/open-webui":
        "https://openwebui.com/",
    "https://github.com/langflow-ai/langflow":
        "https://www.langflow.org/",
    "https://github.com/langgenius/dify":
        "https://dify.ai/",
    "https://github.com/ollama/ollama":
        "https://ollama.com/",
    "https://github.com/n8n-io/n8n":
        "https://n8n.io/",
    "https://github.com/firecrawl/firecrawl":
        "https://www.firecrawl.dev/",
    "https://github.com/Significant-Gravitas/AutoGPT":
        "https://agpt.co/",
    "https://github.com/anomalyco/opencode":
        "https://opencode.ai/",
    "https://github.com/Snailclimb/JavaGuide":
        "https://javaguide.cn/",
    "https://github.com/f/prompts.chat":
        "https://prompts.chat/",
    "https://github.com/huggingface/transformers":
        "https://deepwiki.com/huggingface/transformers",
}

_probe_cache: dict[str, bool] = {}


def gitcode_trending_url(owner: str, repo: str) -> str:
    """Build a GitCode Trending mirror URL for a GitHub repo."""
    del owner  # path uses repo-name prefix only
    prefix = repo[:2].lower() if len(repo) >= 2 else repo.lower()
    return f"https://gitcode.com/GitHub_Trending/{prefix}/{repo}"


def deepwiki_url(owner: str, repo: str) -> str:
    """Build a DeepWiki URL for a GitHub repo."""
    return f"https://deepwiki.com/{owner}/{repo}"


def _fetch_text(url: str) -> str | None:
    """GET URL body as text; return None on failure."""
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "ai-knowledge-base-landing-probe/1.0"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=_PROBE_TIMEOUT) as resp:
            return resp.read(200_000).decode("utf-8", errors="ignore")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        logger.debug("Landing probe failed for %s: %s", url, exc)
        return None


def is_deepwiki_indexed(owner: str, repo: str) -> bool:
    """Return True if DeepWiki appears to have indexed the repo.

    Unindexed repos return a thin SPA shell that renders
    "Repository Not Indexed" in the browser.
    """
    cache_key = f"dw:{owner}/{repo}".lower()
    if cache_key in _probe_cache:
        return _probe_cache[cache_key]

    text = _fetch_text(deepwiki_url(owner, repo))
    if text is None:
        _probe_cache[cache_key] = False
        return False

    lowered = text.lower()
    if (
        "repository not indexed" in lowered
        or "hasn't been indexed" in lowered
        or "has not been indexed" in lowered
    ):
        _probe_cache[cache_key] = False
        return False
    # Indexed pages embed "Last indexed"; thin SPA shells do not.
    indexed = ("Last indexed" in text) or (len(text) >= _DEEPWIKI_INDEXED_MIN_BYTES)
    _probe_cache[cache_key] = indexed
    return indexed


def is_gitcode_populated(owner: str, repo: str) -> bool:
    """Return True if the GitCode Trending mirror has real repo content.

    Many ``GitHub_Trending/{prefix}/{repo}`` URLs are empty placeholder shells
    (``RepoEmptyState``). Populated mirrors expose a non-empty meta description.
    """
    cache_key = f"gc:{owner}/{repo}".lower()
    if cache_key in _probe_cache:
        return _probe_cache[cache_key]

    text = _fetch_text(gitcode_trending_url(owner, repo))
    if text is None:
        _probe_cache[cache_key] = False
        return False

    m = _META_DESC_RE.search(text)
    desc = (m.group(1) if m else "").strip()
    # Real mirrors have a project description; empty shells omit it.
    populated = len(desc) >= 12
    _probe_cache[cache_key] = populated
    return populated


def rewrite_landing_url(url: str, *, probe: bool = True) -> str:
    """Map a source URL to a WeChat-openable HTML page.

    Args:
        url: Original article / repository URL.
        probe: If True, probe DeepWiki/GitCode before rewriting. If False,
            skip probes and keep the GitHub URL for unknown repos (tests).

    Returns:
        Rewritten landing URL; falls back to the original GitHub URL when
        no safe mirror is available.
    """
    url = (url or "").strip()
    if not url:
        return url
    if url.startswith("www."):
        url = "https://" + url

    key = url.rstrip("/")
    m_js = _JSDELIVR_README_RE.match(key)
    if m_js:
        key = f"https://github.com/{m_js.group(1)}/{m_js.group(2)}"

    if key in KNOWN_LANDINGS:
        return KNOWN_LANDINGS[key]

    m = _GITHUB_REPO_RE.match(key)
    if not m:
        return url

    owner, repo = m.group(1), m.group(2)
    if not probe:
        return key

    if is_deepwiki_indexed(owner, repo):
        return deepwiki_url(owner, repo)
    if is_gitcode_populated(owner, repo):
        return gitcode_trending_url(owner, repo)
    # Prefer the real GitHub page over empty DeepWiki / empty GitCode shells.
    return key


def github_og_image(source_url: str) -> str:
    """Return GitHub Open Graph image URL when source is a repo page."""
    m = _GITHUB_REPO_RE.match((source_url or "").strip().rstrip("/"))
    if not m:
        return ""
    return f"https://opengraph.githubassets.com/1/{m.group(1)}/{m.group(2)}"
