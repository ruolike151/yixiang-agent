# W1 演示剧本（3 分钟，照读即可）

> 目标：证明 PART 1 的四件事——**能流式对话 / 能记事排计划 / 每轮有 trace / 每轮有成本账**。
> 剧本里的输出都是实际跑出来的（2026-09-19，cli:default 会话），不要临场改台词。

## 0. 演示前的准备（这 5 分钟不算在 3 分钟里）

```bash
uv sync                              # 建 .venv + 装依赖（默认不装 torch）
cp .env.example .env                 # PowerShell: Copy-Item .env.example .env
# 编辑 .env：填 YIXIANG_API_KEY=sk-...
uv run yixiang doctor                # 期望：6 项全部通过
uv run yixiang migrate               # 期望：user_version=1（最新 1）
```

想从干净状态开始：**不要删 `data/`**，换个会话名就行（`uv run yixiang chat --session cli:demo`）。

## 1. 开场（0:00–0:20，照读）

> "这是 yixiang，一个跑在本机的个人助手。它和'套壳聊天'的差别有三点：每轮对话会现拼一份工作记忆、
> 每次工具调用都留痕、每次模型调用都记成本。今天只演第一周交付的部分：基座和 Agent Loop。"

## 2. 命令面（0:20–0:50）

```bash
uv run yixiang --help
```

期望：列出 `chat / serve / doctor / rag / ops / eval / migrate`。讲到这句就够——
"`rag` 和 `serve` 现在会如实说自己没实现，不假装成功。"

```bash
uv run yixiang doctor
```

期望：6 项自检

```
✓ 1. 配置加载与校验：已加载（data_dir=… · main=deepseek-chat · api_key=已配置）
✓ 2. data 目录可写
✓ 3. SQLite 与迁移：user_version=1，14 张业务表齐备
✓ 4. sqlite-vec 扩展：可加载（向量检索所需）
✓ 5. 模型探活：deepseek-chat 响应 xxx ms
✓ 6. 三文件就位：data/｜新建 无｜保留 soul.md, user.md, memory.md
6 项全部通过。
```

## 3. 对话 + 工具 + trace + 成本（0:50–1:50）

```bash
uv run yixiang chat
```

按顺序敲这四句（每句之间不用解释，让流式自己说话）：

| 输入 | 期望看到 |
|---|---|
| `记一下周五中午前交材料` | 逐字流式回复 → 轮末「本轮」框里 `tools: add_memo ✓` |
| `帮我排一个两周的 RAG 复习计划` | 先 `· 正在查询…`、`调用 create_plan …`、`调用 add_task …`，再整段回答 |
| `今天要做什么` | `list_today ✓`，回答只含今天这一条，不多编 |
| `/trace` | 一屏看完上一轮：gate 有没有跑、调了哪些工具、token、耗时、成本、结束原因 |

最后加两句收尾：

```
/cost
/exit
```

期望：`今日：N 轮 · M 次调用` + `成本：¥0.00xx` + 按角色的明细（main / utility…）。

> 讲点（照读）："`/trace` 这一行里最容易被忽略的是 `working_memory` 的分段长度——
> 出问题时要定位'是人格段太长、还是检索段塞了脏数据'，看的就是这几个数字。"

## 4. 用例（1:50–2:30）

```bash
uv run yixiang eval
```

期望：66 条确定性用例全绿，**离线、零成本、1 秒出头**（`-m "not live"`，不发一次网络请求）。

> 讲点："这 66 条里没有一条连真模型：FakeProvider 按剧本回放，能注入超时、截断、坏 JSON、
> 工具抛异常。超时用例里连 `asyncio.sleep` 都被换成了记录器——所以它永远不会偶发失败。"

## 5. 收尾三句（2:30–3:00，照读）

1. **为什么不用 LangGraph**：核心循环不到 100 行，值得自己写；真正花心思的是三条防护——迭代上限、
   重复调用检测、工具失败计数，以及"工具失败也要如实告诉用户"。
2. **流式的复杂度在边界**：工具调用轮不流式（只显示"正在查询…"）、半截失败保留已输出文本、
   增量只进内存缓冲、整轮结束才写 `chat_log`。
3. **我算过钱**：当前时间戳放在 system 末尾而不是开头，否则自动前缀缓存永远不命中；
   按 §15.2 的敏感性分析，缓存命中率从 70% 掉到 0，日成本从 ¥0.41 涨到 ¥0.58。

## 6. 会被问到的问题（答案在仓库里）

| 问题 | 一句话答案 | 深挖时翻到 |
|---|---|---|
| 工具失败了会怎样？ | 工具不抛异常、只返回 `Error...` 字符串；loop 折叠成"这个操作失败了：…"回喂模型并进 trace | D-19 用例 |
| 模型死循环调同一个工具？ | guard 第 2 次给纠错文本、第 3 次直接打断（`finish_reason=guard_stop`） | D-20 用例 |
| 上下文长了怎么办？ | 按**整轮**丢弃最旧历史，预算按 `input_chars()` 实测；下限是至少留最近一轮 | D-21 用例 |
| 会不会读到 `data/` 之外的文件？ | 只有声明了 `path_args` 的工具才做路径校验，解析到 `data/` 外直接拒绝，工具函数不执行 | D-22 用例 |
| 记忆存哪？ | SQLite（WAL）+ `data/` 三文件；迁移只增不改，`user_version` 一条条推 | D-26 用例 |

## 7. 兜底：没有 API key 时怎么演

不带 key 也能演完第 2、4 步和各条用例（doctor 会显示"2 项告警、0 项失败"）：

```bash
uv run yixiang doctor          # 4 项通过 + 2 项告警（配置未填 key / 模型探活跳过）
uv run yixiang eval            # 66 条全绿，仍然离线零成本
```

第 3 步的对话要么填 key 真跑，要么改成演用例：

```bash
uv run pytest evals/deterministic/test_cli_gateway.py -q
```
