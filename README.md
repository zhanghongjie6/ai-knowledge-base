# AI Knowledge Base 🧠

> 自动化采集 → LLM 分析 → 结构化存储 → 多渠道分发的 AI 知识库系统。

每日自动从 GitHub Trending、Hacker News、RSS 等数据源采集 AI/LLM/Agent 领域的最新技术动态，通过大语言模型进行智能分析，生成结构化知识条目，最终推送至 Telegram / 飞书等渠道，构建持续更新的个人 AI 知识库。

---

## 功能概览

| 功能 | 说明 |
|------|------|
| **自动采集** | GitHub Search API、Hacker News、arXiv、OpenAI Blog、Hugging Face、Lobsters 等多源 |
| **LLM 分析** | 调用 DeepSeek / Qwen / OpenAI 生成中文摘要、亮点、评分、标签 |
| **去重与过滤** | 对标已有的知识条目去重，过滤低分（<6）内容 |
| **质量审核** | Supervisor 审核循环，支持迭代修正（最多 3 轮） |
| **多渠道分发** | Telegram / 飞书 Bot 推送 |
| **MCP 搜索** | 通过 MCP Server 提供本地知识库检索能力 |
| **每日自动化** | GitHub Actions 定时执行，自动提交新条目 |

---

## 系统架构

```
                    ┌─────────────────────────────┐
                    │       数据源层               │
                    │  GitHub  │ HN  │ RSS │ arXiv │
                    └──────────┴─────┴──────┴──────┘
                           │
                    ┌──────▼──────┐
                    │  采集(Collect) │
                    │  原始 JSON    │
                    └──────┬──────┘
                           │
                    ┌──────▼──────┐
                    │  分析(Analyze) │  ◄── LLM (DeepSeek/Qwen/OpenAI)
                    │  摘要/标签/评分 │
                    └──────┬──────┘
                           │
                    ┌──────▼──────┐
                    │  整理(Organize)│  ── 去重、过滤、格式化
                    │  知识条目 JSON│
                    └──────┬──────┘
                           │
                    ┌──────▼──────┐
                    │  审核(Review) │  ◄── Supervisor 审核循环
                    │  passed?     │
                    └──┬───────┬──┘
                  passed │       │ failed (重做)
                         ▼       ▼
                    ┌────────┐ ┌──────────┐
                    │ 保存   │ │ 修正重做 │
                    │ articles│ │ + 反馈    │
                    └────────┘ └──────────┘
                           │
                    ┌──────▼──────┐
                    │  分发(Dispatch)│
                    │ Telegram/飞书 │
                    └─────────────┘
```

有两种执行方式：

1. **Pipeline 流水线**（`pipeline/pipeline.py`）— 线性四阶段脚本，可分段执行
2. **LangGraph 工作流**（`workflows/graph.py`）— 状态图编排，支持审核循环和条件路由

---

## 技术栈

