---
name: github-trending
description: 当需要采集 GitHub 热门开源项目时使用此技能
allowed-tools:
  - Read
  - Grep
  - Glob
  - WebFetch
---

# github-trending Skill

## 使用场景

- 每日采集 GitHub Trending 上 AI/LLM/Agent 领域的开源项目
- 定期扫描新出现的明星仓库，补充知识库
- 生成面向技术决策者的热门项目简报

## 执行步骤

1. **搜索热门仓库** — 调用 GitHub Trending API (`https://github.com/trending?since=daily`)，获取今日热门仓库列表
2. **提取信息** — 对每个仓库提取：名称、描述、Star 数、语言、标签(topic)
3. **过滤** — 纳入规则：项目与 AI / LLM / Agent 相关；排除规则：Awesome 列表、纯文档仓库、已归档项目
4. **去重** — 对比 `knowledge/raw/` 下已有 JSON 文件中的 `items[].name`，跳过已收录项目
5. **撰写中文摘要** — 格式：`项目名 + 做什么 + 为什么值得关注`，每条不超过 100 字
6. **排序取 Top 15** — 按 Star 数降序排列，最多保留 15 条
7. **输出 JSON** — 写入 `knowledge/raw/github-trending-YYYY-MM-DD.json`

## 注意事项

- 遵守 GitHub API 频率限制，两次请求间隔至少 2 秒
- 如果 `knowledge/raw/` 目录不存在，自动创建
- 已存在的同日期文件应追加而非覆盖（覆盖前须用户确认）
- 摘要必须原创，禁止直接翻译英文描述

## 输出格式

```json
{
  "source": "github_trending",
  "skill": "github-trending",
  "collected_at": "2026-04-26T12:00:00+08:00",
  "items": [
    {
      "name": "owner/repo",
      "url": "https://github.com/owner/repo",
      "summary": "LangChain — 一个用于构建 LLM 应用的开发框架，提供链式调用和 Agent 编排能力，近期因多模型支持更新受到广泛关注",
      "stars": 125000,
      "language": "Python",
      "topics": ["llm", "framework", "agent"]
    }
  ]
}
```
