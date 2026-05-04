"""Four-stage knowledge base automation pipeline.

Collects AI-related content from GitHub and RSS sources,
analyzes via LLM, organizes (dedup + format), and saves to knowledge/articles/.

Usage:
    python pipeline/pipeline.py --sources github,rss --limit 20
    python pipeline/pipeline.py --sources github --limit 5
    python pipeline/pipeline.py --sources rss --limit 10 --dry-run
    python pipeline/pipeline.py --verbose
"""

import argparse
import json
import logging
import os
import re
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from model_client import create_provider, chat_with_retry


logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
RAW_DIR = BASE_DIR / "knowledge" / "raw"
ARTICLES_DIR = BASE_DIR / "knowledge" / "articles"
RSS_CONFIG = BASE_DIR / "pipeline" / "rss_sources.yaml"

GITHUB_SEARCH_URL = "https://api.github.com/search/repositories"
GITHUB_QUERY = "AI OR LLM OR Agent OR RAG in:name,description,topics"

RSS_ITEM_RE = re.compile(
    r"<item>.*?<title>(.*?)</title>.*?<link>(.*?)</link>.*?<description>(.*?)</description>.*?</item>",
    re.DOTALL | re.IGNORECASE,
)

HTML_TAG_RE = re.compile(r"<[^>]+>")

SOURCE_CODE_MAP = {
    "github_trending": "gh",
    "hacker_news": "hn",
    "rss": "rss",
}

ANALYSIS_PROMPT = """You are an AI knowledge base analyst. Analyze the following tech item and return a JSON object with these fields:
- "summary": Chinese summary (150-200 characters), covering core value, technical highlights, and use cases
- "highlights": array of 1-3 key highlights in Chinese
- "score": integer 1-10 following this scale:
    9-10 = groundbreaking, may reshape the industry
    7-8 = directly useful, can be applied immediately
    5-6 = worth knowing, broadens horizons
    1-4 = low value, skip-worthy
- "tags": array of 2-5 tags in English, e.g. ["LLM", "Agent", "RAG"]

Item:
Title: {title}
URL: {url}
Source: {source}
Summary: {desc}

Respond with ONLY the JSON object, no markdown fences, no extra text."""


def setup_logging(verbose: bool) -> None:
    """Configure logging level and format."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def now_iso() -> str:
    """Return current UTC time in ISO 8601 format."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def slugify(text: str) -> str:
    """Convert text to a URL-safe filename slug."""
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "-", text)
    text = text.strip("-")
    return text[:80] or "untitled"


def strip_html(text: str) -> str:
    """Remove HTML tags from a string."""
    return HTML_TAG_RE.sub("", text).strip()


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="AI Knowledge Base Automation Pipeline",
    )
    parser.add_argument(
        "--sources",
        default="github,rss",
        help="Comma-separated list of sources to collect from (github, rss). Default: github,rss",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Maximum number of items to collect. Default: 20",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run pipeline without writing files",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug-level logging",
    )
    return parser.parse_args()


# ── Step 1: Collect ──────────────────────────────────────────────────────

def collect_github(limit: int, token: str | None) -> list[dict[str, Any]]:
    """Collect AI-related repositories from GitHub Search API.

    Args:
        limit: Maximum number of repos to return.
        token: Optional GitHub personal access token.

    Returns:
        List of raw item dicts with title, url, source, popularity, summary.
    """
    logger.info("Collecting from GitHub Search API (limit=%d)", limit)
    headers = {"Accept": "application/vnd.github.v3+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    params = {
        "q": GITHUB_QUERY,
        "sort": "stars",
        "order": "desc",
        "per_page": min(limit, 100),
    }

    items: list[dict[str, Any]] = []
    try:
        with httpx.Client(timeout=30.0) as client:
            resp = client.get(GITHUB_SEARCH_URL, headers=headers, params=params)
            resp.raise_for_status()
            data = resp.json()
    except httpx.RequestError as e:
        logger.error("GitHub API request failed: %s", e)
        return items

    for repo in data.get("items", [])[:limit]:
        desc = repo.get("description") or ""
        items.append({
            "title": repo["full_name"],
            "url": repo["html_url"],
            "source": "github_trending",
            "popularity": repo.get("stargazers_count", 0),
            "summary": desc.strip()[:200],
        })

    logger.info("GitHub: collected %d items", len(items))
    return items


