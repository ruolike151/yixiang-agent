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
uv run yixiang rag ingest --source local --file evals/fixtures/media_sample.json   # 入库 31 部（离线）
uv run yixiang rag eval            # golden 集：top-3 命中率 + MRR
uv run yixiang ops explain-search "讲时间循环的"   # 五段中间结果（FTS / 向量 / RRF / 过滤 / 加权）
uv run yixiang brief               # 按需日报：今日安排 + 1 条影视推荐
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

- **三文件核心记忆**：`soul.md` / `user.md` / `memory.md` 原子写 + 上限校验（3000 / 4000 / 活跃区 150 行）；
- **三支柱检索**：`facts`（FTS5 + 向量，RRF 融合）、`episodes`（只用向量）、`skills`（关键词匹配，≤1500 字截断）；
- **检索门控**：规则预过滤 + 小模型判定（fail-open），注入 S6 段（top5 facts + top3 episodes）；
- **人机共治**：手改 `memory.md` 重启即生效（`yixiang memory sync` 文件为准，无 id 的行走导入并分配 id，删行即软删）；
- **巩固**：每 N 轮蒸馏进 `facts` 三档阈值（0.9 自动 / 0.6 待确认 / 其余丢弃）+ watermark，失败不推水印；
- **"记住"硬契约**：`save_memory` 必调，相近记忆走 update 不重复新增，回复带"已记住：…"；
- **运维入口**：`yixiang memory {list,show,sync,verify,restore}`、`yixiang skills validate`。

PART 3（语料与按需推荐）已交付：

- **语料入库**：`rag ingest` 抓 Bangumi / TMDb（单线程 + `sleep(1.0)` + 退避重试 3 次 + 原始 JSON 缓存 `data/raw/`），`source_id` 幂等 upsert——已存在且简介没变就**跳过且不重嵌入**，`--resume` 从 `meta.ingest_cursor_<source>` 续跑，`--dry-run` 一个字节不落库；
- **中文混合检索**：FTS5 必须 **jieba 预分词**（`unicode61` 把连续 CJK 当一个 token，`MATCH '悬疑'` 静默命中 0 条）+ `LIKE` 兜底；向量与关键词两路走 **RRF**（只看名次，对 BM25 / 余弦的量纲免疫）+ 硬过滤 + 口味软加权；
- **口味软加权不硬过滤**：`final = rrf_score × (1 + taste_score)`，`taste_score ∈ [-0.5, +0.5]`；冷启动前 7 天为 0；画像来自 `user.md` 的 `喜欢：/不喜欢：` 约定写法；
- **推荐去重**：`recommend_log` 近 7 天已推的 id 一律硬过滤，对话推荐与日报共用同一个窗口；
- **三个工具**：`search_media`（top-3 带可核对理由，只读）/ `recommend_media`（默认 1 条、写日志）/ `daily_brief`（今日任务 + 到期备忘 + 1 条推荐，落 `data/briefs/YYYY-MM-DD.md`，同日重跑**覆盖**）；
- **可解释与评测**：`yixiang ops explain-search` 打印五段中间结果（FTS / 向量 / RRF / 过滤 / 加权）；`yixiang rag eval` 跑 golden 集（20 条 + 10 条 holdout，top-3 命中率 + MRR）。评测口径**关口味**（`use_taste=False`，只测相关性），排序数字才不随 `user.md` 漂移，PART 4 的 L3 回归拿它当门禁；带口味的排序出现在解释与日报里；
- **降级可用**：嵌入后端或向量扩展不可用 → 退纯 FTS5 仍返回结果，trace 记 `E_EMBED_UNAVAILABLE`，用户无感；
- **外部内容一律包裹**：影视简介是不可信文本，进 prompt 前必包 `<external_content source="media_db">`（D-23）。

边界（写在明面上）：**cron 定时推送 / 唤醒补发属 P2**，本部分只做内容层——`daily_brief` 的组装逻辑被对话、`yixiang brief`、未来的定时任务三种触发源复用。语料入库的**真实抓取**与**真实嵌入模型**（bge-small-zh-v1.5）需要网络；离线的等价入口是 `--source local --file evals/fixtures/media_sample.json`。

后续：PART 4 评测与交付（judge 评测、L3 回归、CI 门禁）。PART 3 交付时留下的 6 个决策点
（联网验证、golden 复核、评测口径、偏好写法、降级标记位置、P2 定时推送）记在
[`docs/TODO-AFTER-PART-4.md`](./docs/TODO-AFTER-PART-4.md)，**等 PART 4 收口后再逐条过**，不阻塞当前门禁。
