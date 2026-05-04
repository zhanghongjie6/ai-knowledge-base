"""Five-dimension quality scoring for knowledge entry JSON files.

Usage:
    python hooks/check_quality.py <json_file> [json_file2 ...]
    python hooks/check_quality.py knowledge/articles/*.json

Exit code 0 if no C-grade files, 1 otherwise.
"""

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path


# ── Standard tag whitelist ──────────────────────────────────────────────
STANDARD_TAGS = {
    "LLM", "Agent", "RAG", "Framework", "Deployment",
    "Evaluation", "Platform", "Computer-Use", "Sandbox", "Automation",
    "Memory", "Coding", "Tutorial", "Education", "Voice",
    "Audio", "Document", "Conversion", "Knowledge-Graph", "Code-Analysis",
    "Inference", "Serving", "Multimodal", "Multi-Agent", "OpenAI",
    "Claude", "Skill", "Monitor", "RSS", "MCP",
    "Collaboration", "Retrieval", "Open-Source", "Tool", "API",
    "Fine-Tuning", "Reasoning", "Safety", "Alignment", "Search",
    "Function-Calling", "Benchmark", "Dataset", "Embedding", "Vector-DB",
    "Workflow", "Orchestration", "Testing", "Observability", "Security",
}

SUMMARY_TECH_KEYWORDS = {
    "LLM", "Agent", "RAG", "AI", "大模型", "深度学习",
    "神经网络", "Transformer", "多模态", "推理", "Agent",
    "微调", "对齐", "检索", "生成", "对话", "embedding",
    "fine-tuning", "prompt", "context", "memory",
}

# ── Buzzword blacklist ─────────────────────────────────────────────────
BUZZWORDS_ZH = [
    "赋能", "抓手", "闭环", "打通", "全链路",
    "底层逻辑", "颗粒度", "对齐", "拉通", "沉淀",
    "强大的", "革命性的",
]

BUZZWORDS_EN = [
    "groundbreaking", "revolutionary", "game-changing", "cutting-edge",
    "state-of-the-art", "disruptive", "next-generation",
]

BUZZWORD_PATTERNS = [
    re.compile(re.escape(w), re.IGNORECASE)
    for w in BUZZWORDS_ZH + BUZZWORDS_EN
]

URL_PATTERN = re.compile(r"^https?://\S+")

VALID_STATUSES = {"draft", "review", "published", "archived"}


# ── Data structures ────────────────────────────────────────────────────
@dataclass
class DimensionScore:
    name: str
    score: float
    max_score: float
    detail: str = ""

    @property
    def ratio(self) -> float:
        return self.score / self.max_score if self.max_score > 0 else 0.0


@dataclass
class QualityReport:
    filepath: str
    dimensions: list[DimensionScore] = field(default_factory=list)

    @property
    def total(self) -> float:
        return sum(d.score for d in self.dimensions)

    @property
    def max_total(self) -> float:
        return sum(d.max_score for d in self.dimensions)

    @property
    def grade(self) -> str:
        score = self.total
        if score >= 80:
            return "A"
        if score >= 60:
            return "B"
        return "C"


# ── Scorers ─────────────────────────────────────────────────────────────
def score_summary_quality(data: dict) -> DimensionScore:
    dim = DimensionScore(name="摘要质量", score=0.0, max_score=25.0)
    summary = data.get("summary", "")
    if not isinstance(summary, str) or not summary.strip():
        dim.detail = "摘要为空或非字符串"
        return dim

    text = summary.strip()
    length = len(text)

    # Length component
    if length >= 50:
        base = 15.0
        dim.detail = f"长度 {length} 字 (>=50)，得基础 15 分"
    elif length >= 20:
        base = 10.0
        dim.detail = f"长度 {length} 字 (>=20)，得基础 10 分"
    else:
        base = 0.0
        dim.detail = f"长度 {length} 字 (<20)，得基础 0 分"
        dim.score = 0.0
        return dim

    # Tech keyword bonus
    found = set()
    for kw in SUMMARY_TECH_KEYWORDS:
        if kw.lower() in text.lower():
            found.add(kw)

    bonus = min(len(found) * 2.0, 10.0)
    if bonus > 0:
        dim.detail += f"，含 {len(found)} 个技术关键词 (+{bonus:.0f})"

    dim.score = min(base + bonus, 25.0)
    return dim


