"""Select Top-N tech/tool digests and push WeChat news cards.

Ranking prefers LLM ``score`` (quality) + GitHub ``popularity`` (stars /
收藏热度), with a bias toward coding tools and hands-on tip content.

Usage:
    python pipeline/digest_push.py --limit 3
    python pipeline/digest_push.py --limit 3 --dry-run
    python pipeline/digest_push.py --limit 3 --days 3
    python pipeline/digest_push.py --mark-all-pushed
    python pipeline/digest_push.py --force   # ignore history, allow re-push
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

from landing_urls import github_og_image, rewrite_landing_url


logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
ARTICLES_DIR = BASE_DIR / "knowledge" / "articles"
RAW_DIR = BASE_DIR / "knowledge" / "raw"
PUSH_HISTORY_FILE = BASE_DIR / "knowledge" / "push_history.json"

# Tags that signal tools / developer technique content
TECH_TOOL_TAGS = {
    "agent",
    "coding assistant",
    "terminal",
    "toolkit",
    "api",
    "rag",
    "llm",
    "automation",
    "cli",
    "devtools",
    "spec-driven development",
    "openapi",
    "coding",
    "ide",
    "workflow",
    "skills",
}

TECH_KEYWORDS = re.compile(
    r"(工具|技巧|编程|编码|Agent|CLI|终端|RAG|编排|助手|本地模型|"
    r"工作流|开发|IDE|Cursor|Claude Code|prompt|技能|SDK|API)",
    re.IGNORECASE,
)

# Soft / interview / broad learning content — demote in ranking
DEMOTE_KEYWORDS = re.compile(
    r"(面试|八股|学习路线|知识体系|入门教程合集)",
)

TITLE_HINTS: dict[str, str] = {
    "anthropics/claude-code": "Claude Code：终端原生 AI 编程代理",
    "github/spec-kit": "GitHub Spec Kit：规范驱动开发工具包",
    "msitarzewski/agency-agents": "Agency Agents：AI 专员一键装进 IDE",
    "re4/LibreCode": "LibreCode：基于 Ollama 的开源类 Cursor",
    "open-webui/open-webui": "Open WebUI：私有化 AI 对话界面",
    "langflow-ai/langflow": "Langflow：可视化编排 LLM / Agent",
    "ultraworkers/claw-code": "Claw Code：开源 Agent 编程运行时",
    "anthropics/skills": "Anthropic Skills：可复用 Agent 技能包",
    "langgenius/dify": "Dify：生产级 Agent / RAG 平台",
    "firecrawl/firecrawl": "Firecrawl：网页转 LLM 友好数据",
    "obra/superpowers": "Superpowers：Agent 超级能力技能集",
    "anomalyco/opencode": "OpenCode：开源 AI 编程助手",
    "ollama/ollama": "Ollama：本地大模型一键运行",
    "n8n-io/n8n": "n8n：开源 AI 工作流自动化",
    "openclaw/openclaw": "OpenClaw：跨平台个人 AI 助手",
    "Significant-Gravitas/AutoGPT": "AutoGPT：自主 Agent 框架",
    "NousResearch/hermes-agent": "Hermes Agent：开源智能体工具",
    "affaan-m/ECC": "ECC：Claude Code 性能与技能增强",
    "affaan-m/everything-claude-code": "Everything Claude Code：扩展合集",
    "multica-ai/andrej-karpathy-skills": "Karpathy Skills：编码技能合集",
    "x1xhlol/system-prompts-and-models-of-ai-tools": "AI 工具 System Prompt 合集",
    "huggingface/transformers": "Transformers：HF 模型与推理库",
    "AUTOMATIC1111/stable-diffusion-webui": "SD WebUI：本地绘图工具",
    "f/prompts.chat": "prompts.chat：提示词社区",
    "Snailclimb/JavaGuide": "JavaGuide：Java 学习知识库",
}


def setup_logging(verbose: bool) -> None:
    """Configure logging."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _parse_created_at(value: str) -> datetime | None:
    """Parse article created_at into aware datetime."""
    if not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _load_popularity_index() -> dict[str, int]:
    """Build title/url -> popularity from newest raw dumps."""
    index: dict[str, int] = {}
    if not RAW_DIR.is_dir():
        return index
    files = sorted(RAW_DIR.glob("raw-*.json"), reverse=True)[:10]
    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(data, list):
            continue
        for item in data:
            if not isinstance(item, dict):
                continue
            pop = int(item.get("popularity") or 0)
            if pop <= 0:
                continue
            url = item.get("url")
            if url and str(url) not in index:
                index[str(url)] = pop
    return index


