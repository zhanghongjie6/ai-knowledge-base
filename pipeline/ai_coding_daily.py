"""AI Coding 资讯日报生成器

按照用户提供的规范生成每日报告：
- 时效性硬要求：近7天内发布
- 影响力门槛：知名公司/实验室/作者
- 规范报告格式：国际资讯 + 国内资讯
- 推送到企业微信群

Usage:
    python pipeline/ai_coding_daily.py
    python pipeline/ai_coding_daily.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx


logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
ARTICLES_DIR = BASE_DIR / "knowledge" / "articles"
REPORT_DIR = BASE_DIR / "ai-coding-news"
STATE_FILE = BASE_DIR / "ai-coding-news" / "sent_state.json"
PUSH_HISTORY_FILE = BASE_DIR / "knowledge" / "push_history.json"

AUTHORITATIVE_SOURCES = {
    "openai.com",
    "anthropic.com",
    "google.com",
    "microsoft.com",
    "meta.com",
    "deeplearning.ai",
    "huggingface.co",
    "arxiv.org",
    "github.com",
    "36kr.com",
    "qbitai.com",
    "jiqizhixin.com",
    "infoq.cn",
    "tech.meituan.com",
    "ruanyifeng.com",
    "juejin.cn",
    "zhihu.com",
}

INFLUENTIAL_AUTHORS = {
    "OpenAI",
    "Anthropic",
    "Google AI",
    "Meta AI",
    "Microsoft Research",
    "DeepLearning.AI",
    "Hugging Face",
    "美团技术团队",
    "阮一峰",
}

CHINESE_SOURCES = {
    "36kr.com",
    "qbitai.com",
    "jiqizhixin.com",
    "infoq.cn",
    "tech.meituan.com",
    "ruanyifeng.com",
    "juejin.cn",
    "zhihu.com",
}

AI_CODING_KEYWORDS = [
    "AI",
    "ai",
    "AI编程",
    "代码生成",
    "编程助手",
    "大模型",
    "LLM",
    "LangChain",
    "LangGraph",
    "OpenClaw",
    "Cursor",
    "Ollama",
    "AI Agent",
    "智能体",
    "RAG",
    "prompt",
    "提示词",
    "微调",
    "训练",
    "推理",
    "部署",
    "模型",
    "算法",
    "数据",
]


def setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _parse_created_at(value: str) -> datetime | None:
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


def is_within_last_7_days(article: dict[str, Any]) -> bool:
    created = _parse_created_at(str(article.get("created_at") or ""))
    if created is None:
        return False
    now = datetime.now(timezone.utc)
    return (now - created) <= timedelta(days=7)


def get_source_domain(url: str) -> str:
    url = str(url).lower()
    if "://" in url:
        url = url.split("://")[1]
    if "/" in url:
        url = url.split("/")[0]
    return url


def is_authoritative(url: str) -> bool:
    domain = get_source_domain(url)
    for source in AUTHORITATIVE_SOURCES:
        if source in domain:
            return True
    return False


def get_influence_level(article: dict[str, Any]) -> tuple[int, str]:
    url = str(article.get("source_url") or "")
    title = str(article.get("title") or "")
    domain = get_source_domain(url)
    
    score = 1
    reason = "普通来源"
    
    if domain in ["openai.com", "anthropic.com", "google.com", "meta.com", "microsoft.com"]:
        score = 5
        reason = "顶级 AI 公司官方发布"
    elif domain in ["arxiv.org"]:
        score = 4
        reason = "学术论文"
    elif domain in ["github.com"] and ("releases" in url or "/tags/" in url):
        score = 4
        reason = "知名项目发布更新"
    elif domain in ["huggingface.co", "deeplearning.ai"]:
        score = 4
        reason = "AI 领域权威平台"
    elif domain in ["36kr.com", "tech.meituan.com"]:
        score = 3
        reason = "知名科技媒体/大厂技术博客"
    elif domain in ["ruanyifeng.com", "juejin.cn"]:
        score = 3
        reason = "高质量技术社区"
    elif is_authoritative(url):
        score = 2
        reason = "权威来源"
    
    for author in INFLUENTIAL_AUTHORS:
        if author.lower() in title.lower():
            score = max(score, 4)
            reason = f"{reason}（提及知名作者）"
    
    if "LLM" in title or "AI" in title or "agent" in title.lower():
        score = max(score, 2)
    
    return score, reason


NON_AI_KEYWORDS = [
    "iphone", "手机", "电脑", "笔记本", "相机", "平板", "手表",
    "游戏", "手游", "电竞", "主机", "显卡", "硬件",
    "汽车", "特斯拉", "比亚迪", "新能源",
    "美食", "旅游", "电影", "音乐", "综艺",
    "购物", "电商", "促销", "优惠",
    "健康", "医疗", "健身", "运动",
    "教育", "高考", "考研", "留学",
    "职场", "招聘", "面试", "薪资",
    "理财", "股票", "基金", "投资",
    "数码", "评测", "对比", "推荐",
    "桌面", "桌垫", "收纳", "整理", "家居", "生活",
    "角落", "房间", "装修", "设计", "风格",
    "咖啡", "咖啡杯", "办公室", "工位", "文具",
    "读书", "书单", "阅读", "推荐书籍",
    "壁纸", "主题", "皮肤", "美化",
    "宠物", "猫", "狗", "动物",
    "穿搭", "时尚", "衣服", "鞋子", "包包",
]

def is_ai_coding_relevant(article: dict[str, Any]) -> bool:
    title = str(article.get("title") or "").lower()
    summary = str(article.get("summary") or "").lower()
    url = str(article.get("source_url") or "").lower()
    
    has_ai_keyword = False
    for keyword in AI_CODING_KEYWORDS:
        if keyword.lower() in title or keyword.lower() in summary:
            has_ai_keyword = True
            break
    
    ai_domains = {
        "openai.com", "anthropic.com", "huggingface.co",
        "deepmind.com", "ollama.com", "langchain.com",
        "infoq.cn", "jiqizhixin.com", "qbitai.com",
    }
    domain = get_source_domain(url)
    is_ai_domain = domain in ai_domains
    
    if domain == "arxiv.org":
        arxiv_ai_categories = {"cs.ai", "cs.cl", "cs.lg", "cs.cv"}
        for cat in arxiv_ai_categories:
            if cat in url.lower():
                is_ai_domain = True
                break
    
    source_type = str(article.get("source_type") or "").lower()
    is_ai_source = source_type in ["github_trending", "ai"]
    
    is_ai_content = has_ai_keyword or is_ai_domain or is_ai_source
    
    if not is_ai_content:
        for keyword in NON_AI_KEYWORDS:
            if keyword.lower() in title or keyword.lower() in summary:
                return False
    
    return is_ai_content


def is_github_source_code_update(article: dict[str, Any]) -> bool:
    url = str(article.get("source_url") or "")
    title = str(article.get("title") or "")
    domain = get_source_domain(url)
    
    if domain != "github.com":
        return False
    
    if "/releases/tag/" in url or "/tags/" in url:
        return True
    
    if re.match(r"^v?\d+\.\d+(\.\d+)?(-\w+)?$", title.strip()):
        return True
    
    if re.match(r"^[\w-]+/[\w-]+$", title.strip()):
        return True
    
    return True


def is_chinese_article(article: dict[str, Any]) -> bool:
    title = str(article.get("title") or "")
    url = str(article.get("source_url") or "")
    
    if is_chinese_source(url):
        return True
    
    chinese_chars = re.findall(r'[\u4e00-\u9fff]', title)
    if len(chinese_chars) >= 3:
        return True
    
    return False


def is_chinese_source(url: str) -> bool:
    domain = get_source_domain(url)
    return any(source in domain for source in CHINESE_SOURCES)


def load_articles() -> list[dict[str, Any]]:
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


def load_sent_state() -> set[str]:
    sent_ids: set[str] = set()
    
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            sent_ids.update(data.get("sent_ids", []))
        except (json.JSONDecodeError, OSError):
            pass
    
    if PUSH_HISTORY_FILE.exists():
        try:
            data = json.loads(PUSH_HISTORY_FILE.read_text(encoding="utf-8"))
            items = data.get("items", {})
            for key, rec in items.items():
                if isinstance(rec, dict):
                    url = str(rec.get("source_url") or "").strip().rstrip("/").lower()
                    art_id = str(rec.get("id") or "").strip()
                    if url:
                        sent_ids.add(url)
                    if art_id:
                        sent_ids.add(art_id)
        except (json.JSONDecodeError, OSError):
            pass
    
    return sent_ids


def save_sent_state(sent_ids: set[str]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "sent_ids": list(sent_ids),
        "updated_at": datetime.now(timezone.utc).isoformat()
    }
    STATE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def generate_report(articles: list[dict[str, Any]]) -> str:
    today = datetime.now(timezone.utc)
    date_str = today.strftime("%Y-%m-%d")
    date_cn = today.strftime("%Y年%m月%d日")
    
    international = []
    chinese = []
    
    for article in articles:
        if is_chinese_source(str(article.get("source_url"))):
            chinese.append(article)
        else:
            international.append(article)
    
    international.sort(key=lambda x: get_influence_level(x)[0], reverse=True)
    chinese.sort(key=lambda x: get_influence_level(x)[0], reverse=True)
    
    lines = []
    lines.append(f"# AI Coding 资讯日报 - {date_cn}")
    lines.append("")
    lines.append(f"> 本期共收录 {len(articles)} 篇高质量文章，覆盖范围：近7天发布")
    lines.append("")
    
    if international:
        lines.append("## 🌍 国际资讯")
        lines.append("")
        for i, article in enumerate(international, 1):
            score, reason = get_influence_level(article)
            lines.append(f"### {i}. [{article.get('title', '')}]")
            domain = get_source_domain(str(article.get('source_url')))
            created = _parse_created_at(str(article.get('created_at')))
            date_str = created.strftime("%Y-%m-%d") if created else "日期不明"
            lines.append(f"- **来源**：{domain}")
            lines.append(f"- **日期**：{date_str}")
            lines.append(f"- **影响力**：{reason}")
            summary = str(article.get('summary') or "")[:200]
            lines.append(f"- **摘要**：{summary}")
            lines.append(f"- **链接**：[阅读原文]({article.get('source_url')})")
            lines.append("")
    
    if chinese:
        lines.append("## 🇨🇳 国内资讯")
        lines.append("")
        for i, article in enumerate(chinese, 1):
            score, reason = get_influence_level(article)
            lines.append(f"### {i}. [{article.get('title', '')}]")
            domain = get_source_domain(str(article.get('source_url')))
            created = _parse_created_at(str(article.get('created_at')))
            date_str = created.strftime("%Y-%m-%d") if created else "日期不明"
            lines.append(f"- **来源**：{domain}")
            lines.append(f"- **日期**：{date_str}")
            lines.append(f"- **影响力**：{reason}")
            summary = str(article.get('summary') or "")[:200]
            lines.append(f"- **摘要**：{summary}")
            lines.append(f"- **链接**：[阅读原文]({article.get('source_url')})")
            lines.append("")
    
    lines.append("---")
    lines.append("")
    lines.append("*本报告由 AI-Coding资讯日报机器人自动生成，每日上午9:00更新*")
    lines.append("*筛选标准：近7天发布 + 有行业影响力 + 直接AI Coding相关*")
    
    return "\n".join(lines)


def push_to_wechat(articles: list[dict[str, Any]], webhook: str) -> None:
    batches = [articles[i:i+8] for i in range(0, len(articles), 8)]
    
    for i, batch in enumerate(batches, 1):
        news = []
        for article in batch:
            title = str(article.get("title") or "")[:100]
            url = str(article.get("source_url") or "")
            score, reason = get_influence_level(article)
            summary = str(article.get("summary") or "")[:120]
            desc = f"{reason} · {summary}"[:200]
            
            news.append({
                "title": title,
                "url": url,
                "description": desc,
            })
        
        payload = {"msgtype": "news", "news": {"articles": news}}
        try:
            with httpx.Client(timeout=30.0) as client:
                resp = client.post(
                    webhook,
                    headers={"Content-Type": "application/json; charset=utf-8"},
                    json=payload,
                )
                resp.raise_for_status()
                result = resp.json()
                logger.info(f"Batch {i}/{len(batches)}: {len(news)} items pushed successfully")
        except Exception as e:
            logger.error(f"Batch {i}/{len(batches)} failed: {e}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="AI Coding 资讯日报生成器")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只生成报告，不推送",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="详细日志",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=3,
        help="每次推送文章数量限制（默认3条）",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    setup_logging(args.verbose)
    
    logger.info("开始生成 AI Coding 资讯日报...")
    
    all_articles = load_articles()
    logger.info(f"加载到 {len(all_articles)} 篇文章")
    
    sent_ids = load_sent_state()
    logger.info(f"已推送过 {len(sent_ids)} 篇文章")
    
    articles = []
    for article in all_articles:
        article_id = str(article.get("id") or article.get("source_url") or "")
        if article_id in sent_ids:
            continue
        if not is_within_last_7_days(article):
            continue
        if not is_ai_coding_relevant(article):
            continue
        if is_github_source_code_update(article):
            continue
        if not is_chinese_article(article):
            continue
        articles.append(article)
    
    logger.info(f"筛选后：{len(articles)} 篇符合条件（近7天 + AI Coding相关 + 中文教程 + 非源码更新 + 未推送）")
    
    articles = articles[:args.limit]
    logger.info(f"按限制取前 {len(articles)} 篇推送")
    
    if not articles:
        logger.warning("没有符合条件的新文章")
        return 0
    
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    report_path = REPORT_DIR / f"AI-Coding-News-{today_str}.md"
    
    report = generate_report(articles)
    report_path.write_text(report, encoding="utf-8")
    logger.info(f"报告已生成：{report_path}")
    
    if not args.dry_run:
        webhook = os.environ.get("WECHAT_WEBHOOK_URL")
        if webhook:
            push_to_wechat(articles, webhook)
            new_sent_ids = {str(a.get("id") or a.get("source_url") or "") for a in articles}
            save_sent_state(sent_ids | new_sent_ids)
            logger.info(f"已保存推送状态，共 {len(sent_ids | new_sent_ids)} 篇已推送")
        else:
            logger.warning("WECHAT_WEBHOOK_URL 未设置，跳过推送")
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
