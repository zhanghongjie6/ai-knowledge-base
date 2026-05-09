"""Router pattern with two-layer intent classification.

Layer 1: Keyword fast matching (zero LLM cost).
Layer 2: LLM classification fallback for ambiguous queries.

Intents:
    github_search    — Search GitHub repositories.
    knowledge_query  — Search local knowledge base articles.
    general_chat     — Free-form LLM chat.

Usage:
    from patterns.router import route
    print(route("搜索 GitHub 上的 LLM 项目"))
"""

from __future__ import annotations

import json
import logging
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

# Ensure project root is on sys.path for imports like workflows.model_client
_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from workflows.model_client import chat, chat_json


logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
ARTICLES_DIR = BASE_DIR / "knowledge" / "articles"

GITHUB_API = "https://api.github.com/search/repositories"

# ── Layer 1: Keyword router ─────────────────────────────────────────────

KEYWORD_RULES: dict[str, list[str]] = {
    "github_search": [
        "github", "repo", "仓库", "开源项目", "star", "trending",
        "git仓库", "开源", "项目", "代码库",
    ],
    "knowledge_query": [
        "知识", "文章", "条目", "知识库", "知识条目",
        "article", "articles", "检索", "查找",
    ],
}


def _keyword_classify(query: str) -> str | None:
    """First-layer classification using keyword matching.

    Args:
        query: User input string.

    Returns:
        Intent name if keywords match, None otherwise.
    """
    lower = query.lower()
    for intent, keywords in KEYWORD_RULES.items():
        for kw in keywords:
            if kw in lower:
                logger.debug("Keyword match: '%s' → %s", kw, intent)
                return intent
    return None


# ── Layer 2: LLM router ────────────────────────────────────────────────

LLM_CLASSIFY_SYSTEM = (
    "You are a query classifier. Categorize the user's query into exactly "
    "one of these intents and respond with ONLY the intent name, nothing else.\n\n"
    "intents:\n"
    "  github_search    — looking for GitHub repos, open-source projects, code repositories\n"
    "  knowledge_query  — asking about stored knowledge, articles, previously saved content\n"
    "  general_chat     — general conversation, questions, anything else\n\n"
    "Examples:\n"
    "  query: 帮我找找 RAG 相关的 GitHub 项目\n"
    "  intent: github_search\n"
    "  query: 知识库里有没有关于 Agent 的文章\n"
    "  intent: knowledge_query\n"
    "  query: 什么是大语言模型\n"
    "  intent: general_chat"
)


def _llm_classify(query: str) -> str:
    """Second-layer classification using LLM.

    Args:
        query: User input string.

    Returns:
        Intent name: github_search, knowledge_query, or general_chat.
    """
    try:
        text, _ = chat(query, system_prompt=LLM_CLASSIFY_SYSTEM)
        text = text.strip().lower()
        if "github_search" in text:
            return "github_search"
        if "knowledge_query" in text:
            return "knowledge_query"
        logger.debug("LLM fallback classification: '%s'", text)
    except RuntimeError as e:
        logger.error("LLM classification failed: %s", e)
    return "general_chat"


# ── Intent classifiers ─────────────────────────────────────────────────

def classify_intent(query: str) -> str:
    """Two-layer intent classification.

    Args:
        query: User input string.

    Returns:
        Intent name.
    """
    result = _keyword_classify(query)
    if result is not None:
        return result
    logger.debug("No keyword match, falling back to LLM")
    return _llm_classify(query)


# ── Handlers ────────────────────────────────────────────────────────────

_GITHUB_STOP_WORDS = [
    "搜索", "最近的", "有哪些", "关于", "帮我", "找找", "找到",
    "最近", "最新", "热门", "什么", "看看", "推荐", "有哪些好的",
    "有没有", "怎么", "如何", "哪里", "为什么", "请问",
]


def _clean_github_query(query: str) -> str:
    """Remove natural language prefixes to extract search keywords.

    Args:
        query: Raw user query.

    Returns:
        Cleaned search terms suitable for GitHub API.
    """
    text = query.strip()
    for w in _GITHUB_STOP_WORDS:
        text = text.replace(w, " ")
    words = [w for w in text.split() if w.strip()]
    return " ".join(words) if words else query


