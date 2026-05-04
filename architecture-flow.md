```mermaid
flowchart TB
    subgraph Config ["配置层"]
        AGENTS[AGENTS.md<br/>系统架构与红线]
        A_DIR[.opencode/agents/<br/>collector | analyzer | organizer]
        S_DIR[.opencode/skills/<br/>github-trending | tech-summary]
    end

    subgraph Data ["数据层"]
        RAW[knowledge/raw/<br/>原始采集数据]
        ARTICLES[knowledge/articles/<br/>标准化知识条目]
    end

    subgraph Roles ["角色与行为"]
        C["采集者 Collector<br/>权限: Read/Grep/Glob/WebFetch<br/>禁止: Write/Bash/Edit"]
        A["分析者 Analyzer<br/>权限: Read/Grep/Glob/WebFetch<br/>禁止: Write/Bash/Edit"]
        O["整理者 Organizer<br/>权限: Read/Grep/Glob/Write/Edit<br/>禁止: WebFetch/Bash"]
    end

    AGENTS -- 定义角色边界 --> A_DIR
    AGENTS -- 定义文件命名规范 --> ARTICLES
    AGENTS -- 定义数据格式 --> RAW

    A_DIR -- 注入角色设定 --> C
    A_DIR -- 注入角色设定 --> A
    A_DIR -- 注入角色设定 --> O

    S_DIR -- 提供操作步骤 --> C
    S_DIR -- 提供操作步骤 --> A

    C -- 步骤1: WebFetch采集 --> RAW
    RAW -- 步骤2: 读取 --> A
    A -- 步骤3: LLM分析 --> O
    O -- 步骤4: 去重+格式化写入 --> ARTICLES

    C -. Skill 加载 .-> S_DIR
    A -. Skill 加载 .-> S_DIR

    RAW -. Agent 实例化 .-> A_DIR
    ARTICLES -. Agent 实例化 .-> A_DIR

    style Config fill:#e1f5fe,stroke:#0288d1
    style Data fill:#f3e5f5,stroke:#7b1fa2
    style Roles fill:#fff3e0,stroke:#ef6c00
    style AGENTS fill:#b3e5fc,stroke:#0288d1,stroke-width:2px
</mermaid>
```
