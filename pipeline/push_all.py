"""Push all articles to WeChat Work webhook in batches (max 8 per request)."""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

import httpx

from landing_urls import github_og_image, rewrite_landing_url


logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
ARTICLES_DIR = BASE_DIR / "knowledge" / "articles"


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def load_all_articles() -> list[dict[str, Any]]:
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
        if isinstance(data, dict) and data.get("source_url") and data.get("title"):
            articles.append(data)
    return articles


def to_news_articles(articles: list[dict[str, Any]]) -> list[dict[str, str]]:
    news: list[dict[str, str]] = []
    for art in articles:
        source_url = str(art.get("source_url") or "")
        landing = rewrite_landing_url(source_url)
        summary = str(art.get("summary") or "").strip()[:120]
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
            "title": str(art.get("title") or "").strip(),
            "url": landing,
            "description": " · ".join(desc_bits)[:200],
        }
        pic = github_og_image(source_url)
        if pic:
            entry["picurl"] = pic
        news.append(entry)
    return news


def push_news_batch(articles: list[dict[str, str]], webhook: str) -> dict[str, Any]:
    payload = {"msgtype": "news", "news": {"articles": articles}}
    with httpx.Client(timeout=30.0) as client:
        resp = client.post(
            webhook,
            headers={"Content-Type": "application/json; charset=utf-8"},
            json=payload,
        )
        resp.raise_for_status()
        return resp.json()


def main() -> int:
    setup_logging()
    webhook = os.environ.get("WECHAT_WEBHOOK_URL")
    if not webhook:
        logger.error("WECHAT_WEBHOOK_URL not set")
        return 1

    articles = load_all_articles()
    total = len(articles)
    logger.info("Loaded %d articles", total)

    if not articles:
        logger.warning("No articles found")
        return 0

    news_list = to_news_articles(articles)
    batch_size = 8
    batches = [news_list[i:i + batch_size] for i in range(0, len(news_list), batch_size)]
    logger.info("Splitting into %d batches (max %d per batch)", len(batches), batch_size)

    success_count = 0
    for i, batch in enumerate(batches, 1):
        logger.info("Pushing batch %d/%d (%d items)...", i, len(batches), len(batch))
        try:
            result = push_news_batch(batch, webhook)
            errcode = result.get("errcode", -1)
            if errcode == 0:
                logger.info("Batch %d success: %s", i, result)
                success_count += len(batch)
            else:
                logger.error("Batch %d failed: %s", i, result)
        except Exception as e:
            logger.error("Batch %d exception: %s", i, e)
        
        if i < len(batches):
            time.sleep(1)

    logger.info("Push completed: %d/%d articles sent successfully", success_count, total)
    return 0 if success_count == total else 1


if __name__ == "__main__":
    sys.exit(main())