def score_technical_depth(data: dict) -> DimensionScore:
    dim = DimensionScore(name="技术深度", score=0.0, max_score=25.0)
    raw = data.get("score")
    if raw is None:
        dim.detail = "缺少 score 字段"
        return dim
    if not isinstance(raw, (int, float)) or not (1 <= raw <= 10):
        dim.detail = f"score 值无效: {raw!r}"
        return dim
    score_val = raw * 2.5
    dim.score = min(score_val, 25.0)
    dim.detail = f"score={raw}，映射得分 {dim.score:.1f}"
    return dim


def score_format_compliance(data: dict) -> DimensionScore:
    dim = DimensionScore(name="格式规范", score=0.0, max_score=20.0)
    checks: list[tuple[str, bool]] = []

    # id
    id_valid = bool(isinstance(data.get("id"), str) and data["id"].strip())
    checks.append(("id", id_valid))

    # title
    title_valid = bool(isinstance(data.get("title"), str) and data["title"].strip())
    checks.append(("title", title_valid))

    # source_url
    url_valid = bool(
        isinstance(data.get("source_url"), str)
        and URL_PATTERN.match(data["source_url"])
    )
    checks.append(("source_url", url_valid))

    # status
    status_valid = bool(
        isinstance(data.get("status"), str)
        and data["status"] in VALID_STATUSES
    )
    checks.append(("status", status_valid))

    # timestamps (created_at + updated_at)
    created_ok = bool(isinstance(data.get("created_at"), str) and data["created_at"].strip())
    updated_ok = bool(isinstance(data.get("updated_at"), str) and data["updated_at"].strip())
    ts_valid = created_ok and updated_ok
    checks.append(("时间戳", ts_valid))

    passed = sum(1 for _, ok in checks if ok)
    dim.score = passed * 4.0
    failed = [name for name, ok in checks if not ok]
    if failed:
        dim.detail = f"未通过: {', '.join(failed)}"
    else:
        dim.detail = "全部通过"
    return dim


def score_tag_precision(data: dict) -> DimensionScore:
    dim = DimensionScore(name="标签精度", score=0.0, max_score=15.0)
    tags = data.get("tags", [])
    if not isinstance(tags, list) or len(tags) == 0:
        dim.detail = "缺少标签"
        return dim

    # Count component
    n = len(tags)
    if 1 <= n <= 3:
        count_score = 10.0
        dim.detail = f"标签数 {n} (1-3)，得 10 分"
    else:
        count_score = 5.0
        dim.detail = f"标签数 {n} (>3)，得 5 分"

    # Standard tag bonus
    all_standard = all(
        isinstance(t, str) and t in STANDARD_TAGS for t in tags
    )
    if all_standard:
        dim.detail += "，全部为标准标签 (+5)"
        dim.score = min(count_score + 5.0, 15.0)
    else:
        non_std = [t for t in tags if not (isinstance(t, str) and t in STANDARD_TAGS)]
        dim.detail += f"，非标准标签: {', '.join(map(str, non_std))}"
        dim.score = count_score
    return dim


def score_buzzword_detection(data: dict) -> DimensionScore:
    dim = DimensionScore(name="空洞词检测", score=15.0, max_score=15.0)
    texts = []

    summary = data.get("summary", "")
    if isinstance(summary, str):
        texts.append(summary)

    title = data.get("title", "")
    if isinstance(title, str):
        texts.append(title)

    combined = " ".join(texts)
    if not combined:
        return dim

    found_words: list[str] = []
    for pattern in BUZZWORD_PATTERNS:
        if pattern.search(combined):
            found_words.append(pattern.pattern)

    if found_words:
        unique = set()
        for w in BUZZWORDS_ZH + BUZZWORDS_EN:
            if re.search(re.escape(w), combined, re.IGNORECASE):
                unique.add(w)
        penalty = min(len(unique) * 3.0, 15.0)
        dim.score = max(15.0 - penalty, 0.0)
        dim.detail = f"发现空洞词: {', '.join(sorted(unique))}，扣 {penalty:.0f} 分"
    else:
        dim.detail = "未发现空洞词"

    return dim


