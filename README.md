# yixiang（以湘）

本地优先的个人 Agent：**有记忆、有评测、有成本账**——是"连续在用"的助手，不是"跑过一次"的演示。

它和聊天机器人的差别在三条可验证的工程事实：

1. **每轮现拼工作记忆**：人格（`soul.md`）+ 用户画像（`user.md`）+ 核心记忆（`memory.md`）+ 检索到的长尾 facts / episodes，由代码决定注入什么，不靠"希望模型记得"；
2. **每件事都留痕**：工具调用进 `data/traces/`（`ops tail` / `show-trace` 可回放），每次模型调用进 `usage.jsonl`（`ops cost --explain` 打印各段 token 占比）；
3. **退步会被拦住**：GitHub Actions 四步门禁 + `release_gate` 五项判定（确定性 100% / judge ≥4.0 / 门控漏检 0 / 检索 top-3 ≥60% / 成本告警）。判定逻辑只有一份，不在 YAML 里重写。

架构与分层纪律见 [`docs/architecture.md`](./docs/architecture.md)，三张数字卡见 [`docs/NUMBERS.md`](./docs/NUMBERS.md)。

## 三条命令跑起来

需要 Python 3.12 与 [uv](https://docs.astral.sh/uv/)（`python -m pip install uv`）：

```bash
uv sync                      # ① 建 .venv 并装依赖（默认不装 torch）
cp .env.example .env         # ② Windows: Copy-Item .env.example .env；然后把 YIXIANG_API_KEY 填进去
uv run yixiang chat          # ③ 开始对话（流式输出）
```

没有 API key 也能验证"跑起来了"——离线入口两条命令：

```bash
uv run yixiang doctor        # 八项启动自检：配置 / 目录 / SQLite / 向量扩展 / 模型探活 / 三文件 / 密钥可恢复性 / Bangumi 收藏 token
uv run yixiang rag eval      # 检索回归：top-3 命中率 + MRR（无 key 可跑；首次会下 100MB 嵌入模型，
                             #   想零下载就加 YIXIANG_EMBED_BACKEND=hash，那一路是离线假嵌入）
```

CLI 内的斜杠命令：`/help` `/new [名字]` `/history` `/tools` `/trace [n]` `/cost` `/exit`。

不想在终端里翻人设和记忆，就开一个本机前端（只绑 `127.0.0.1`，标准库实现，装不上依赖
这种事先排除掉）：

```bash
uv run yixiang web        # 打开 http://127.0.0.1:8765/
```

七个面板对应测试时最常做的事：**对话**（流式，逐字出）、**历史对话**（按会话翻往
来，点一条即切过去）、**人设与记忆**（`soul.md` / `user.md` / `memory.md` 就地改，超限
一个字节都不写）、**模型配置**（主模型 / api_base / 上限 / 预算，写回 `.env` 并热生效）、
**提示词**（S1~S8 本轮实况，自查"模型到底看到了什么"）、**QQ 设置**（白名单校验：开了
网关却没白名单直接拒）、**链路 trace**（本轮迭代 / 工具调用 / tokens / 成本，与
`yixiang ops show-trace <turn_id>` 同源）。上传的文件落在 `data/uploads/`，模型用
`read_file` 读它。
手边没文件可传，用 [`evals/fixtures/web_upload_sample.md`](./evals/fixtures/web_upload_sample.md)
当样张。

## 数据边界（诚实版本，TECH §14.4）

声称"数据完全本地"是不成立的：每轮拼好的 prompt 会发给模型供应商。真实边界如下——

| 数据 | 是否离开本机 |
|---|---|
| `data/soul.md` / `user.md` / `memory.md`、`state.db`、trace、备份 | **否** |
| 嵌入计算（bge-small-zh-v1.5，本地 CPU 推理） | **否** |
| 每轮拼好的 prompt（**含被注入的记忆片段、检索到的语料片段**） | **是**，发给所选模型供应商 |
| QQ 消息内容（P2 接入后） | 是，先经腾讯服务器 |
| 影视语料元数据（**入库阶段**，一次性） | 是，从 Bangumi / TMDb 拉取并缓存在 `data/raw/` |
| Bangumi live 检索（**对话阶段**，`bangumi_search` / `bangumi_subject` / `bangumi_my_collections`） | **是**，当场请求 `api.bgm.tv`（读自己的收藏要 PAT；搜索与条目详情免 token） |

缩小出网面的路线是明确的：门控与巩固换本地小模型（`.env` 的 `YIXIANG_GATE_MODEL` / `YIXIANG_UTILITY_MODEL` 配置位已就绪）、嵌入已经是本地、检索片段按需注入而不是全量塞入。**先把边界写清楚，再谈缩小**——反过来做，最容易在答辩时被一句话戳穿。

## 四层评测与发布门禁

一条纪律：**依赖越多的层跑得越少**（TECH §13.1）。这不是省事，是不让外部 API 的抖动阻塞开发，也不让成本随提交次数线性增长。

| 层 | 内容 | 什么时候跑 | 成本 |
|---|---|---|---|
| **L1 单元** | 纯函数与单模块：解析、截断、计价、幂等键… | 每次保存 | 0 |
| **L2 集成** | 假 Provider 驱动的完整轮次：工具调用、门控、巩固、调度、安全 | PR + CI | 0 |
| **L3 检索回归** | golden 集上的 top-3 命中率与 MRR | PR + CI（离线嵌入后端）+ nightly 真嵌入 | 0 |
| **L4 judge** | 10 条 rubric（闲聊 / 推荐 / 计划 / 记忆管理 / 情绪陪伴） | nightly + 发版前（`--live`） | ≈¥0.06/次 |

```bash
uv run pytest evals/deterministic -m "not live"   # L1+L2+L3：离线、零成本、≤30 秒
uv run yixiang eval judge                         # L4：离线基线自检（有 key 时 --live 走真模型）
uv run yixiang eval gate                          # 门控用例：漏检 = 0 是硬门禁
uv run python -m yixiang.ops.release_gate         # 五项汇总判定：退出码 0 = 可以合并
uv run ruff check .                               # 静态检查
uv run yixiang skills validate                    # 技能文件格式
```

发布门禁的五项与阈值（改动等于改考核标准，只写在 `yixiang/ops/release_gate.py` 一处）：

| 检查 | 阈值 | 性质 |
|---|---|---|
| 确定性用例通过率 | 100% | 硬门禁 |
| judge 均分 | ≥4.0 / 5 | 硬门禁（**只当回归警报**，不当质量结论） |
| 门控漏检率 | = 0（误检容忍 ≤30%） | 硬门禁（漏检 = 失忆，产品级事故） |
| 检索 top-3 命中率 | ≥60% | 硬门禁 |
| 单轮成本 | ≤日预算 ×1.5 | **只告警**，不阻止合并 |

两条容易踩的口径，明写在这里：

- **检索评测关口味**（`use_taste=False`）：门禁量的是"相关性排序有没有退步"，数字不能随 `user.md` 内容漂移。带口味的排序只出现在 `ops explain-search` 与 `yixiang brief` 里——给人看的解释保留口味；
- **live 用例不进 PR**：外部 API 抖动不该阻塞开发。nightly 与发版前手动 `uv run yixiang eval --live`。

judge 的局限主动交底（TECH §13.4）：P0 用与 main 同族的模型自评，存在偏好偏差。三条缓解同时在场——阈值只当回归警报、可客观判断的部分（工具调没调 / 参数对不对 / 有没有编造）**下沉成确定性断言**、换 judge 模型时重跑全部历史分数再比。

## 命令速查

```bash
uv run yixiang chat                     # 交互式对话（流式）
uv run yixiang web                      # 本机 Web 控制台（http://127.0.0.1:8765/）
uv run yixiang doctor                   # 八项启动自检
uv run yixiang migrate                  # 应用数据库迁移
uv run yixiang brief                    # 按需日报：今日安排 + 1 条影视推荐
uv run yixiang rag ingest --source local --file evals/fixtures/media_sample.json   # 离线入库 31 部
uv run yixiang rag eval                 # golden 集：20 条 + 10 条 holdout（不加 YIXIANG_EMBED_BACKEND=hash 就是真嵌入）
uv run yixiang ops explain-search "讲时间循环的"    # 五段中间结果：FTS / 向量 / RRF / 过滤 / 加权
uv run yixiang ops cost --day           # 今日 token 与成本（--explain 打印分段占比）
uv run yixiang ops tail                 # 实时跟随今天的 trace
uv run yixiang memory verify            # 记忆三方对账：memory.md / 数据库 / FTS 索引
uv run yixiang backup                   # state.db 日快照 + data/ 私有仓提交
uv run yixiang backup gc --keep-days 30 # 回收过期快照（默认保留 30 天）
uv run python scripts/restore_drill.py  # 恢复演练：快照当唯一库源启动一次并逐项对账
uv run yixiang --help                   # chat / web / serve / doctor / rag / brief / ops / eval / migrate / memory / skills / backup
```

## 目录结构

```
yixiang/         包本体
  runtime/       模型客户端、时钟、会话状态（每轮装配的唯一入口）
  loop/          Agent Loop 与防绕圈护栏
  tools/         工具定义与注册表（memo / plan / 记忆 / 影视）
  memory/        三文件核心记忆、三支柱检索、门控、巩固、人机共治
  rag/           影视语料：ingest/（入库）+ retrieve/（检索）+ 口味加权 / 评测 / 嵌入
  gateway/       入口层：cli.py（REPL）/ qq.py（OneBot v11 反向 WS）/ sinks.py（投递通道）
  web/           Web 控制台：console.py（业务适配）+ server.py（HTTP/SSE/静态文件）+ static/（手写前端，无构建）
  scheduler/     常驻 job：jobs.py（巩固兜底 / 每日汇总 / 周巡检）+ brief_job.py（晨报）+ runtime.py
  ops/           trace、usage、doctor、explain-search、release_gate、backup
templates/       soul.md / user.md / memory.md 的初版模板（仓库只放模板）
data/            运行时数据（gitignore）：三文件、state.db、traces/、usage.jsonl、briefs/、backups/
                 └ 它同时是一个**私有仓**：只版本化三文件 / skills / briefs，永不推远端
evals/           deterministic/（L1+L2+L3）+ live/（`-m live` 真模型）+ golden/ + judge/ + fixtures/
docs/            PRODUCT.md、TECH-DESIGN.md、architecture.md、NUMBERS.md、HANDOFF.md、
                 SECRETS-RECOVERY.md、golden-decisions.md、TODO-AFTER-PART-4.md、parts/
scripts/         demo 剧本与恢复演练脚本
.github/         workflows/ci.yml（四步门禁）
```

## 当前进度

**PART 1 基座与 Agent Loop**：CLI 流式对话、`memo` / `plan` 工具、trace 与成本账、FakeProvider 确定性用例。

**PART 2 记忆系统**：三文件核心记忆（原子写 + 上限校验：3000 / 4000 / 活跃区 150 行）、三支柱检索（facts 走 FTS5 + 向量 RRF；episodes 只用向量；skills 关键词）、检索门控（规则预过滤 + 小模型判定，fail-open）、人机共治（手改 `memory.md` 重启即生效）、巩固（三档阈值 + watermark，失败不推水印）、"记住"硬契约、`memory {list,show,sync,verify,restore}`。

**PART 3 语料与按需推荐**：`source_id` 幂等入库（简介没变就跳过且不重嵌入）、中文混合检索（jieba 预分词 + LIKE 兜底 + RRF + 硬过滤 + 口味软加权）、推荐去重（7 天窗口，对话与日报共用）、三个工具（`search_media` / `recommend_media` / `daily_brief`）、`ops explain-search` 五段可解释、`rag eval` golden 回归、嵌入不可用时降级为纯 FTS5（trace 记 `E_EMBED_UNAVAILABLE`，用户无感）、外部内容一律 `<external_content>` 包裹。

**PART 4 评测·运维·交付**：

- **CI 四步门禁**：`.github/workflows/ci.yml` 按 `ruff` → `pytest -m "not live"` → `skills validate` → `release_gate` 顺序执行，失败即停；嵌入后端固定 `hash`（离线确定性，不下载模型），失败时把 trace / 日志 / 报告作为 artifact 交出来；
- **judge 10 条 rubric**：五类各 2 条，`must_have` 与 `must_not_have` 分开写，离线均分 **4.80**（`yixiang eval judge`）；解析失败该条计 0 并留痕；
- **常驻调度**：巩固兜底 23:30（幂等靠 §7.7.1 水印）、每日汇总 23:50（`usage:YYYY-MM-DD`）、记忆巡检周日 22:00（`verify:YYYY-Www`）；`run_job` 异常隔离——**失败可见，但绝不杀主链路**，留痕在 `data/logs/jobs-*.jsonl`；
- **备份与恢复**：`VACUUM INTO` 日快照 + `data/` 私有仓提交 + 30 天回收；`scripts/restore_drill.py` 把快照当唯一库源重建一次并逐项对账（表结构 / 行数 / 记忆三方一致性）；
- **本机测试前端**：`yixiang web` 起一个零依赖的本地控制台（内置标准库 HTTP 服务，只绑 `127.0.0.1:8765`），七个面板分别管对话、历史对话、人设与记忆、模型配置、提示词、QQ 设置、链路 trace；上传的文件落 `data/uploads/`，由注册表里的第 19 个工具 `read_file` 按需读取（§9.2 / §9.5 另有三个 Bangumi live 工具），外部内容一律 `<external_content>` 包裹；
- **文档面**：本 README（含数据边界表）、[`docs/architecture.md`](./docs/architecture.md)、[`scripts/demo-week4.md`](./scripts/demo-week4.md)、三张数字卡 [`docs/NUMBERS.md`](./docs/NUMBERS.md)。

**边界（写在明面上）**：cron 定时晨报推送与唤醒补发、QQ 入口都已落地（`gateway/sinks.py` 三个投递通道、`gateway/qq.py` 的 OneBot v11 反向 WS、`scheduler/` 的晨报 job），但**默认关闭**——要开才生效（`YIXIANG_SCHEDULER_ENABLED` / `YIXIANG_QQ_ENABLED`），入口是 `yixiang serve`；它们**不参与任何门禁**。语料入库的**真实抓取**与**真实嵌入模型**需要网络；离线的等价入口是 `--source local --file evals/fixtures/media_sample.json` + `YIXIANG_EMBED_BACKEND=hash`。

PART 3 交付时留下的 6 个决策点记在 [`docs/TODO-AFTER-PART-4.md`](./docs/TODO-AFTER-PART-4.md)（PART 4 收口后再逐条过；其中 T3「评测关口味」的口径已写进本 README 的评测段与 `docs/NUMBERS.md`）。

## 文档

| 文档 | 回答什么问题 |
|---|---|
| [`docs/PRODUCT.md`](./docs/PRODUCT.md) | 做什么、给谁用、为什么是这个形态（WHAT / WHY） |
| [`docs/TECH-DESIGN.md`](./docs/TECH-DESIGN.md) | 怎么实现：数据流、ADR、冻结接口、安全与成本模型（HOW） |
| [`docs/architecture.md`](./docs/architecture.md) | 一页看懂：入口 / Loop / 记忆 / RAG / 评测四支柱 + 分层纪律 |
| [`docs/NUMBERS.md`](./docs/NUMBERS.md) | 三张数字卡（成本 / 命中率 / 用例数）的原始数据与算法 |
| [`docs/parts/`](./docs/parts/) | 按周切分的 4 个工作包与各自的验收命令 |
| [`scripts/`](./scripts/) | 每周演示剧本（照读即可，含无网络兜底）与恢复演练脚本 |
