---
name: tech-summary
description: 当需要对采集的技术内容进行深度分析总结时使用此技能
allowed-tools:
  - Read
  - Grep
  - Glob
  - WebFetch
---

# tech-summary Skill

## 使用场景

- 对每日采集的 GitHub Trending / Hacker News 原始数据进行深度分析
- 从海量技术动态中提取高价值信息，生成结构化知识条目
- 辅助人工判断哪些项目值得深入跟进

## 执行步骤

1. **读取最新文件** — 扫描 `knowledge/raw/` 目录，找到最新生成的采集 JSON 文件
2. **逐条深度分析** — 对每条项目执行：
   - **摘要**：用不超过 50 字概括核心内容
   - **技术亮点**：提取 2-3 个具体的技术要点（用事实和数据说话，避免模糊表述）
   - **评分**：1-10 分制并附评分理由（参见评分标准）
   - **标签建议**：推荐 2-4 个标签，优先使用已有标签体系
3. **趋势发现** — 整体扫描后归纳：
   - 共同主题（如：本周多个项目聚焦 Agent 编排）
   - 值得关注的新概念或方向
4. **输出 JSON** — 写入 `knowledge/tech-summary-YYYY-MM-DD.json`

## 评分标准

| 分值 | 含义 | 说明 |
|------|------|------|
| 9-10 | 改变格局 | 可能重塑技术路线或行业格局 |
| 7-8  | 直接有帮助 | 能解决当前实际问题，值得尝试 |
| 5-6  | 值得了解 | 有创意或潜力，但目前不够成熟 |
| 1-4  | 可略过 | 重复、过时或价值有限 |

## 注意事项

- 15 个项目中，评分 9-10 的项目不超过 2 个（保持评价的区分度）
- 技术亮点必须用事实支撑，禁止空泛描述（如"性能好"应改为"吞吐量提升 3 倍"）
- 如果 `knowledge/raw/` 目录为空或不存在，应报错提示
- 输出文件命名使用分析执行日期，而非采集日期

## 输出格式

```json
{
  "source": "tech_summary",
  "skill": "tech-summary",
  "analyzed_at": "2026-04-26T12:00:00+08:00",
  "source_file": "github-trending-2026-04-26.json",
  "trends": {
    "common_themes": ["Agent 编排框架成为热点"],
    "new_concepts": ["MCP 协议在工具调用中的普及"]
  },
  "items": [
    {
      "name": "owner/repo",
      "url": "https://github.com/owner/repo",
      "summary": "轻量级 Agent 框架，支持多模型路由",
      "highlights": [
        "支持 10+ LLM 提供商一键切换",
        "内置 MCP 协议支持，工具调用延迟 < 50ms",
        "周下载量突破 5 万"
      ],
      "rating": 8,
      "rating_reason": "直接提升了多模型开发效率，生态建设良好",
      "suggested_tags": ["agent", "framework", "llm"]
    }
  ]
}
```
