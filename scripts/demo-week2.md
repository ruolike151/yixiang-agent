# W2 演示剧本（3 分钟，照读即可）

> 目标：证明 PART 2 的五件事——**跨会话记得住 / 门控决定要不要检索 / 人改 `memory.md` 立即生效 /
> "记住"指令必落库 / 巩固与三方对账**。
> 第 3 步的输出是实际跑出来的（2026-09-19，`cli:demo` 会话）；第 2、4 步要真模型，没 key 时用第 8 步的兜底。

## 0. 演示前的准备（不算在 3 分钟里）

```bash
uv sync                              # 建 .venv + 装依赖（默认不装 torch）
cp .env.example .env                 # PowerShell: Copy-Item .env.example .env
# 编辑 .env：填 YIXIANG_API_KEY=sk-...
uv run yixiang doctor                # 期望：6 项全部通过（第 6 项含三文件与上限）
uv run yixiang migrate               # 期望：user_version=1（最新 1）
```

想从干净状态开始：**不要删 `data/`**，换个会话名（`uv run yixiang chat --session cli:demo2`）。

## 1. 开场（0:00–0:20，照读）

> "上周演的是基座和 Agent Loop。这周加的是记忆：它能跨会话记住你、能被你直接编辑、
> 而且**不是每轮都无脑把记忆塞进 prompt**——先过一个门控，该检索才检索。"

## 2. 跨会话记忆 + 门控（0:20–1:10）

```bash
uv run yixiang chat --session cli:demo
```

按顺序敲这四句：

| 输入 | 期望看到 |
|---|---|
| `记住我周末喜欢睡到十点` | 先 `manage_memory(search)` 查重、再 `save_memory ✓`，回复里有 `已记住：…` |
| `/trace` | `intent.remember=true`；工具列里 `search ✓ / save_memory ✓` |
| `/new` | 新会话 `cli:20260919-1530`（换个会话，记忆不换） |
| `我上周说喜欢什么来着` | `gate.retrieve=true`（规则命中"我 / 上周 / 喜欢"），S6 段 `facts≥1`、`chars>0`，回答复述"睡到十点" |

> 讲点（照读）："门控分两层——规则先筛一遍，能拍板的（寒暄、'记住'、自我指涉）不花模型调用；
> 规则没结论才问一次小模型。模型挂了是 **fail-open**：照样检索，trace 里带 `E_GATE_FAIL_OPEN`，
> 宁可多检索一次也不装作没记住。"

## 3. 人改 `memory.md` 立即生效（1:10–2:00，离线也能演）

先在 `data/memory.md` 的 `## 偏好` 下手写一行 `- 用户喜欢看悬疑和科幻`，保存，然后：

```bash
uv run yixiang memory sync
uv run yixiang memory list
uv run yixiang memory verify
```

实测输出：

```
新增 1 · 更新 0 · 软删 0 · 复活 0 · 归档 0
  · 第 11 行：无 id 的手写条目已导入为 fact #1
[1] (偏好) 用户喜欢看悬疑和科幻
一致：memory.md 与数据库、索引三方对齐
```

接着**把那一行整个删掉**（不是清空内容，是删行），再跑一次：

```
新增 0 · 更新 0 · 软删 1 · 复活 0 · 归档 0
（还没有任何记忆条目；用 save_memory 工具或直接编辑 memory.md）
```

删错了不慌——软删可恢复，`memory restore` 一句话捞回来：

```bash
uv run yixiang memory restore 1        # → {"ok": true, "id": 1, "action": "restore"}
uv run yixiang memory show 1           # → #1 [偏好] 用户喜欢看悬疑和科幻 · 状态：存活
```

> 讲点："`memory.md` 是**文件为准**：手写的行导入并分配 id，删的行软删、不再注入也不被检索；
> 删掉的 id 不会回收，所以导入时不会跟老记忆撞号。聊天进程每次启动也会同步一次，
> 手改完重启就生效，不用敲命令。"

## 4. 巩固（2:00–2:30）

接着聊满 `consolidate_every`（默认 20）轮，或者直接演兜底路径：

```bash
uv run yixiang eval consolidation
```

期望：4 条用例全绿——**撞车的候选不重复落库、模型没货时一条不编、坏 JSON 不写库、
模型失败时水印不动**（水印不动 = 下次重试，`chat_log` 原始数据永不删）。

> 讲点："巩固分三档：置信度 ≥0.9 直接写进 `用户/偏好`，0.6~0.9 进 `待确认` 段等人点头，
> <0.6 丢掉——情绪化的'今天好累'不该变成永久记忆。"

## 5. 收尾三句（2:30–3:00，照读）

1. **记忆分三层但不是三个库**：`facts`（FTS5 + 向量混合，RRF 融合）、`episodes`（只用向量）、
   `skills`（关键词匹配，单条 ≤1500 字）。三支柱都写在同一张 SQLite 里，靠 `source` 和表分开。
2. **上限是硬的**：`soul.md` 3000 / `user.md` 4000 字符，`memory.md` 活跃区 150 行，
   超了先淘汰最久没用过的（置顶 `*` 的不动），归档区只提示不硬删。
3. **门禁是量化的**：门控 golden 集 40 条标注，要求**漏检率 = 0**、误检率 ≤30%、规则跳过率 ≥15%，
   跑 `uv run yixiang eval gate`。

## 6. 会被问到的问题（答案在仓库里）

| 问题 | 一句话答案 | 深挖时翻到 |
|---|---|---|
| 记忆存哪？ | SQLite（WAL）+ `data/` 三文件，文件为准、双写同事务 | `yixiang/memory/sync.py` |
| 索引漂移了怎么办？ | `memory verify` 三方对账；`facts_fts` 与存活条目行数不一致就报出来 | D-08 用例 |
| 每次都得重新嵌入吗？ | 不用：文件 sha256 与上次一致就整段跳过；embedder 不可用时降级成"没有检索结果" | `sync.should_sync` |
| 检索会不会塞爆上下文？ | S6 上限 top5 facts + top3 episodes，trace 里只记条数与字符数 | §6.1 |
| 巩固会不会把噪声写进去？ | 三档阈值 + 撞车查重 + 负样本约束写进 prompt | D-18 用例 |

## 7. 兜底：没有 API key 时怎么演

第 3、4 步和全部用例都不需要 key（doctor 会显示"2 项告警、0 项失败"）：

```bash
uv run yixiang doctor                  # 4 项通过 + 2 项告警（配置未填 key / 模型探活跳过）
uv run yixiang memory sync             # 文件 → DB，纯本地
uv run yixiang eval                    # 108 条确定性用例全绿，离线零成本、3 秒
uv run yixiang eval gate               # 只跑门控门禁那一组（16 条）
```

第 2 步要么填 key 真跑，要么改成读 `evals/deterministic/test_memory_write.py` 的用例讲。