# ── Quality check ──────────────────────────────────────────────────────
def check_file(filepath: Path) -> QualityReport:
    try:
        data = json.loads(filepath.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, Exception) as e:
        report = QualityReport(filepath=str(filepath))
        report.dimensions.append(
            DimensionScore(
                name="整体", score=0.0, max_score=100.0,
                detail=f"无法读取或解析 JSON: {e}",
            )
        )
        return report

    if not isinstance(data, dict):
        report = QualityReport(filepath=str(filepath))
        report.dimensions.append(
            DimensionScore(
                name="整体", score=0.0, max_score=100.0,
                detail="JSON 根节点必须为对象",
            )
        )
        return report

    return QualityReport(
        filepath=str(filepath),
        dimensions=[
            score_summary_quality(data),
            score_technical_depth(data),
            score_format_compliance(data),
            score_tag_precision(data),
            score_buzzword_detection(data),
        ],
    )


# ── Console helpers ────────────────────────────────────────────────────
def draw_progress(current: int, total: int, bar_width: int = 30) -> str:
    if total == 0:
        return ""
    filled = int(bar_width * current / total)
    bar = "#" * filled + "-" * (bar_width - filled)
    pct = int(100 * current / total)
    return f"[{bar}] {pct}%"


def print_report(report: QualityReport, index: int, total: int) -> None:
    grade_color = {
        "A": "",
        "B": "",
        "C": "",
    }
    progress = draw_progress(index, total)
    short = Path(report.filepath).name
    print(f"\n  [{index}/{total}] {progress}  {short}", file=sys.stderr)
    print(f"  {'─' * 50}", file=sys.stderr)

    for d in report.dimensions:
        pct = d.ratio * 100
        bar_len = 20
        filled = int(bar_len * d.ratio)
        bar = "█" * filled + "░" * (bar_len - filled)
        print(
            f"    {d.name:10s} {bar} {d.score:5.1f}/{d.max_score:<4.0f}",
            file=sys.stderr,
        )
        if d.detail:
            print(f"    {'':10s} └─ {d.detail}", file=sys.stderr)

    grade = report.grade
    total_score = report.total
    print(
        f"    {'─' * 50}", file=sys.stderr,
    )
    print(
        f"    TOTAL:      {total_score:5.1f}/{report.max_total:<4.0f}  "
        f"(Grade {grade_color[grade]}{grade})",
        file=sys.stderr,
    )


# ── Main ────────────────────────────────────────────────────────────────
def main() -> int:
    parser = argparse.ArgumentParser(
        description="Five-dimension quality scoring for knowledge entries",
    )
    parser.add_argument(
        "files",
        metavar="FILE",
        nargs="+",
        type=str,
        help="JSON file(s) to score (supports glob patterns)",
    )
    args = parser.parse_args()

    all_reports: list[QualityReport] = []
    has_c = False
    file_queue: list[Path] = []

    for pattern in args.files:
        if "*" in pattern or "?" in pattern:
            matched = sorted(Path().resolve().glob(pattern))
        else:
            matched = [Path(pattern).resolve()]

        if not matched:
            print(f"  \u26a0 Warning: no files matched '{pattern}'", file=sys.stderr)
            continue
        file_queue.extend(matched)

    total = len(file_queue)
    for i, filepath in enumerate(file_queue, 1):
        report = check_file(filepath)
        all_reports.append(report)
        print_report(report, i, total)
        if report.grade == "C":
            has_c = True

    # Summary
    if all_reports:
        grades = [r.grade for r in all_reports]
        a_count = grades.count("A")
        b_count = grades.count("B")
        c_count = grades.count("C")
        avg = sum(r.total for r in all_reports) / len(all_reports)
        print(f"\n  {'=' * 50}", file=sys.stderr)
        print(
            f"  Summary: {len(all_reports)} file(s)  "
            f"A={a_count}  B={b_count}  C={c_count}  "
            f"avg={avg:.1f}/100",
            file=sys.stderr,
        )

    return 1 if has_c else 0


if __name__ == "__main__":
    sys.exit(main())