def load_articles() -> list[dict[str, Any]]:
    """Load all knowledge articles with optional popularity backfill."""
    pop_index = _load_popularity_index()
    articles: list[dict[str, Any]] = []
    if not ARTICLES_DIR.is_dir():
        return articles
    for path in sorted(ARTICLES_DIR.glob("*.json")):
        if path.name == "index.json":
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(data, dict):
            continue
        if not data.get("source_url") or not data.get("title"):
            continue
        pop = int(data.get("popularity") or 0)
        if pop <= 0:
            pop = pop_index.get(str(data.get("source_url")), 0)
            data["popularity"] = pop
        articles.append(data)
    return articles


def tech_tool_boost(article: dict[str, Any]) -> float:
    """Return ranking boost for technical / tool-tip oriented items."""
    tags = {str(t).strip().lower() for t in article.get("tags") or []}
    text = f"{article.get('title', '')} {article.get('summary', '')}"
    boost = 0.0
    overlap = tags & TECH_TOOL_TAGS
    boost += 1.5 * len(overlap)
    if TECH_KEYWORDS.search(text):
        boost += 2.0
    if DEMOTE_KEYWORDS.search(text):
        boost -= 3.0
    # Pure interview / general Java guide without AI coding signal
    if "javaguide" in str(article.get("title", "")).lower() and not overlap:
        boost -= 4.0
    return boost


def rank_key(article: dict[str, Any]) -> tuple[float, float, float]:
    """Sort key: higher is better (tech-biased score, log popularity, score)."""
    score = float(article.get("score") or 0)
    popularity = float(article.get("popularity") or 0)
    heat = math.log10(popularity + 1.0)  # stars / 收藏热度
    boost = tech_tool_boost(article)
    # Combined heat: quality *10 + log stars + tech bias
    combined = score * 10.0 + heat * 3.0 + boost
    return (combined, heat, score)


def article_dedupe_key(article: dict[str, Any]) -> str:
    """Stable push-history key — always by source_url (IDs may change)."""
    url = str(article.get("source_url") or "").strip().rstrip("/")
    if url:
        return f"url:{url.lower()}"
    art_id = str(article.get("id") or "").strip()
    return f"id:{art_id}"


