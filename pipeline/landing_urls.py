"""Rewrite destination URLs for WeChat-friendly HTML landings.

GitHub pages often fail inside 企微; README.md / jsDelivr raw markdown
renders as code. Prefer official docs sites, else DeepWiki HTML pages.
"""

from __future__ import annotations

import re
from typing import Final

_JSDELIVR_README_RE = re.compile(
    r"^https?://cdn\.jsdelivr\.net/gh/([^/\s]+)/([^@/\s]+)@[^/]+/README\.md$",
    re.IGNORECASE,
)
_GITHUB_REPO_RE = re.compile(
    r"^https?://github\.com/([^/\s]+)/([^/\s#?]+)/?$",
)

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


def rewrite_landing_url(url: str) -> str:
    """Map a source URL to a WeChat-openable HTML page.

    Args:
        url: Original article / repository URL.

    Returns:
        Rewritten landing URL; unchanged if no rule matches.
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
    if m:
        return f"https://deepwiki.com/{m.group(1)}/{m.group(2)}"
    return url


def github_og_image(source_url: str) -> str:
    """Return GitHub Open Graph image URL when source is a repo page."""
    m = _GITHUB_REPO_RE.match((source_url or "").strip().rstrip("/"))
    if not m:
        return ""
    return f"https://opengraph.githubassets.com/1/{m.group(1)}/{m.group(2)}"
