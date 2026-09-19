# yixiang（以湘）

本地优先的个人 Agent：**有记忆、有评测、有成本账**，是"连续在用"而不是"跑过一次"的助手。

一句话讲清它和聊天机器人的差别：每轮对话都会装配工作记忆（人格 + 用户画像 + 核心记忆 + 检索到的长尾记忆），每次工具调用都进 trace，每次 LLM 调用都进成本账。

## 安装

需要 Python 3.12 与 [uv](https://docs.astral.sh/uv/)（`python -m pip install uv`）。

```bash
uv sync                     # 创建 .venv 并装依赖（默认不装 torch）
cp .env.example .env        # Windows: Copy-Item .env.example .env
# 编辑 .env 填入 YIXIANG_API_KEY
uv run yixiang doctor       # 启动自检：配置 / 数据库 / 向量扩展 / 嵌入模型 / 模型探活 / 三文件
```

## 跑起来

```bash
uv run yixiang chat                # 交互式对话（流式输出）
uv run yixiang migrate             # 应用数据库迁移
uv run yixiang ops cost            # 今日 token 与成本
uv run yixiang ops tail            # 实时跟随 trace
uv run yixiang --help              # chat / serve / doctor / rag / ops / eval / migrate
```

CLI 内斜杠命令：`/help` `/new [名字]` `/history` `/tools` `/trace [n]` `/cost` `/exit`。

跑测试（离线、零成本、不依赖网络）：

```bash
uv run pytest evals/deterministic -m "not live"
```

## 数据边界（诚实版本）

声称"数据完全本地"是不成立的——每轮拼好的 prompt 会发给模型供应商。真实边界如下：

| 数据 | 是否离开本机 |
|---|---|
| `data/soul.md` / `user.md` / `memory.md`、`state.db`、trace、备份 | 否 |
| 嵌入计算（bge-small-zh-v1.5，本地 CPU 推理） | 否 |
| 每轮拼好的 prompt（**含被注入的记忆片段、检索到的语料片段**） | **是**，发给所选模型供应商 |
| QQ 消息内容（P2 接入后） | 是，先经腾讯服务器 |
| 影视语料元数据（入库阶段） | 是，从 Bangumi / TMDb 拉取 |

## 目录结构

```
yixiang/     包本体：config / providers / app / runtime / loop / tools / gateway / ops
templates/   soul.md / user.md / memory.md 的初版模板（仓库只放模板）
data/        运行时数据（gitignore）：三文件、state.db、traces/、usage.jsonl、briefs/、backups/
evals/       确定性用例（FakeProvider 驱动，离线零成本）+ golden 集 + judge
docs/        PRODUCT.md（WHAT/WHY）、TECH-DESIGN.md（HOW）、parts/（按周切分的工作包）
```

## 当前进度

PART 1（基座与 Agent Loop）已交付：CLI 流式对话、memo/plan 工具、trace 与成本账、FakeProvider 确定性用例。

PART 2（记忆系统）已交付：

- **三文件核心记忆**：`soul.md` / `user.md` / `memory.md` 原子写 + 上限校验（8000 / 4000 / 活跃区 150 行）；
- **三支柱检索**：`facts`（FTS5 + 向量，RRF 融合）、`episodes`（只用向量）、`skills`（关键词匹配，≤1500 字截断）；
- **检索门控**：规则预过滤 + 小模型判定（fail-open），注入 S6 段（top5 facts + top3 episodes）；
- **人机共治**：手改 `memory.md` 重启即生效（`yixiang memory sync` 文件为准，无 id 的行走导入并分配 id，删行即软删）；
- **巩固**：每 N 轮蒸馏进 `facts` 三档阈值（0.9 自动 / 0.6 待确认 / 其余丢弃）+ watermark，失败不推水印；
- **"记住"硬契约**：`save_memory` 必调，相近记忆走 update 不重复新增，回复带"已记住：…"；
- **运维入口**：`yixiang memory {list,show,sync,verify,restore}`、`yixiang skills validate`。

后续：PART 3 语料与按需推荐 → PART 4 评测与交付。
