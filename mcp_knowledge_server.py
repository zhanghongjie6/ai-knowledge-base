"""MCP Server for local AI knowledge base search.

Provides 3 tools via JSON-RPC 2.0 over stdio:
  - search_articles: keyword search in titles and summaries
  - get_article: full article by ID
  - knowledge_stats: article counts, source distribution, top tags

Usage:
    python mcp_knowledge_server.py

Run as a subprocess — the MCP client communicates over stdin/stdout.
"""

import json
import sys
from collections import Counter
from pathlib import Path


ARTICLES_DIR = Path(__file__).resolve().parent / "knowledge" / "articles"

SERVER_INFO = {
    "name": "knowledge-server",
    "version": "1.0.0",
}

TOOL_DEFINITIONS = [
    {
        "name": "search_articles",
        "description": "Search knowledge articles by keyword in title and summary",
        "inputSchema": {
            "type": "object",
            "properties": {
                "keyword": {
                    "type": "string",
                    "description": "Search keyword (case-insensitive)",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of results (default 5)",
                    "default": 5,
                },
            },
            "required": ["keyword"],
        },
    },
    {
        "name": "get_article",
        "description": "Get full article content by its ID",
        "inputSchema": {
            "type": "object",
            "properties": {
                "article_id": {
                    "type": "string",
                    "description": "Article ID to retrieve",
                },
            },
            "required": ["article_id"],
        },
    },
    {
        "name": "knowledge_stats",
        "description": "Get knowledge base statistics (total articles, source distribution, top tags)",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
]


# ── Article store ────────────────────────────────────────────────────────

class ArticleStore:
    """Loads and indexes all articles from the articles directory."""

    def __init__(self, directory: Path) -> None:
        self._articles: list[dict] = []
        self._by_id: dict[str, dict] = {}
        if directory.is_dir():
            self._load(directory)

    def _load(self, directory: Path) -> None:
        for fpath in sorted(directory.glob("*.json")):
            try:
                data = json.loads(fpath.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if not isinstance(data, dict):
                continue
            self._articles.append(data)
            article_id = data.get("id", "")
            if isinstance(article_id, str) and article_id:
                self._by_id[article_id] = data

    @property
    def count(self) -> int:
        return len(self._articles)

    def search(self, keyword: str, limit: int = 5) -> list[dict]:
        """Case-insensitive search in title and summary."""
        kw = keyword.lower()
        results: list[dict] = []
        for article in self._articles:
            title = article.get("title", "")
            summary = article.get("summary", "")
            if kw in title.lower() or kw in summary.lower():
                results.append(article)
            if len(results) >= limit:
                break
        return results

    def get(self, article_id: str) -> dict | None:
        return self._by_id.get(article_id)

    def stats(self) -> dict:
        total = self.count
        source_counter: Counter[str] = Counter()
        tag_counter: Counter[str] = Counter()

        for article in self._articles:
            source = article.get("source_type") or article.get("source") or "unknown"
            source_counter[str(source)] += 1
            tags = article.get("tags", [])
            if isinstance(tags, list):
                for tag in tags:
                    if isinstance(tag, str):
                        tag_counter[tag] += 1

        top_sources = [
            {"source": s, "count": c}
            for s, c in source_counter.most_common()
        ]
        top_tags = [
            {"tag": t, "count": c}
            for t, c in tag_counter.most_common(20)
        ]

        return {
            "total_articles": total,
            "source_distribution": top_sources,
            "top_tags": top_tags,
        }


# ── MCP over stdio ──────────────────────────────────────────────────────

def make_response(
    request_id: int | str | None,
    result: object = None,
    error: dict | None = None,
) -> str:
    body: dict = {"jsonrpc": "2.0"}
    if request_id is not None:
        body["id"] = request_id
    if error:
        body["error"] = error
    else:
        body["result"] = result
    return json.dumps(body, ensure_ascii=False)


def handle_request(raw: str, store: ArticleStore) -> str | None:
    """Process a single JSON-RPC request and return a response string.

    Returns None for notifications (no id).
    """
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError:
        return make_response(
            None,
            error={"code": -32700, "message": "Parse error"},
        )

    req_id = msg.get("id")
    method = msg.get("method", "")
    params = msg.get("params", {})

    if not isinstance(params, dict):
        params = {}

    # Notifications have no id
    if req_id is None:
        return None

    if method == "initialize":
        return make_response(req_id, {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": SERVER_INFO,
        })

    if method == "tools/list":
        return make_response(req_id, {"tools": TOOL_DEFINITIONS})

    if method == "tools/call":
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})

        if not isinstance(arguments, dict):
            arguments = {}

        if tool_name == "search_articles":
            keyword = arguments.get("keyword", "")
            if not keyword:
                return make_response(
                    req_id,
                    error={"code": -32602, "message": "Missing required argument: keyword"},
                )
            limit = arguments.get("limit", 5)
            if not isinstance(limit, int):
                try:
                    limit = int(limit)
                except (ValueError, TypeError):
                    limit = 5
            results = store.search(keyword, limit)
            return make_response(req_id, {"articles": results})

        if tool_name == "get_article":
            article_id = arguments.get("article_id", "")
            if not article_id:
                return make_response(
                    req_id,
                    error={"code": -32602, "message": "Missing required argument: article_id"},
                )
            article = store.get(article_id)
            if article is None:
                return make_response(
                    req_id,
                    error={"code": -32000, "message": f"Article not found: {article_id}"},
                )
            return make_response(req_id, {"article": article})

        if tool_name == "knowledge_stats":
            return make_response(req_id, store.stats())

        return make_response(
            req_id,
            error={"code": -32601, "message": f"Unknown tool: {tool_name}"},
        )

    return make_response(
        req_id,
        error={"code": -32601, "message": f"Method not found: {method}"},
    )


def main() -> None:
    store = ArticleStore(ARTICLES_DIR)
    sys.stderr.write(
        f"knowledge-server: loaded {store.count} articles from {ARTICLES_DIR}\n"
    )
    sys.stderr.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        response = handle_request(line, store)
        if response is not None:
            sys.stdout.write(response + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