def handle_github_search(query: str) -> str:
    """Search GitHub repositories via the Search API.

    Args:
        query: Search keywords.

    Returns:
        Formatted search results as text.
    """
    search_term = _clean_github_query(query)
    if not search_term:
        search_term = query
    encoded = urllib.parse.quote(f"{search_term} in:name,description,topics")
    url = f"{GITHUB_API}?q={encoded}&sort=stars&order=desc&per_page=5"

    token = _github_token()
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github.v3+json"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
    except (urllib.error.URLError, json.JSONDecodeError, OSError) as e:
        logger.error("GitHub API error: %s", e)
        return f"GitHub API 请求失败: {e}"

    items = data.get("items", [])
    if not items:
        return "未找到相关 GitHub 项目。"

    lines: list[str] = [f"找到 {len(items)} 个相关项目：\n"]
    for repo in items:
        name = repo.get("full_name", "unknown")
        stars = repo.get("stargazers_count", 0)
        desc = repo.get("description") or "（无描述）"
        url = repo.get("html_url", "")
        lines.append(f"  ★ {stars}  {name}")
        lines.append(f"     {desc[:120]}")
        lines.append(f"     {url}")
        lines.append("")
    return "\n".join(lines)


def _github_token() -> str | None:
    import os
    return os.environ.get("GITHUB_TOKEN") or None


def handle_knowledge_query(query: str) -> str:
    """Search locally stored knowledge articles.

    Args:
        query: Search keywords.

    Returns:
        Formatted matching articles as text.
    """
    articles = _load_articles()
    if not articles:
        return "知识库为空，暂无文章。"

    keywords = [w.lower() for w in query.split() if len(w) > 1]
    if not keywords:
        keywords = [query.lower()]
    matches: list[dict[str, Any]] = []
    for art in articles:
        title = (art.get("title") or "").lower()
        summary = (art.get("summary") or "").lower()
        tag_text = " ".join(art.get("tags", []) or []).lower()
        haystack = f"{title} {summary} {tag_text}"
        for kw in keywords:
            if kw in haystack:
                matches.append(art)
                break

    if not matches:
        return f"未找到与 «{query}» 相关的知识条目。"

    lines: list[str] = [f"找到 {len(matches)} 条相关知识条目：\n"]
    for art in matches[:5]:
        title = art.get("title", "未知")
        summary = (art.get("summary") or "")[:120]
        tags = ", ".join(art.get("tags", []) or [])
        lines.append(f"  {title}")
        lines.append(f"     {summary}")
        if tags:
            lines.append(f"     标签: {tags}")
        lines.append("")
    return "\n".join(lines)


def _load_articles() -> list[dict[str, Any]]:
    if not ARTICLES_DIR.is_dir():
        return []
    articles: list[dict[str, Any]] = []
    for fpath in sorted(ARTICLES_DIR.glob("*.json")):
        try:
            data = json.loads(fpath.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                articles.append(data)
        except (json.JSONDecodeError, OSError):
            continue
    return articles


def handle_general_chat(query: str) -> str:
    """Answer a general question via LLM chat.

    Args:
        query: User question.

    Returns:
        LLM response text.
    """
    system = (
        "你是一个 AI 知识助手。请用中文回答用户的问题。"
        "回答应简洁、准确、有帮助。"
    )
    try:
        text, _ = chat(query, system_prompt=system)
        return text
    except RuntimeError as e:
        logger.error("Chat failed: %s", e)
        return f"抱歉，回答失败: {e}"


# ── Router ──────────────────────────────────────────────────────────────

INTENT_HANDLERS: dict[str, Any] = {
    "github_search": handle_github_search,
    "knowledge_query": handle_knowledge_query,
    "general_chat": handle_general_chat,
}


def route(query: str) -> str:
    """Route a query to the appropriate handler and return the result.

    Two-layer classification:
        1. Keyword matching (zero cost).
        2. LLM classification fallback.

    Args:
        query: User input string.

    Returns:
        Handler response text.
    """
    intent = classify_intent(query)
    logger.info("Query: '%s' → intent: %s", query[:60], intent)

    handler = INTENT_HANDLERS.get(intent, handle_general_chat)
    return handler(query)


# ── CLI test entry ──────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    queries = sys.argv[1:] if len(sys.argv) > 1 else [
        "搜索 GitHub 上的 RAG 项目",
        "知识库里有哪些关于 Agent 的文章",
        "什么是大语言模型",
    ]

    for q in queries:
        print(f"\n{'=' * 60}")
        print(f"QUERY: {q}")
        print(f"{'=' * 60}")
        result = route(q)
        print(result)
