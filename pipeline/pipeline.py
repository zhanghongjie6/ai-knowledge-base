"""Four-stage knowledge base automation pipeline.

Collects AI-related content from GitHub and RSS sources,
analyzes via LLM, organizes (dedup + format), and saves to knowledge/articles/.

Usage:
    python pipeline/pipeline.py --sources github,rss --limit 20
    python pipeline/pipeline.py --sources github --limit 5
    python pipeline/pipeline.py --sources rss --limit 10 --dry-run
    python pipeline/pipeline.py --verbose
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

from model_client import TRACKER, create_provider, chat_with_retry


logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
RAW_DIR = BASE_DIR / "knowledge" / "raw"
ARTICLES_DIR = BASE_DIR / "knowledge" / "articles"
RSS_CONFIG = BASE_DIR / "pipeline" / "rss_sources.yaml"
STATE_FILE = RAW_DIR / ".pipeline_state.json"

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

ID_PREFIX_MAP = {
    "github_trending": "github",
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
        "--fresh-days",
        type=int,
        default=7,
        help="GitHub only: prefer repos created/pushed within N days (default: 7)",
    )
    parser.add_argument(
        "--step",
        type=str,
        help="Comma-separated step numbers to run (e.g. 1,2 or 3,4). Default: all steps",
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

def _existing_source_urls() -> set[str]:
    """Return source_url set already saved under knowledge/articles/."""
    urls: set[str] = set()
    if not ARTICLES_DIR.is_dir():
        return urls
    for fpath in ARTICLES_DIR.glob("*.json"):
        if fpath.name == "index.json":
            continue
        try:
            data = json.loads(fpath.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if isinstance(data, dict):
            url = str(data.get("source_url") or "").strip().rstrip("/")
            if url:
                urls.add(url)
    return urls


def _github_search(
    client: httpx.Client,
    headers: dict[str, str],
    query: str,
    sort: str,
    per_page: int,
) -> list[dict[str, Any]]:
    """Execute one GitHub Search API request and map repos to raw items."""
    params = {
        "q": query,
        "sort": sort,
        "order": "desc",
        "per_page": min(per_page, 100),
    }
    try:
        resp = client.get(GITHUB_SEARCH_URL, headers=headers, params=params)
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPStatusError as e:
        logger.error("GitHub API HTTP error: %s", e)
        return []
    except httpx.RequestError as e:
        logger.error("GitHub API request failed: %s", e)
        return []

    items: list[dict[str, Any]] = []
    for repo in data.get("items", []):
        desc = repo.get("description") or ""
        items.append({
            "title": repo["full_name"],
            "url": repo["html_url"],
            "source": "github_trending",
            "popularity": repo.get("stargazers_count", 0),
            "summary": desc.strip()[:200],
            "pushed_at": repo.get("pushed_at") or "",
            "created_at_source": repo.get("created_at") or "",
        })
    return items


def collect_github(
    limit: int,
    token: str | None,
    fresh_days: int = 7,
) -> list[dict[str, Any]]:
    """Collect fresh AI-related repos (recently pushed / newly created).

    Avoids the all-time stars ranking which returns the same mega-repos every day.
    Skips URLs already present in knowledge/articles/.

    Args:
        limit: Maximum number of repos to return.
        token: Optional GitHub personal access token.
        fresh_days: Lookback window for pushed/created filters.

    Returns:
        List of raw item dicts with title, url, source, popularity, summary.
    """
    fresh_days = max(1, fresh_days)
    since = (datetime.now(timezone.utc) - timedelta(days=fresh_days)).strftime(
        "%Y-%m-%d"
    )
    logger.info(
        "Collecting fresh GitHub repos (limit=%d, window=%d days, since=%s)",
        limit,
        fresh_days,
        since,
    )

    headers = {"Accept": "application/vnd.github.v3+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    existing = _existing_source_urls()
    # Over-fetch so we can skip known URLs and still fill ``limit``
    fetch_n = min(100, max(limit * 3, limit + 10))

    # Two complementary queries for daily freshness
    queries = [
        (f"{GITHUB_QUERY} pushed:>{since}", "updated", "recently_pushed"),
        (f"{GITHUB_QUERY} created:>{since}", "stars", "newly_created"),
    ]

    seen: set[str] = set()
    items: list[dict[str, Any]] = []

    with httpx.Client(timeout=30.0) as client:
        for query, sort, label in queries:
            if len(items) >= limit:
                break
            logger.info("GitHub query [%s]: %s (sort=%s)", label, query, sort)
            batch = _github_search(client, headers, query, sort, fetch_n)
            # gentle pacing between search requests (secondary rate limit)
            time.sleep(1.0)
            for item in batch:
                url = str(item.get("url") or "").rstrip("/")
                if not url or url in seen:
                    continue
                if url in existing:
                    logger.debug("Skip known repo: %s", item.get("title"))
                    continue
                seen.add(url)
                items.append(item)
                if len(items) >= limit:
                    break

    # Fallback: if the fresh window yields too few new repos, widen once
    if len(items) < max(1, limit // 2):
        wider_since = (
            datetime.now(timezone.utc) - timedelta(days=max(fresh_days * 2, 14))
        ).strftime("%Y-%m-%d")
        logger.info(
            "Fresh window yielded %d items; widening to since=%s",
            len(items),
            wider_since,
        )
        with httpx.Client(timeout=30.0) as client:
            batch = _github_search(
                client,
                headers,
                f"{GITHUB_QUERY} pushed:>{wider_since}",
                "updated",
                fetch_n,
            )
            for item in batch:
                url = str(item.get("url") or "").rstrip("/")
                if not url or url in seen or url in existing:
                    continue
                seen.add(url)
                items.append(item)
                if len(items) >= limit:
                    break

    logger.info("GitHub: collected %d fresh items (skipped known=%d)", len(items), len(existing))
    return items[:limit]


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

    existing = _existing_source_urls()
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    per_feed = max(1, (limit * 2) // len(enabled))

    for feed in enabled:
        feed_items = _fetch_rss_feed(feed, per_feed)
        for item in feed_items:
            url = str(item.get("url") or "").rstrip("/")
            if not url or url in seen or url in existing:
                continue
            seen.add(url)
            items.append(item)
            if len(items) >= limit:
                break
        if len(items) >= limit:
            break

    logger.info("RSS: collected %d fresh items from %d feeds", len(items), len(enabled))
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
            ], tracker=TRACKER)
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


_id_counters: dict[str, int] = {}


def _generate_article_id(source_type: str) -> str:
    """Generate a sequential article ID in {source}-{YYYYMMDD}-{NNN} format.

    Args:
        source_type: Source type string (e.g. github_trending, hacker_news).

    Returns:
        A unique ID string, e.g. github-20260504-001.
    """
    prefix = ID_PREFIX_MAP.get(source_type, source_type)
    date_part = datetime.now(timezone.utc).strftime("%Y%m%d")
    cache_key = f"{prefix}-{date_part}"

    if cache_key not in _id_counters:
        max_seq = 0
        existing_pattern = re.compile(rf"^{re.escape(prefix)}-{re.escape(date_part)}-(\d{{3}})$")
        if ARTICLES_DIR.is_dir():
            for fpath in ARTICLES_DIR.glob("*.json"):
                if fpath.name == "index.json":
                    continue
                try:
                    data = json.loads(fpath.read_text(encoding="utf-8"))
                    if not isinstance(data, dict):
                        continue
                    entry_id = data.get("id", "")
                    m = existing_pattern.match(str(entry_id))
                    if m:
                        seq = int(m.group(1))
                        if seq > max_seq:
                            max_seq = seq
                except (json.JSONDecodeError, OSError):
                    continue
        _id_counters[cache_key] = max_seq

    _id_counters[cache_key] += 1
    return f"{prefix}-{date_part}-{_id_counters[cache_key]:03d}"


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

    timestamp = now_iso()

    return {
        "id": _generate_article_id(source_type),
        "title": item["title"],
        "source_url": item["url"],
        "source_type": source_type,
        "summary": analysis.get("summary", item.get("summary", "")),
        "tags": tags,
        "score": analysis.get("score", 5),
        "popularity": int(item.get("popularity") or 0),
        "status": "draft",
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

def _parse_step_arg(raw: str | None) -> list[int]:
    """Parse --step argument into a list of step numbers (1-4)."""
    if raw is None:
        return [1, 2, 3, 4]
    steps: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part.isdigit():
            raise ValueError(f"Invalid step value: '{part}'")
        n = int(part)
        if n < 1 or n > 4:
            raise ValueError(f"Step must be 1-4, got {n}")
        steps.append(n)
    return sorted(set(steps))


def main() -> int:
    """Execute pipeline steps: collect → analyze → organize → save."""
    args = parse_args()
    setup_logging(args.verbose)

    steps = _parse_step_arg(args.step)
    logger.info("Pipeline started (steps=%s, sources=%s, limit=%d, dry_run=%s)",
                steps, args.sources, args.limit, args.dry_run)

    selected = [s.strip() for s in args.sources.split(",") if s.strip()]

    # ── Step 1: Collect ──────────────────────────────────────────────────
    if 1 in steps:
        all_items: list[dict[str, Any]] = []
        selected_set = set(selected)
        use_github = "github" in selected_set
        use_rss = "rss" in selected_set

        # Split budget so RSS is not starved when GitHub fills --limit
        if use_github and use_rss:
            gh_budget = max(1, args.limit // 2)
            rss_budget = max(1, args.limit - gh_budget)
        elif use_github:
            gh_budget, rss_budget = args.limit, 0
        else:
            gh_budget, rss_budget = 0, args.limit

        if use_github:
            gh_token = os.environ.get("GITHUB_TOKEN")
            gh_items = collect_github(
                gh_budget,
                gh_token,
                fresh_days=args.fresh_days,
            )
            all_items.extend(gh_items)
            # Give leftover GitHub quota to RSS
            leftover = max(0, gh_budget - len(gh_items))
            rss_budget += leftover

        if use_rss and rss_budget > 0:
            rss_items = collect_rss(rss_budget)
            all_items.extend(rss_items)

        if not all_items:
            logger.warning("No items collected, exiting")
            return 0

        RAW_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
        raw_file = RAW_DIR / f"raw-{timestamp}.json"
        if args.dry_run:
            logger.info(
                "Dry-run: would save %d items to %s",
                len(all_items),
                raw_file,
            )
        else:
            raw_file.write_text(
                json.dumps(all_items, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            logger.info("Raw data saved: %s (%d items)", raw_file, len(all_items))
    else:
        all_items = []

    # ── Step 2: Analyze ──────────────────────────────────────────────────
    if 2 in steps:
        if not all_items:
            logger.warning("No items to analyze, skipping")
            return 0

        provider: dict[str, Any] | None = None
        try:
            provider = create_provider()
        except ValueError as e:
            logger.warning("LLM provider setup failed: %s", e)

        analyzed = analyze_items(all_items, provider)
        RAW_DIR.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(
            json.dumps(analyzed, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        logger.info("State saved: %s (%d items)", STATE_FILE, len(analyzed))

        if 3 not in steps and 4 not in steps:
            logger.info("Step 2 complete, state saved for later processing")
            return 0
    else:
        if STATE_FILE.is_file():
            analyzed = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            logger.info("Loaded %d items from state file", len(analyzed))
        else:
            analyzed = []

    # ── Step 3: Organize ────────────────────────────────────────────────
    if 3 in steps:
        if not analyzed:
            logger.warning("No items to organize, exiting")
            return 0
        organized = organize_items(analyzed)
    else:
        organized = analyzed

    # ── Step 4: Save ────────────────────────────────────────────────────
    if 4 in steps:
        if not organized:
            logger.warning("No articles to save, exiting")
            return 0
        save_articles(organized, dry_run=args.dry_run)

        # Clean up state file after successful save
        if STATE_FILE.is_file():
            STATE_FILE.unlink()
            logger.info("State file cleaned up")

    TRACKER.report()

    logger.info("Pipeline completed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