| 类别 | 选型 |
|------|------|
| 语言 | Python 3.11+ |
| AI 驱动开发 | [OpenCode](https://opencode.ai) + Agent 定义 |
| 工作流编排 | [LangGraph](https://langchain-ai.github.io/langgraph/) |
| LLM 提供商 | DeepSeek / 通义千问 / OpenAI |
| 定时任务 | GitHub Actions（每日北京时间 08:00 / UTC 00:00） |
| 数据格式 | JSON（结构化知识条目） |
| 搜索服务 | MCP Server（JSON-RPC 2.0 over stdio） |

---

## 快速开始

### 环境准备

```bash
# 克隆仓库
git clone https://github.com/your-username/ai-knowledge-base.git
cd ai-knowledge-base

# 创建虚拟环境
python3 -m venv venv
source venv/bin/activate

# 安装依赖
pip install -r requirements.txt
```

### 配置环境变量

```bash
# LLM 提供商（至少配置一个）
export LLM_PROVIDER=deepseek          # deepseek | qwen | openai
export DEEPSEEK_API_KEY=sk-xxxxx

# 可选：GitHub Token（提高 API 限频）
export GITHUB_TOKEN=ghp_xxxxx
```

### 运行 Pipeline

```bash
# 全量运行：采集 → 分析 → 整理 → 保存
python pipeline/pipeline.py --sources github,rss --limit 20

# 仅采集（预览原始数据）
python pipeline/pipeline.py --sources github --limit 5 --step 1

# 干运行（不写文件）
python pipeline/pipeline.py --sources rss --limit 10 --dry-run

# 指定步骤分段执行
python pipeline/pipeline.py --sources github,rss --limit 20 --step 1,2   # 采集+分析
python pipeline/pipeline.py --step 3,4                                    # 整理+保存

# 调试模式
python pipeline/pipeline.py --verbose
```

### 运行 LangGraph 工作流

```bash
python workflows/graph.py
```

工作流内置审核循环：前 2 轮模拟审核不通过并生成改进反馈，第 3 轮强制通过。输出详细的日志追踪。

---

## 项目结构

```
├── .github/workflows/
│   └── daily-collect.yml       # 每日定时采集 action
├── .opencode/
│   ├── agents/                 # OpenCode Agent 定义
│   │   ├── collector.md        #   采集者
│   │   ├── analyzer.md         #   分析者
│   │   └── organizer.md        #   整理者
│   └── skills/                 # OpenCode Skills
│       ├── github-trending/    #   GitHub Trending 技能
│       └── tech-summary/       #   技术摘要技能
├── hooks/
│   ├── validate_json.py        # JSON Schema 校验
│   └── check_quality.py        # 五维质量评分
├── knowledge/
│   ├── raw/                    # 原始采集数据 (JSON)
│   └── articles/               # 分析后的知识条目 (JSON)
│       └── index.json          # 文章索引
├── patterns/
│   ├── router.py               # 两阶段意图分类（关键词+LLM）
│   └── supervisor.py           # Supervisor 工作模式（Worker-Review-Revise）
├── pipeline/
│   ├── pipeline.py             # 四阶段数据流水线（采集→分析→整理→保存）
│   ├── model_client.py         # LLM 客户端（多提供商、重试、成本追踪）
│   └── rss_sources.yaml        # RSS 数据源配置
├── workflows/
│   ├── graph.py                # LangGraph 状态图定义
│   ├── nodes.py                # 5 个节点函数（collect/analyze/organize/review/save）
│   ├── state.py                # 共享状态类型定义 (KBState)
│   └── model_client.py         # 工作流用 LLM 客户端
├── utils/
│   └── github_api.py           # GitHub API 工具函数
├── mcp_knowledge_server.py     # MCP Server（本地知识库搜索）
├── opencode.json               # OpenCode 配置（MCP 注册）
├── AGENTS.md                   # 系统架构与红线规则
└── architecture-flow.md        # 架构流程图 (Mermaid)
```

---

## 知识条目格式

每条知识条目是 `knowledge/articles/` 下的独立 JSON 文件：

```json
{
  "id": "github-20260504-001",
  "title": "langchain-ai/langgraph",
  "source_url": "https://github.com/langchain-ai/langgraph",
  "source_type": "github_trending",
  "summary": "LangGraph 是一个用于构建有状态、多参与者 LLM 应用的工作流编排框架...",
  "tags": ["LLM", "Agent", "Framework", "Workflow", "github"],
  "score": 9,
  "status": "draft",
  "created_at": "2026-05-04T12:00:00+00:00",
  "updated_at": "2026-05-04T12:00:00+00:00"
}
```

评分标准：
- **9-10** — 开创性工作，可能重塑行业格局
- **7-8** — 直接可用，能立即应用
- **5-6** — 值得了解，拓宽视野
- **1-4** — 价值较低，建议跳过

---

## 数据源

### 当前启用

| 数据源 | 类型 | 说明 |
|--------|------|------|
| [GitHub Search API](https://docs.github.com/en/rest/search) | API | 搜索 AI/LLM/Agent/RAG 相关仓库，按 Stars 排序 |
| [Hacker News Best](https://hnrss.org/best) | RSS | HN 高分帖子，LLM 过滤 AI 相关内容 |
| [Lobsters](https://lobste.rs) | RSS | 全站 RSS，LLM 过滤 |
| [arXiv cs.AI](https://arxiv.org/list/cs.AI/recent) | RSS | AI 领域最新论文 |
| [OpenAI Blog](https://openai.com/blog) | RSS | OpenAI 官方更新 |
| [Hugging Face Blog](https://huggingface.co/blog) | RSS | HF 官方博客 |

### 可选关闭（enabled: false）

| 数据源 | 原因 |
|--------|------|
| Hacker News AI 关键词 | 与 HN Best 来源重叠，量较大 |
| 量子位 | 内容质量参差不齐 |
| 机器之心 | RSS 可用性待确认 |

配置见 [pipeline/rss_sources.yaml](pipeline/rss_sources.yaml)。

---

## 软件设计模式

项目实现了两种可复用的 Agent 协作模式：

### Router（两阶段意图分类）

```python
from patterns.router import route
print(route("搜索 GitHub 上的 LLM 项目"))
```

- **Layer 1**: 关键词快速匹配（零 LLM 成本）
- **Layer 2**: LLM 分类兜底（模糊查询）

支持意图：`github_search` / `knowledge_query` / `general_chat`

### Supervisor（Worker-Review-Revise 循环）

```python
from patterns.supervisor import supervisor
result = supervisor("分析 LangGraph 和 CrewAI 的架构区别")
```

- **Worker Agent**: 生成 JSON 分析报告
- **Supervisor Agent**: 从准确度、深度、格式三维度评分
- **Revise**: 未通过则携带反馈重做（最多 3 轮）

---

## MCP 知识库搜索

通过 [MCP (Model Context Protocol)](https://modelcontextprotocol.io) 提供本地知识库搜索能力。

```bash
python mcp_knowledge_server.py
```

提供 3 个工具：
- `search_articles(keyword, limit?)` — 按关键词搜索标题和摘要
- `get_article(id)` — 按 ID 获取完整文章
- `knowledge_stats()` — 统计信息（总数、来源分布、热门标签）

在 OpenCode / Claude Code 中使用：

```json
{
  "mcp": {
    "knowledge": {
      "type": "local",
      "command": ["python3", "mcp_knowledge_server.py"],
      "enabled": true
    }
  }
}
```

---

## 质量保障

每次 CI 自动执行两轮检查：

1. **Schema 校验** (`hooks/validate_json.py`)
   - 检查必填字段完整性
   - 校验 `id` 格式（`{source}-{YYYYMMDD}-{NNN}`）
   - 校验 `status` 合法值
   - 校验 URL 格式

2. **质量评分** (`hooks/check_quality.py`)
   - 五维评分：摘要质量、标签准确度、分类合理性、一致性、可操作性
   - 评分范围 1-5，C 级（<3 分）文件触发构建失败
   - 标签白名单校验

---

## 开发指南

### 编码规范

- **风格**: PEP 8, `snake_case` 命名
- **文档**: Google 风格 docstring
- **日志**: 使用 `logging` 模块，禁止裸 `print()`
- **类型**: 所有函数必须标注类型注解
- **提交**: [Conventional Commits](https://www.conventionalcommits.org/)（`feat:` / `fix:` / `chore:`）

### 添加新数据源

1. 在 [pipeline/rss_sources.yaml](pipeline/rss_sources.yaml) 中添加源配置
2. 在 `collect_rss()` 或新增函数中实现采集逻辑
3. 在 `SOURCE_CODE_MAP` 和 `ID_PREFIX_MAP` 注册新源类型
4. 在 GitHub Actions 中确认 `--sources` 参数包含新源

### 添加新 LLM 提供商

1. 在 `pipeline/model_client.py` 的 `PROVIDER_CONFIGS` 和 `PRICE_TABLE` 中注册
2. 设置对应的环境变量 `{NAME}_API_KEY`
3. 在 CI 的 `env` 中传递新的 Secret

---

## CI/CD

GitHub Actions 每日北京时间 08:00（UTC 00:00）自动执行：

1. 安装依赖
2. 运行采集 Pipeline（GitHub + RSS，各 20 条）
3. JSON Schema 校验
4. 五维质量评分
5. 自动 commit & push 新条目

也可通过 `workflow_dispatch` 手动触发。

---

## 红线（绝对禁止）

- ❌ 禁止将 API Key、Token 等敏感信息硬编码到代码中
- ❌ 禁止对原始数据源进行高频爬取（需遵守 robots.txt）
- ❌ 禁止在无人工审核的情况下自动分发高危 / 争议内容
- ❌ 禁止修改知识条目的 `id` 和 `created_at` 字段（一旦写入只读）
- ❌ 禁止在 Agent 之间产生循环触发

---

## License

MIT