def load_push_history() -> dict[str, Any]:
    """Load push history JSON; return empty structure if missing."""
    empty: dict[str, Any] = {"version": 2, "items": {}, "runs": []}
    if not PUSH_HISTORY_FILE.is_file():
        return empty
    try:
        data = json.loads(PUSH_HISTORY_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to read push history: %s", e)
        return empty
    if not isinstance(data, dict):
        return empty
    data.setdefault("version", 2)
    data.setdefault("items", {})
    data.setdefault("runs", [])
    if not isinstance(data["items"], dict):
        data["items"] = {}
    if not isinstance(data["runs"], list):
        data["runs"] = []
    # Migrate legacy id:* keys → url:* when source_url is present
    migrated: dict[str, Any] = {}
    for key, rec in list(data["items"].items()):
        if not isinstance(rec, dict):
            continue
        url = str(rec.get("source_url") or "").strip().rstrip("/").lower()
        new_key = f"url:{url}" if url else key
        migrated[new_key] = rec
    data["items"] = migrated
    return data


def save_push_history(history: dict[str, Any]) -> None:
    """Persist push history to knowledge/push_history.json."""
    PUSH_HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    PUSH_HISTORY_FILE.write_text(
        json.dumps(history, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    logger.info(
        "Push history saved: %s (%d items)",
        PUSH_HISTORY_FILE,
        len(history.get("items", {})),
    )


def record_push(
    history: dict[str, Any],
    articles: list[dict[str, Any]],
    news: list[dict[str, str]],
) -> dict[str, Any]:
    """Append successful push entries into history."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    run_ids: list[str] = []
    for art, card in zip(articles, news):
        key = article_dedupe_key(art)
        run_ids.append(key)
        history["items"][key] = {
            "id": art.get("id"),
            "title": art.get("title"),
            "source_url": art.get("source_url"),
            "landing_url": card.get("url"),
            "display_title": card.get("title"),
            "pushed_at": now,
        }
        _mark_article_file_published(art)
    history["runs"].append(
        {
            "pushed_at": now,
            "count": len(run_ids),
            "keys": run_ids,
            "titles": [c.get("title") for c in news],
        }
    )
    if len(history["runs"]) > 200:
        history["runs"] = history["runs"][-200:]
    return history


def _mark_article_file_published(article: dict[str, Any]) -> None:
    """Set article JSON status to published after a successful push."""
    target_id = str(article.get("id") or "")
    target_url = str(article.get("source_url") or "").strip().rstrip("/").lower()
    if not ARTICLES_DIR.is_dir():
        return
    for path in ARTICLES_DIR.glob("*.json"):
        if path.name == "index.json":
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(data, dict):
            continue
        match_id = target_id and str(data.get("id") or "") == target_id
        match_url = (
            target_url
            and str(data.get("source_url") or "").strip().rstrip("/").lower()
            == target_url
        )
        if not (match_id or match_url):
            continue
        if data.get("status") == "published":
            return
        data["status"] = "published"
        data["updated_at"] = datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S+00:00"
        )
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        logger.debug("Marked published: %s", path.name)
        return


def is_already_pushed(article: dict[str, Any], history: dict[str, Any]) -> bool:
    """Return True if this article was pushed or marked published."""
    if str(article.get("status") or "").lower() == "published":
        return True

    items = history.get("items") or {}
    key = article_dedupe_key(article)
    if key in items:
        return True

    url = str(article.get("source_url") or "").strip().rstrip("/").lower()
    art_id = str(article.get("id") or "").strip()
    for rec in items.values():
        if not isinstance(rec, dict):
            continue
        if art_id and str(rec.get("id") or "") == art_id:
            return True
        rec_url = str(rec.get("source_url") or "").strip().rstrip("/").lower()
        if url and rec_url == url:
            return True
    return False


def select_digest(
    articles: list[dict[str, Any]],
    limit: int = 3,
    days: int | None = 3,
    history: dict[str, Any] | None = None,
    skip_pushed: bool = True,
) -> list[dict[str, Any]]:
    """Filter recent tech-leaning articles and return Top-N by heat.

    Args:
        articles: Full article list.
        limit: Max items to return (default 3).
        days: Only consider articles created within N days; None = all.
        history: Push history for dedupe.
        skip_pushed: If True, exclude already-pushed / published articles.

    Returns:
        Sorted shortlist (highest first). Never re-includes pushed items
        when ``skip_pushed`` is True — even if the recent window is sparse.
    """
    history = history or {"items": {}}
    now = datetime.now(timezone.utc)

    def _tech_ok(art: dict[str, Any]) -> bool:
        boost = tech_tool_boost(art)
        score = float(art.get("score") or 0)
        if boost < -2 and score < 8:
            return False
        return True

    def _not_pushed(art: dict[str, Any]) -> bool:
        return not (skip_pushed and is_already_pushed(art, history))

    def _in_days(art: dict[str, Any]) -> bool:
        if days is None:
            return True
        created = _parse_created_at(str(art.get("created_at") or ""))
        if created is None:
            return False
        return (now - created) <= timedelta(days=days)

    skipped = sum(1 for art in articles if not _not_pushed(art))
    if skipped:
        logger.info("Skipped %d already-pushed/published articles", skipped)

    pool = [
        art
        for art in articles
        if _not_pushed(art) and _in_days(art) and _tech_ok(art)
    ]

    if len(pool) < limit:
        logger.info(
            "Only %d unpushed articles in last %s days; "
            "expanding to all unpushed (still skip history)",
            len(pool),
            days,
        )
        pool = [art for art in articles if _not_pushed(art) and _tech_ok(art)]

    pool.sort(key=rank_key, reverse=True)
    return pool[:limit]


def mark_articles_pushed(
    articles: list[dict[str, Any]],
    history: dict[str, Any],
) -> dict[str, Any]:
    """Mark articles as pushed without sending (backfill history)."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    keys: list[str] = []
    for art in articles:
        key = article_dedupe_key(art)
        keys.append(key)
        history["items"][key] = {
            "id": art.get("id"),
            "title": art.get("title"),
            "source_url": art.get("source_url"),
            "landing_url": rewrite_landing_url(str(art.get("source_url") or "")),
            "display_title": display_title(art),
            "pushed_at": now,
            "note": "marked_without_send",
        }
        _mark_article_file_published(art)
    history["runs"].append(
        {
            "pushed_at": now,
            "count": len(keys),
            "keys": keys,
            "titles": [display_title(a) for a in articles],
            "note": "mark_only",
        }
    )
    if len(history["runs"]) > 200:
        history["runs"] = history["runs"][-200:]
    return history


def display_title(article: dict[str, Any]) -> str:
    """Human-readable Chinese-leaning title for the news card."""
    raw = str(article.get("title") or "").strip()
    if raw in TITLE_HINTS:
        return TITLE_HINTS[raw]
    summary = str(article.get("summary") or "").strip()
    if summary:
        # First clause of Chinese summary as titleish fallback
        first = re.split(r"[。；;\n]", summary, maxsplit=1)[0].strip()
        if 8 <= len(first) <= 40:
            return first
    return raw


def to_news_articles(articles: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Convert digest items to WeChat news payload articles."""
    news: list[dict[str, str]] = []
    for art in articles:
        source_url = str(art.get("source_url") or "")
        landing = rewrite_landing_url(source_url)
        summary = re.sub(r"<[^>]+>", "", str(art.get("summary") or ""))
        summary = re.sub(r"\s+", " ", summary).strip()[:120]
        score = art.get("score", "")
        popularity = int(art.get("popularity") or 0)
        desc_bits = []
        if score != "":
            desc_bits.append(f"评分 {score}")
        if popularity > 0:
            desc_bits.append(f"★ {popularity:,}")
        if summary:
            desc_bits.append(summary)
        entry = {
            "title": display_title(art),
            "url": landing,
            "description": " · ".join(desc_bits)[:200],
        }
        pic = github_og_image(source_url)
        if pic:
            entry["picurl"] = pic
        news.append(entry)
    return news


def resolve_webhook_url() -> str:
    """Load webhook from env or local Cursor skill config."""
    url = (os.environ.get("WECHAT_WEBHOOK_URL") or "").strip()
    if url:
        return url
    config = Path.home() / ".cursor/skills/wechat-notify/config.local.env"
    if config.is_file():
        for line in config.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            if key.strip() == "WECHAT_WEBHOOK_URL":
                return val.strip().strip("'").strip('"')
    return ""


def push_news(articles: list[dict[str, str]], webhook: str) -> dict[str, Any]:
    """POST a news card to the WeChat Work webhook."""
    if not (1 <= len(articles) <= 8):
        raise ValueError(f"news articles must be 1–8 items, got {len(articles)}")
    payload = {"msgtype": "news", "news": {"articles": articles}}
    with httpx.Client(timeout=30.0) as client:
        resp = client.post(
            webhook,
            headers={"Content-Type": "application/json; charset=utf-8"},
            json=payload,
        )
        resp.raise_for_status()
        return resp.json()


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(
        description="Push Top-N tech/tool digests to WeChat Work",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=3,
        help="Number of items to push (default: 3, max: 8)",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=3,
        help="Prefer articles from the last N days (default: 3; 0 = all)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print selection only, do not push",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Ignore push history and allow re-push",
    )
    parser.add_argument(
        "--mark-all-pushed",
        action="store_true",
        help="Mark all existing articles as already pushed (no send)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Debug logging",
    )
    return parser.parse_args()


def main() -> int:
    """Select ranked digest and optionally push."""
    args = parse_args()
    setup_logging(args.verbose)
    limit = max(1, min(args.limit, 8))
    days: int | None = None if args.days == 0 else max(1, args.days)
    history = load_push_history()

    articles = load_articles()
    if not articles:
        logger.error("No articles found in %s", ARTICLES_DIR)
        return 1

    if args.mark_all_pushed:
        history = mark_articles_pushed(articles, history)
        save_push_history(history)
        logger.info("Marked %d articles as pushed", len(articles))
        return 0

    selected = select_digest(
        articles,
        limit=limit,
        days=days,
        history=history,
        skip_pushed=not args.force,
    )
    if not selected:
        logger.info("No new unpushed articles to send; skipping push")
        return 0

    news = to_news_articles(selected)
    logger.info("Selected %d digest items:", len(news))
    for i, (art, card) in enumerate(zip(selected, news), 1):
        logger.info(
            "  [%d] score=%s pop=%s boost=%.1f | %s -> %s",
            i,
            art.get("score"),
            art.get("popularity"),
            tech_tool_boost(art),
            card["title"],
            card["url"],
        )

    if args.dry_run:
        print(json.dumps(news, ensure_ascii=False, indent=2))
        return 0

    webhook = resolve_webhook_url()
    if not webhook:
        logger.error(
            "WECHAT_WEBHOOK_URL not set "
            "(env or ~/.cursor/skills/wechat-notify/config.local.env)"
        )
        return 1

    result = push_news(news, webhook)
    logger.info("WeChat response: %s", result)
    if result.get("errcode", -1) != 0:
        return 1

    history = record_push(history, selected, news)
    save_push_history(history)
    return 0


if __name__ == "__main__":
    sys.exit(main())