def collect_rss(limit: int) -> list[dict[str, Any]]:
    """Collect items from enabled RSS feeds in the config file.

    Args:
        limit: Maximum number of items to return across all feeds.

    Returns:
        List of raw item dicts.
    """
    logger.info("Collecting from RSS feeds (limit=%d)", limit)

    if not RSS_CONFIG.is_file():
        logger.warning("RSS config not found: %s", RSS_CONFIG)
        return []

    try:
        config = json.loads(RSS_CONFIG.read_text(encoding="utf-8"))
        # Handle both .json and .yaml - if JSON fails, try YAML-like parse
    except (json.JSONDecodeError, OSError):
        config = _parse_yaml_simple(RSS_CONFIG.read_text(encoding="utf-8"))

    sources = config.get("sources", [])
    enabled = [s for s in sources if s.get("enabled", False)]

    if not enabled:
        logger.info("No enabled RSS feeds found")
        return []

    items: list[dict[str, Any]] = []
    per_feed = max(1, limit // len(enabled))

    for feed in enabled:
        feed_items = _fetch_rss_feed(feed, per_feed)
        items.extend(feed_items)
        if len(items) >= limit:
            break

    logger.info("RSS: collected %d items from %d feeds", len(items), len(enabled))
    return items[:limit]


def _parse_yaml_simple(text: str) -> dict[str, Any]:
    """Parse a simple YAML file (sources list only) without PyYAML dependency.

    Handles the structure used in rss_sources.yaml.
    """
    sources: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    for line in text.splitlines():
        stripped = line.strip()
        if stripped == "sources:":
            continue
        if stripped.startswith("- name:"):
            current = {"name": stripped.split(":", 1)[1].strip().strip('"')}
            sources.append(current)
        elif current is not None and ":" in stripped:
            key, _, value = stripped.partition(":")
            key = key.strip()
            value = value.strip().strip('"')
            if value.lower() == "true":
                value = True
            elif value.lower() == "false":
                value = False
            elif value.isdigit():
                value = int(value)
            current[key] = value

    return {"sources": sources}


def _fetch_rss_feed(feed: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    """Fetch and parse a single RSS feed.

    Args:
        feed: Dict with keys: name, url, category.
        limit: Max items to return from this feed.

    Returns:
        List of raw item dicts.
    """
    url = feed.get("url", "")
    name = feed.get("name", "unknown")
    if not url:
        logger.warning("RSS feed '%s' has no URL, skipping", name)
        return []

    try:
        with httpx.Client(timeout=30.0, follow_redirects=True) as client:
            resp = client.get(url)
            resp.raise_for_status()
            body = resp.text
    except httpx.RequestError as e:
        logger.warning("RSS feed '%s' request failed: %s", name, e)
        return []

    items: list[dict[str, Any]] = []
    for match in list(RSS_ITEM_RE.finditer(body))[:limit]:
        title = strip_html(match.group(1)).strip()
        link = strip_html(match.group(2)).strip()
        desc = strip_html(match.group(3)).strip()[:200]

        if not title or not link:
            continue

        items.append({
            "title": title,
            "url": link,
            "source": "hacker_news" if "hackernews" in url.lower() or "hnrss" in url.lower() else "rss",
            "popularity": 0,
            "summary": desc[:200],
            "feed_name": name,
        })

    return items


# ── Step 2: Analyze ──────────────────────────────────────────────────────

def analyze_items(
    items: list[dict[str, Any]],
    provider: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Analyze each raw item using the LLM provider.

    Args:
        items: Raw items from collect step.
        provider: LLM provider config, or None to skip analysis.

    Returns:
        Items enriched with an 'analysis' dict, or original items if no provider.
    """
    if not items:
        return items
    if provider is None:
        logger.warning("No LLM provider available, skipping analysis step")
        return items

    logger.info("Analyzing %d items via LLM (%s)...", len(items), provider["provider"])

    analyzed: list[dict[str, Any]] = []
    for i, item in enumerate(items):
        logger.debug("Analyzing [%d/%d]: %s", i + 1, len(items), item["title"])
        prompt = ANALYSIS_PROMPT.format(
            title=item["title"],
            url=item["url"],
            source=item["source"],
            desc=item.get("summary", "")[:200],
        )

        try:
            response = chat_with_retry(provider, [
                {"role": "user", "content": prompt},
            ])
            analysis = _parse_analysis_response(response)
        except (RuntimeError, json.JSONDecodeError) as e:
            logger.warning("Analysis failed for '%s': %s", item["title"], e)
            analysis = {
                "summary": item.get("summary", ""),
                "highlights": [],
                "score": 5,
                "tags": [],
            }

        item["analysis"] = analysis
        analyzed.append(item)

    success = sum(1 for a in analyzed if a.get("analysis", {}).get("score", 0) > 0)
    logger.info("Analyzed %d/%d items successfully", success, len(analyzed))
    return analyzed


def _parse_analysis_response(response: str) -> dict[str, Any]:
    """Parse the LLM response into an analysis dict.

    Handles JSON with or without markdown code fences.
    """
    text = response.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        text = text.strip()

    data = json.loads(text)

    return {
        "summary": data.get("summary", ""),
        "highlights": data.get("highlights", []),
        "score": data.get("score", 5),
        "tags": data.get("tags", []),
    }


# ── Step 3: Organize ─────────────────────────────────────────────────────

def organize_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deduplicate against existing articles and format to standard schema.

    Args:
        items: Analyzed items from Step 2.

    Returns:
        List of article dicts in standard knowledge entry format.
    """
    existing = _load_existing_articles()
    seen: set[tuple[str, str]] = set()
    for art in existing:
        seen.add((art.get("title", ""), art.get("source_url", "")))

    organized: list[dict[str, Any]] = []
    for item in items:
        dedup_key = (item.get("title", ""), item.get("url", ""))
        if dedup_key in seen:
            logger.info("Skipping duplicate: %s", item["title"])
            continue

        article = _format_article(item)
        organized.append(article)
        seen.add(dedup_key)

    logger.info("Organized: %d new articles (skipped %d duplicates)", len(organized), len(items) - len(organized))
    return organized


def _load_existing_articles() -> list[dict[str, Any]]:
    """Load all existing articles from the articles directory."""
    articles: list[dict[str, Any]] = []
    if not ARTICLES_DIR.is_dir():
        return articles
    for fpath in sorted(ARTICLES_DIR.glob("*.json")):
        try:
            data = json.loads(fpath.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                articles.append(data)
        except (json.JSONDecodeError, OSError):
            continue
    return articles


def _format_article(item: dict[str, Any]) -> dict[str, Any]:
    """Convert a raw/analyzed item into standard article format.

    Args:
        item: Item dict with optional 'analysis' sub-dict.

    Returns:
        Article dict matching the standard schema.
    """
    analysis = item.get("analysis", {})
    source_type = item["source"]
    tags = list(analysis.get("tags", []))

    if source_type == "github_trending":
        if "github" not in tags:
            tags.append("github")
    else:
        source_tag = "hackernews" if source_type == "hacker_news" else source_type
        if source_tag not in tags:
            tags.append(source_tag)

    article_id = str(uuid.uuid4())
    timestamp = now_iso()

    return {
        "id": article_id,
        "title": item["title"],
        "source_url": item["url"],
        "source_type": source_type,
        "summary": analysis.get("summary", item.get("summary", "")),
        "tags": tags,
        "status": "pending",
        "created_at": timestamp,
        "updated_at": timestamp,
    }


# ── Step 4: Save ─────────────────────────────────────────────────────────

def save_articles(articles: list[dict[str, Any]], dry_run: bool = False) -> list[Path]:
    """Save articles as individual JSON files to knowledge/articles/.

    Args:
        articles: List of formatted article dicts.
        dry_run: If True, only log what would be saved without writing.

    Returns:
        List of file paths that were (or would be) written.
    """
    if not articles:
        logger.info("No articles to save")
        return []

    if dry_run:
        logger.info("DRY RUN: would save %d articles", len(articles))
        for article in articles:
            logger.info("  Would write: %s → %s", article["title"], article["source_url"])
        return []

    ARTICLES_DIR.mkdir(parents=True, exist_ok=True)

    written: list[Path] = []
    for article in articles:
        source_code = SOURCE_CODE_MAP.get(article["source_type"], article["source_type"])
        date_part = article["created_at"][:10]
        slug = slugify(article["title"])
        filename = f"{date_part}-{source_code}-{slug}.json"
        filepath = ARTICLES_DIR / filename

        if filepath.exists():
            logger.warning("File exists, overwriting: %s", filename)

        filepath.write_text(
            json.dumps(article, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        written.append(filepath)
        logger.debug("Saved: %s", filename)

    logger.info("Saved %d articles to %s", len(written), ARTICLES_DIR)
    return written


# ── Main ─────────────────────────────────────────────────────────────────

def main() -> int:
    """Execute the full pipeline: collect → analyze → organize → save."""
    args = parse_args()
    setup_logging(args.verbose)

    logger.info("Pipeline started (sources=%s, limit=%d, dry_run=%s)",
                args.sources, args.limit, args.dry_run)

    selected = [s.strip() for s in args.sources.split(",") if s.strip()]

    # Step 1: Collect
    all_items: list[dict[str, Any]] = []
    remaining = args.limit

    if "github" in selected:
        gh_token = os.environ.get("GITHUB_TOKEN")
        gh_items = collect_github(remaining, gh_token)
        all_items.extend(gh_items)
        remaining -= len(gh_items)

    if "rss" in selected and remaining > 0:
        rss_items = collect_rss(remaining)
        all_items.extend(rss_items)

    if not all_items:
        logger.warning("No items collected, exiting")
        return 0

    # Save raw data
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    raw_file = RAW_DIR / f"raw-{timestamp}.json"
    raw_file.write_text(
        json.dumps(all_items, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    logger.info("Raw data saved: %s (%d items)", raw_file, len(all_items))

    # Step 2: Analyze
    provider: dict[str, Any] | None = None
    try:
        provider = create_provider()
    except ValueError as e:
        logger.warning("LLM provider setup failed: %s", e)

    analyzed = analyze_items(all_items, provider)

    # Step 3: Organize
    organized = organize_items(analyzed)

    # Step 4: Save
    save_articles(organized, dry_run=args.dry_run)

    logger.info("Pipeline completed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
