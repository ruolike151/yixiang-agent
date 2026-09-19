# yixiang — 本地个人助手 Agent 产品文档

> 版本 v0.4 · 2026-09-19 · 状态：**关键项已定，可进入实现**
> 项目代号 **yixiang**，中文名 **以湘**（原 Momo）。下游命名（包名 / CLI / env 前缀）统一见 TECH-DESIGN §0.1。
> 分部实施文档：[`docs/parts/`](./parts/README.md)——按 W1–W4 切成 4 个工作包，每个包有独立的目标/验收/接口契约/任务分解/DoD。
> 变更记录：v0.4 —— 中文名定为**以湘**；**晨报改为按需形态**（用户想要时由 agent 组装并发出，定时推送降为 P2 路线图）；嵌入后端定为 **fastembed**；judge 在 P0 先用与主模型同一模型（同源自偏好记为已知局限）；`data/` 建私有 Git 备份仓；首期语料定为 500 部。v0.3 —— 代号改 yixiang；交付顺序改为**本地可运行优先**（QQ 从 P1 降为 P2 路线图）；流式输出列为 P0 体验项；明确 soul/user/memory.md 属 data/ 运行时数据。v0.2 —— 新增记忆治理设计（memory.md 人工可编辑与 id 双向同步、"记住"直达指令、对话式记忆管理，§5.3.6）。

---

## 0. 文档说明与来源声明

- 本文档是产品的单一事实来源（PRD + 技术设计合一），后续新功能先改本文再动手。
- 本项目**极大程度参考** [AI-Engineer-from-scratch](https://github.com/ShenSeanChen) 课程第 12 模块的 hermes 剪枝版（waku-agent，~6000 行）：借鉴其四大支柱划分（Harness / Loop / Memory / Eval-Ops）与关键设计（检索门控、三支柱记忆、trace + 发布门禁）。
- **核心代码（loop / memory / gateway / provider）自行实现，以"每一行都能向面试官讲清楚"为验收标准**；工具与评测借鉴其思路后重写。与参考实现的保留/差异清单见 §5.3.6。

## 1. 产品概述

### 1.1 一句话定位

跑在本人 Windows 电脑上、**先在本地 CLI 里跑通**的个人 Agent：有持久记忆（记得我是谁、我喜欢什么）、能管理学习计划与备忘录、基于本地影视知识库做**按需推荐**与闲聊（问"今天有什么安排"就得到一份任务+备忘+推荐的汇总），并且全程可评测、可观测。QQ 入口与定时晨报推送都是 P2 可选扩展（§11 路线图 #7、#8）。

### 1.2 背景与动机

- 真实需求：秋招季需要管理学习进度和备忘，同时是重度影视/动画观众，希望有一个"记得我"的助手而不是每次从零开始的聊天窗口。
- 工程目标：以面试为导向，做一个覆盖 Agent 开发核心考点（tool calling loop、记忆架构、RAG、多端接入、主动触发、评测运维）的完整作品，且**真实日用**，沉淀使用数据与迭代故事。

### 1.3 设计原则

1. **无框架自研**：不依赖 LangChain / LangGraph，核心链路自己写，换取对每一层的理解与可讲述性。
2. **本地优先**：数据全部落在本机（SQLite + Markdown 文件），密钥只进 `.env`，不上云、不外发。
3. **同一大脑，多个入口**：Gateway 只搬文本，CLI / QQ / 定时任务共享同一个 Session 与记忆。
4. **可评测**：改任何 prompt / 模型 / 检索参数都要能跑回归评测，不过门禁不合并。
5. **可扩展**：新工具、新语料、新入口都走既有注册机制，不改核心链路。

### 1.4 成功标准（可量化）

| 维度 | 目标 |
|---|---|
| 功能 | CLI 上可完成：记事/查计划/影视推荐/闲聊，说"今天有什么安排"能得到一份任务+备忘+推荐（定时晨报送达是 P2 加分项） |
| 记忆 | 跨会话记住用户画像与偏好，"上周我说喜欢什么"能答对 |
| 记忆治理 | "记住"指令在评测中 100% 触发写入；memory.md 人工删/改后同步生效；对话中可指导 yixiang 修改/删除记忆 |
| 检索 | 影视 golden 集 20 条，混合检索 top-3 命中率 ≥ 60% |
| 评测 | 确定性评测全绿 + judge 平均分 ≥ 4/5，GitHub Actions 门禁生效 |
| 成本 | 正常日用（每天 30~50 轮 + 1 次按需推荐）API 成本 ≤ 0.5 元/天 |
| 使用 | 连续真实使用 ≥ 14 天，沉淀 trace 与成本数据 |

## 2. 用户故事与核心场景

**场景 1：学习计划管理**
"帮我排一个两周的 RAG 复习计划，每天晚上两小时" → yixiang 调 `create_plan` + `add_task` 生成逐日任务；"今天要学什么" → `list_today` 如实返回，不编造；"算法看完了" → `complete_task` 并记入情景记忆。

**场景 2：备忘录**
"记一下周五中午前交开题材料" → `add_memo`（截止时间由 yixiang 解析相对日期）；推荐汇总里自动带出到期备忘；"那个材料交了" → `finish_memo`。

**场景 3：影视知识库问答与推荐**
"想看类似《怪物》的悬疑番" → `search_media` 混合检索（关键词 + 向量），返回带年份/类型/一句话理由的候选；"这部我看过，一般" → 记入口味画像，影响后续排序。

**场景 4：按需推荐汇总（Agent 组装 + 发出）**
"今天有什么安排？"或"来一条推荐" → yixiang 当场组装：今日学习任务 + 到期备忘 + 一条影视推荐（过滤已推、按口味排序），直接流式回复给用户，并写 `data/briefs/YYYY-MM-DD.md` 便于回看。定时 8:00 主动推送 + 睡眠补发是 P2 路线图 #8（同一个组装逻辑，只换触发源）。

**场景 5：B 站视频分享（P2）**
"找讲 KV Cache 讲得好的视频" → `bilibili_search` 返回标题/UP 主/链接卡片。

**场景 6："记住"直达指令**
"记住我周末喜欢睡到十点""别忘了我不吃香菜" → 会话层识别指令并打标 → yixiang 必须先把事实去重后写入长期记忆（已存在相近记忆则更新），并复述"已记住：……"确认。

**场景 7：记忆治理（人工 + 对话式）**
两条路：
- 人工直改：用户直接编辑 `data/memory.md`，删掉不重要的行、修改过时的描述、甚至手写新记忆——下次同步即生效，删除的记忆不再被注入和检索。
- 对话式："你记得哪些关于我的事？" → yixiang 列出带编号的记忆 → "第 2 条不对，我喜欢的是科幻不是恐怖" / "把第 3 条删了" → yixiang 按指定修改/删除并复述变更。

## 3. 功能需求与优先级

| 优先级 | 功能 | 验收标准 |
|---|---|---|
| P0 | Agent Loop（reason→act→observe） | 工具调用链正确，≤8 轮收敛，工具报错可重试 ≤2 次 |
| P0 | CLI 入口 | 命令行完整对话，含 `[tools used]` 折叠记录 |
| P0 | 流式输出 | 无工具轮逐字可见（首字 <1s）；工具调用轮显示"正在查询…"；流式失败自动降级为非流式 |
| P0 | 三文件核心记忆 + 情景/程序性记忆 + 门控 | 跨会话记忆生效；门控"1+1"不检索、"我上周说喜欢啥"检索 |
| P0 | "记住"指令直达写入 | 触发后必调 save_memory（先去重，相近则更新）；未写入时兜底重试并如实报告 |
| P0 | 记忆治理（人工 + 对话式） | memory.md 人工删/改/增同步生效；对话中可让 yixiang 列出/修改/删除记忆 |
| P0 | 学习计划 / 备忘录工具 | CRUD 正确，相对日期解析正确 |
| P0 | Trace + Usage | 每轮 JSONL 落盘，成本可按天汇总 |
| P2 | QQ 接入（NapCat + OneBot v11，**已延后**） | 见 §11 路线图 #7；不参与 P0/P1 验收门禁 |
| P1 | RAG 影视库（混合检索） | golden 集 top-3 命中 ≥60%，检索延迟 < 1s |
| P1 | 按需推荐汇总 + 口味闭环（`daily_brief`） | 说"今天有什么安排"即得到 任务+备忘+1 条推荐；多次推荐不重复；反馈影响排序 |
| P2 | 定时晨报推送（延后，路线图 #8） | 8:00 准点送达 + 睡眠补发；复用 P1 的组装逻辑，只换触发源 |
| P1 | 确定性评测 + judge + CI 门禁 | Actions 跑离线评测，失败阻止合并 |
| P2 | B 站搜索/分享 | 返回结构化卡片消息 |
| P2 | Pixiv 下载分享 | 登录态可用时按 PID 下载并发图（合规灰区，仅作花活） |
| P3+ | 见 §11 路线图 | — |

## 4. 系统架构

### 4.1 架构图

> 注：QQ 入口已降为 P2 可选（§11 路线图 #7），P0/P1 以**本地 CLI** 为主入口。架构图保留 QQ 分支，用来表达"入口可插拔"这一设计。


```
QQ (NapCat, OneBot v11 反向WS) ─┐
CLI (终端) ─────────────────────┤→ Gateway 层 → Session(工作记忆拼装) → Agent Loop ⇄ Tool Registry
APScheduler(巩固/汇总；P2 加晨报) ┘     ↑                                      │
                                        │            ┌───────────────────────────┤
              soul.md/user.md ──────────┤            ▼                           ▼
              记忆门控检索(小模型) ←─────┘   Memory(SQLite: facts/episodes   Tools: plan/memo/
                                              /skills) + 核心区(memory.md 同步)   media/bilibili/…
                                                                        │
                                        RAG 影视库(media表 + FTS5 + sqlite-vec 混合检索)
                                        Ops: trace.jsonl / usage.jsonl / evals / CI 门禁
```

### 4.2 模块职责

| 模块 | 职责 | 对应参考实现 |
|---|---|---|
| `gateway/` | 只做协议转换与文本搬运：cli / qq / scheduler | `waku/gateway/`（qq 为全新实现） |
| `loop/` | tool-calling 主循环、迭代上限、错误重试 | `waku/loop/agent.py`（重写） |
| `providers.py` | OpenAI-compatible 抽象，按角色路由模型 | `waku/loop/models.py`（重写简化） |
| `memory/` | 三文件核心记忆 + 三支柱 + 门控 + 巩固 | `waku/memory/`（重写，加 user.md） |
| `rag/` | 语料入库管线 + 混合检索 | 无（全新） |
| `tools/` | 工具注册与实现 | `waku/tools/`（重写） |
| `ops/` | trace / usage / 评测 / 门禁 | `waku/ops/`（重写简化） |

### 4.3 一次对话的完整数据流

1. QQ/CLI 收到消息 → Gateway 规整为 `{session_id, source, text}` 入队
2. Session 拼装工作记忆（每轮现拼，不缓存）：`soul.md` + `user.md` + `memory.md`（核心区三文件）+ 当前时间/模型信息 + 近 N 轮历史 + 门控检索到的记忆与相关 skill + （若涉及影视）RAG 结果
3. Loop 调主模型 → 产生 tool_calls → 执行 → observation 回填 → 循环直至模型给出最终回复或达 8 轮
4. 回复经 Gateway 发回；`add_exchange` 把工具活动折叠为 `[tools used: …]` 写入历史（防止下一轮重复调用）
5. 异步落盘：chat_log、trace JSONL、usage；每 N 轮触发巩固

### 4.4 按需推荐汇总数据流（P1）＋ 定时推送（P2）

**按需（P1，先做）**：用户说"今天有什么安排"（或跑 `yixiang brief`）→ `daily_brief` 工具读今日 plan_items + 到期 memos + 口味画像（近期反馈 episodes + user.md 偏好）→ `recommend_media`（混合检索 → 过滤 recommend_log 已推 → 按口味加权排序）→ 模板 + 模型润色 → **流式回复给用户** → 写 `data/briefs/YYYY-MM-DD.md` + recommend_log 落库。

**定时（P2，路线图 #8）**：把上面前半段装进 APScheduler cron(8:00)，末尾的"流式回复"换成 sink 投递（CLI 启动打印 / 写文件 / Windows 通知 / QQ），睡眠错过则唤醒补发。**组装逻辑完全复用，只换触发源与出口。**

## 5. 核心子系统设计

### 5.1 Agent Loop

- 伪代码与参考一致：`while not done: resp = llm(messages, tools); if tool_calls: run → observe; else: done`
- `max_iterations = 8`；单工具失败返回结构化错误文本（`{"error": "..."}`）交回模型，同一工具连续失败 2 次则放弃并如实告知
- 工具调用记录折叠进 assistant 历史（`[tools used: …]`），杜绝"重复订日历"类 bug

### 5.2 Provider 抽象（按角色路由）

- 统一走 OpenAI-compatible 接口（`base_url` + `api_key`），DeepSeek / GLM / 本地 vLLM/Ollama 同一适配器
- **角色路由**是本设计特色：主对话 / 检索门控 / judge / 摘要巩固可指向不同模型，配置示例：
  ```ini
  YIXIANG_MAIN_MODEL=deepseek-chat          # 主对话
  YIXIANG_GATE_MODEL=deepseek-chat          # 门控（后续切本地小模型）
  YIXIANG_JUDGE_MODEL=deepseek-chat         # 评测裁判（P0 先用同 main，见 TECH-DESIGN §13.4）
  YIXIANG_EMBED_BACKEND=fastembed           # 本地嵌入后端（onnx，约 100MB）
  YIXIANG_EMBED_MODEL=BAAI/bge-small-zh-v1.5  # 512 维，零成本
  ```
- 升级路径：门控/judge 这类"窄决策"任务逐步切本地小模型，把 API 成本压到接近只剩主对话

### 5.3 记忆系统（★项目特色）

#### 5.3.1 总览：三类记忆 + 两道工序

| 层 | 载体 | 生命周期 | 进工作记忆方式 |
|---|---|---|---|
| 核心记忆 | `soul.md` / `user.md` / `memory.md`（data/ 下） | 永久，可手改 | 每轮全量拼入 |
| 情景记忆 | SQLite `chat_log` + `episodes` | 永久，可蒸馏 | 门控命中时检索注入 |
| 程序性记忆 | `skills/*/SKILL.md` | 永久 | 关键词匹配注入 |

两级结构：**核心区**（三个 Markdown 文件，每轮全量注入，人机共治）与**检索区**（SQLite 长尾记忆，经门控命中才注入）。两道工序：**检索门控**（查前先判断）与**巩固**（批量蒸馏、向核心区提案）。

#### 5.3.2 核心记忆三文件（对齐 hermes 风格）

| 文件 | 内容 | 谁能改 | 上限 |
|---|---|---|---|
| `soul.md` | yixiang 的人格、行为守则、工具使用纪律（如"相对日期自己解析，不要问用户现在几点"） | `update_soul` 工具**只追加** "## Learned rules"（不能自删诚实条款）；完整重写仅限人 | 3000 字符 |
| `user.md` | 用户画像：作息、身份（秋招 agent 岗候选人）、偏好（喜欢的影视类型、口味）、约束 | `update_user` 工具追加/更新条目；人可手改 | 4000 字符 |
| `memory.md` | 精选事实区（人机共治）：当前最重要的持久事实，每条带 id 标注；既是每轮注入的核心记忆，也是人直接编辑记忆的入口 | 人：直接编辑文件（删/改/增）；yixiang：`save_memory` / `manage_memory` 工具（原子地同时更新 DB 与文件）；巩固：追加候选条目，不覆盖人工内容 | 150 行内 |

设计理由：soul（我是谁）/ user（用户是谁）/ memory（我知道什么）三问分离，等价于 Letta/MemGPT 的 persona block + human block 思路，但用 Markdown 文件实现——可 diff、可 git 版本化、可手改，这是面试讲解的好素材。memory.md（核心区）与 facts 表（检索区，FTS5 + 向量）按条目级 id 双向同步：文件是人机共治的接口，DB 是长尾与检索的载体，`- [12] 内容` 中的 id 即 facts 主键，保证同步无损（机制见 §5.3.6）。

> **这三个文件不在代码目录里。** 它们是 `data/` 下的运行时数据（gitignore）：记忆是用户的资产，不是代码。仓库只提供 `templates/` 里的初版模板，首次 `yixiang doctor` 时复制过去。这样做有三个好处：改记忆不等于改代码；私人记忆不会误提交进公开仓库；`data/` 可以单独做一个私有仓做版本化（TECH-DESIGN §12.4）。

#### 5.3.3 情景记忆

- `chat_log` 逐轮记录（session_id / source / user_text / reply / tools_json / created_at）
- 巩固工序每 N=20 轮把未巩固的 chat_log 蒸馏成 `episodes(happened_at, summary)`，如"2026-09-20：用户开始两周 RAG 复习计划，偏好晚上学习"；同时向 memory.md **追加**候选条目（不覆盖人工内容，治理机制见 §5.3.6）
- episodes 参与门控检索，是口味画像的数据来源

#### 5.3.4 程序性记忆（skills）

- `create_skill` 工具可让 yixiang 把用户教的重复流程写成 SKILL.md（前置校验：slug 合法、不覆盖已有、需用户确认）
- 每轮按消息关键词匹配相关 skill 注入 system prompt

#### 5.3.5 检索门控（Hero 设计，保留并强化）

- 写在每轮检索之前：小模型只回答一个 JSON 问题——`{"retrieve": bool, "query": str, "reason": str}`
- 动机：默认全量检索(a)慢 (b)无关记忆会带偏回答（过度解读）
- **fail-open**：门控自身出错时选择检索——"宁可给旧记忆，不可丢记忆"
- 成本：一次几百 token 的小模型调用；后续切本地模型归零

#### 5.3.6 记忆写入与治理（"记住"指令 · 人工编辑 · 对话式管理）

**写入路径（三条）：**

| 路径 | 触发 | 行为 |
|---|---|---|
| "记住"指令 | 消息以"记住 / 帮我记住 / 别忘了"开头，或明确表达记住意图 | 会话前置处理打标 → system prompt 注入硬性契约：先 `manage_memory(action=search)` 去重，存在相近记忆则 update，否则 `save_memory` 新增；回复必须复述"已记住：……"。**后验兜底**：已打标但本轮未调用写入工具 → 强提示重试 1 次，仍失败则如实报告"没记住"，绝不假装记住 |
| 对话中自主判断 | 用户说出持久事实（偏好/人物/项目），soul.md 使用纪律要求落库 | 调 `save_memory` |
| 巩固提案 | 每 N=20 轮 | 从未巩固 chat_log 蒸馏候选，**追加**到 memory.md（带 new 标记），不覆盖人工内容；人可随时删 |

与备忘录的边界（写入 soul.md 纪律并用评测固化）："记一下周五交材料"（有截止、一次性）→ `add_memo`；"记住我喜欢悬疑番"（持久事实）→ `save_memory`。

**memory.md 人工编辑与同步（人的裁决权最高）：**

```markdown
# Memory — yixiang 记得的事（人工可直接编辑，删除行即删除该记忆）
## 用户
- [12] 2026 届本科，秋招目标 Agent 开发岗
## 偏好
- [7] 喜欢悬疑/科幻题材；日常番轻度观众
- [15] 晚上学习效率高，计划排在 20:00–22:00
```

同步规则（启动时 + 每次巩固前执行 `memory/sync.py`，**以文件为准**）：
- 删除某行 → 对应 fact 软删（不再注入、不再被检索；回收站表可恢复）
- 修改行内文字 → 更新对应 fact 内容
- 新增无 id 的行 → 导入为新 fact（人也可以手写记忆）
- 无法解析的行 → 原样保留为手写笔记并记日志，不报错不丢内容
- yixiang 侧的一切修改必须走工具，工具原子地同时更新 DB 与文件，保证两侧一致

**对话式记忆管理：**
"你记得哪些关于我的事？" → `manage_memory(action=search)` 返回带 id 的编号列表 → "第 2 条不对，改成……" / "把第 3 条删了" → 按 id update/delete（同样双写 DB 与文件）并复述变更。

#### 5.3.7 与参考实现的保留/差异清单

| 项 | 参考（waku） | 本项目 |
|---|---|---|
| SOUL.md + update_soul 只追加 | ✅ | **保留** |
| 检索门控 fail-open | ✅ | **保留**，门控模型可独立配置/本地化 |
| manage_memory / create_skill | ✅ | **保留**（重写） |
| MEMORY.md 镜像 | ✅ | **升级**：从只读镜像升级为人机共治的精选事实区，带 id 双向同步（§5.3.6） |
| "记住"直达指令 | ❌（仅靠 prompt 自觉） | **新增**：触发词打标 + 硬性工具契约 + 后验兜底（§5.3.6） |
| user.md 用户画像 | ❌ 无 | **新增**（对齐 hermes 原版三文件） |
| 记忆检索 | FTS5 纯关键词 | **升级**：FTS5 + sqlite-vec 混合 |
| 巩固 | 每 N 轮蒸馏 | **保留**，同时生成 memory.md |
| Telegram/Apple 生态 gateway | ✅ | **替换**为 QQ（NapCat/OneBot v11） |
| RAG 影视库 | ❌ 无 | **新增**（含语料管线与口味闭环） |

### 5.4 RAG 影视库

#### 5.4.1 语料来源与入库管线（为持续更新而设计）

- 来源：Bangumi API（番剧，免 key）为主，TMDb（电影，免费 key）为辅；**首期 500 部**（Bangumi 番剧约 400 + TMDb 电影约 100，已拍板）
- 管线命令化、幂等：`python -m yixiang.rag.ingest --source bangumi --tags 悬疑,科幻 --pages 5`
  - 按 `bangumi_id` upsert：已存在且简介未变则跳过，新增/变更才重新嵌入（省时省钱）
  - 元数据字段：标题 / 类型(电影·TV·番剧) / 年份 / genres / 评分 / 简介 / 封面 URL
- **不做暴力 chunk**：结构化字段 + 简介整体作为检索单元（一部作品一条记录），嵌入文本 = 标题×2 + 类型 + genres + 简介前 500 字

#### 5.4.2 混合检索

1. FTS5 关键词召回（标题/简介，top 20）
2. sqlite-vec 向量召回（bge-small-zh-v1.5，512 维，top 20）
3. RRF（k=60）融合排序列表
4. 元数据过滤：类型/年份硬过滤、已推（recommend_log）排除
5. 口味加权：user.md 偏好 tags + 近期 episodes 反馈（"好看"+ / "一般"−）做软加权
6. 返回 top-3，附"推荐理由"所需的命中字段

#### 5.4.3 检索质量评测

- 20 条 golden QA（"类似《怪物》的悬疑番"→ 期望含《虚构推理》类同题材作品；"轻松日常番"→ 期望集合），指标 top-3 命中率，纳入确定性评测

### 5.5 Gateway 层

#### 5.5.1 CLI（P0）

终端 REPL，`source=cli` 打标；`/new` 开新会话、`/history` 切换会话。

#### 5.5.2 QQ（P2，已延后）

- 协议：NapCat（Windows 可执行）+ OneBot v11，**反向 WebSocket** 接入 yixiang（无需公网 IP）
- 可靠性：断线指数退避重连（1s→2s→…→60s 封顶）；OneBot 事件可能重复投递，按 `message_id` 幂等去重
- 安全：`YIXIANG_QQ_ALLOWED` 白名单只响应本人 QQ 号；**群消息默认忽略**（防第三方注入 prompt），仅私聊
- 富媒体：收到图片先落盘再把路径给模型；回复支持 CQ 码发图（影视封面、Pixiv 图）

#### 5.5.3 Scheduler

**P1 只跑三个后台 job**（`yixiang serve`，`YIXIANG_SCHEDULER_ENABLED=1` 时启用）：

- 每 N=20 轮触发巩固 + 每晚 23:30 兜底；每日 23:50 usage 汇总；每周日 22:00 记忆巡检
- 任务异常捕获记 trace，绝不杀主进程

**P2 才加晨报 job**（路线图 #8）：APScheduler cron(8:00) + "应发日期"记录，启动时发现今天未发且当前时间在 8:00~12:00 则补发（标注"（补发）"）。P0/P1 阶段 `YIXIANG_SCHEDULER_ENABLED` 默认关，推荐走 §4.4 的按需路径。

### 5.6 工具系统

Tool 约定：`Tool(name, description, input_schema, fn)`，注册进全局 registry；错误以 `{"error": …}` 字符串返回给模型。

| 工具 | 优先级 | 参数要点 | 说明 |
|---|---|---|---|
| `add_memo` / `list_memos` / `finish_memo` | P0 | content, due_at(ISO) | 备忘录 CRUD |
| `create_plan` / `add_task` / `list_today` / `complete_task` | P0 | 日期、内容、est_minutes | 学习计划 |
| `save_memory` | P0 | subject, content | 写入持久事实（"记住"指令与对话中自主判断统一走此工具；写入前先去重） |
| `manage_memory` | P0 | action: search/update/delete | 记忆治理：列出（带 id）→ 在用户指导下修改/删除；先查 id 再改 |
| `update_soul` / `update_user` / `create_skill` | P0 | rule / profile 条目 / skill | 三文件与程序性记忆 |
| `search_media` / `recommend_media` | P1 | query, filters | 混合检索 + 推荐排序 |
| `daily_brief` | P1 | scope?(today/tomorrow) | 按需组装：今日任务 + 到期备忘 + 1 条推荐，并写 `data/briefs/` |
| `bilibili_search` | P2 | keyword | 返回标题/UP/链接 |
| `pixiv_download` | P2 | pid | 登录态依赖，仅花活 |

**新工具扩展指南**（后续追加功能照此三步，不改核心链路）：
1. `tools/` 下新建文件，实现函数并包成 `Tool`（schema 用 JSON Schema）
2. 在 `tools/__init__.py` 注册
3. 在 `evals/deterministic/` 加至少 1 条触发/参数断言用例

### 5.7 运维与可观测

- **trace**：`data/traces/YYYY-MM-DD.jsonl`，字段：`ts / session / source / user_text / gate{retrieve,query,reason} / tool_calls[{tool,args,ok,ms}] / tokens{in,out} / cost / model / error`
- **usage**：`data/usage.jsonl` 按轮追加，`yixiang ops usage --day` 汇总成本/轮次/工具分布
- 错误处理策略：工具错误→交回模型重试（≤2）；门控错误→fail-open 检索；QQ 断线→退避重连；调度错误→记 trace 不中断；LLM 超时→一次重试后如实告知

## 6. 评测方案

### 6.1 确定性评测（pytest，离线优先）

| 用例 | 断言 |
|---|---|
| 备忘触发 | "记一下周五中午前交材料" → 必调 `add_memo` 且 due 解析正确 |
| 计划查询 | `list_today` 只返回今日 items，不编造 |
| 记忆持久化 | 写入 fact → 新会话询问 → 答案正确 |
| "记住"指令 | "记住我周末喜欢睡到十点" → 必调 save_memory；已有相近记忆时走 update 不重复新增 |
| 记忆/备忘边界 | "记一下周五交材料" → add_memo 而非 save_memory |
| memory.md 人工同步 | 删除文件中某 id 行并重启 → 该 fact 软删、不再注入/检索；新增无 id 行 → 导入为新 fact |
| 对话式治理 | "列出记忆 → 改第 2 条 → 删第 3 条"全链路断言（DB 与文件两侧一致） |
| 门控跳过/命中 | "1+1=?"→false；"我上周说喜欢什么来着"→true |
| 混合检索 | golden 20 条 top-3 命中率 ≥60% |
| 按需推荐组装 | "今天有什么安排" → 必调 `daily_brief`；返回含今日任务 + 到期备忘 + 1 条推荐；写 `data/briefs/` 与 recommend_log |
| 推荐去重 | 连续两次推荐集无交集 |
| QQ 幂等（P2 生效，P0/P1 skip） | 同 message_id 二次投递只处理一次 |
| 定时补发（P2 生效，P0/P1 skip） | 模拟 8:00 未运行、9:30 启动 → 补发一次且标注"（补发）"，二次启动不重发 |
| 三文件边界 | update_soul 不可删除既有规则；user.md 超限拒绝 |

### 6.2 LLM-as-judge

10 条开放对话（闲聊/推荐/计划混合），judge 模型按"事实正确/工具合理/语气"1~5 打分，均值 ≥4 通过。

### 6.3 发布门禁

GitHub Actions：push/PR 自动跑 `pytest evals/deterministic -m "not live"` + ruff，失败阻止合并。原则：**线上修一个 bug，必补一条回归用例**。

## 7. 数据模型（SQLite `data/state.db`，DDL 摘要）

```sql
CREATE TABLE facts(id INTEGER PRIMARY KEY, subject TEXT, content TEXT,
                   deleted INTEGER DEFAULT 0, updated_at TEXT);  -- deleted=1 为软删（memory.md 删行同步所致），注入与检索均排除，回收站可恢复
CREATE VIRTUAL TABLE facts_fts USING fts5(subject, content);      -- 同步维护
CREATE VIRTUAL TABLE facts_vec USING vec0(embedding float[512]);

CREATE TABLE chat_log(id INTEGER PRIMARY KEY, session_id TEXT, source TEXT,
                      user_text TEXT, reply_text TEXT, tools_json TEXT, created_at TEXT);
CREATE TABLE episodes(id INTEGER PRIMARY KEY, happened_at TEXT, summary TEXT, session_id TEXT);

CREATE TABLE plans(id INTEGER PRIMARY KEY, title TEXT, goal TEXT,
                   start_date TEXT, end_date TEXT, status TEXT);
CREATE TABLE plan_items(id INTEGER PRIMARY KEY, plan_id INTEGER, date TEXT,
                        content TEXT, est_minutes INTEGER, status TEXT);   -- todo/done/skipped
CREATE TABLE memos(id INTEGER PRIMARY KEY, content TEXT, due_at TEXT,
                   done INTEGER DEFAULT 0, created_at TEXT);

CREATE TABLE media(id INTEGER PRIMARY KEY, source_id TEXT UNIQUE, title TEXT,
                   mtype TEXT, year INTEGER, genres TEXT, rating REAL,
                   synopsis TEXT, cover TEXT, embed BLOB);
CREATE VIRTUAL TABLE media_fts USING fts5(title, synopsis);
CREATE VIRTUAL TABLE media_vec USING vec0(embedding float[512]);
CREATE TABLE recommend_log(id INTEGER PRIMARY KEY, media_id INTEGER,
                           recommended_on TEXT, channel TEXT, feedback TEXT); -- good/bad/none

CREATE TABLE scheduled_runs(id INTEGER PRIMARY KEY, job TEXT, run_date TEXT, status TEXT); -- 定时任务幂等/补发依据（P2 晨报用）
```

## 8. 目录结构

```
yixiang/                       # 包名 = CLI 命令 = 日志前缀
  app.py  config.py  providers.py
  loop/agent.py
  gateway/{cli.py, sinks.py, scheduler.py, qq.py (P2)}
  memory/{core_files.py, semantic.py, episodic.py, procedural.py, gate.py, consolidate.py, sync.py, memory_admin.py}
  rag/{ingest.py, retrieve.py, embed.py, taste.py}
  tools/{registry.py, memo.py, plan.py, media.py, memory_admin.py, bilibili.py}
  ops/{tracing.py, usage.py, show_trace.py, release_gate.py}
templates/                     # soul.md / user.md / memory.md 的初版模板（入库、可 diff）
evals/{deterministic/, judge/, golden/, fixtures/}
docs/PRODUCT.md                ← 本文档（+ TECH-DESIGN.md）
data/                          # 运行时数据（gitignore）：state.db、soul.md、user.md、memory.md、
                               #   skills/、traces/、usage.jsonl、media/、briefs/、backups/、logs/
scripts/                       # demo 种子数据等
```

> `soul.md` / `user.md` / `memory.md` 是 `data/` 下的运行时数据，不在代码目录里——仓库只放 `templates/`。详见 §5.3.2 与 TECH-DESIGN §1.4。

## 9. 非功能需求

| 项 | 要求 |
|---|---|
| 成本 | ≤0.5 元/天（§1.4 口径）；usage 每日汇总可查 |
| 延迟 | 单轮端到端 <5s（无工具）/ <15s（含工具+检索） |
| 隐私 | 数据全部本地；密钥仅 `.env`（gitignore）；QQ 白名单；群消息忽略 |
| 平台 | Windows 优先开发（本机），代码保持 Linux 兼容（为后续迁移服务器） |
| 依赖 | Python 3.12 + uv；sqlite-vec、httpx、apscheduler、websockets、**fastembed**（嵌入，onnx 约 100MB）；默认**不装 torch** |

## 10. 实施计划（方案二：按模块自顶向下，每周末有可演示里程碑）

| 周 | 模块 | 里程碑（可演示） |
|---|---|---|
| 1 | 基座：仓库/config/provider 抽象/loop/工具注册 + memo、plan 工具/CLI/trace+usage | **CLI 里完整对话，能记事、排计划、看 trace** |
| 2 | 记忆系统：三文件 + episodic + skills + 门控 + 巩固 + 记忆管理工具；确定性评测起步（≥10 条） | **跨会话记忆生效（"上周我说过啥"能答），evals 绿** |
| 3 | RAG 全链路（ingest 管线、混合检索、media 工具）+ 按需推荐（`daily_brief` 组装、去重、写 `data/briefs/`） | **影视问答可用；说"今天有什么安排"能得到完整推荐** |
| 4 | 口味加权推荐 + judge 评测 + CI 门禁 + README/架构图/demo 脚本 + B 站工具；开始连续真实使用 | **简历可写、视频可演示，进入"用-测-改"循环** |

缓冲与砍单顺序：时间超支先砍 Pixiv → B 站工具 → demo 润色；**评测与记忆永不砍**。

> **QQ 与定时晨报推送都已移出四周计划**：它们是 P2 可选扩展（§11 路线图 #7、#8），不影响任何里程碑。推荐在 P1 走按需形态——组装逻辑一次写完，P2 接上 cron 与 sink 就能变成主动推送。先把"本地能跑、连续在用"做出来，再考虑什么时候推、推到哪。

## 11. 后续路线图（文档随功能演进）

1. **更多工具**（按 §5.6 扩展指南）：日历(ICS)、RSS 订阅摘要、浏览器阅读、邮件摘要
2. **语料库持续更新**：ingest 管线支持 `--since` 增量 + 每周自动拉新番季度表；订阅源（追更通知）
3. **本地小模型接管**：门控/judge/嵌入逐步本地化（vLLM + Qwen 小模型），主对话保留云端
4. **记忆可视化 dashboard**：三文件在线编辑 + trace 浏览 + 成本图表
5. **语音入口**、**多用户隔离**（每用户独立 data 目录）
6. 迁移部署到云服务器（Docker Compose：yixiang + NapCat）
7. **QQ 入口（P2）**：NapCat + OneBot v11 反向 WS 接入，复用同一个 loop 与流式输出；白名单 + 群消息忽略（§13）
8. **定时晨报推送（P2）**：把 P1 的 `daily_brief` 装进 APScheduler cron(8:00) + 唤醒补发，出口换成 `gateway/sinks.py`（CLI 打印 / 写文件 / Windows 通知 / QQ）——**内容层不变，只加触发与投递层**

## 12. 面试叙事要点

**电梯稿（30s）**：我做了一个跑在本机、持续在用的个人 Agent（QQ 入口与定时推送为 P2 扩展）：无框架自研 tool-calling loop，hermes 风格的三文件核心记忆（soul/user/memory.md）加检索门控和周期巩固，本地 SQLite 上做 FTS5+向量混合检索的影视推荐，问一句"今天有什么安排"就现场组装出任务+备忘+推荐；全程 trace 落盘、确定性评测加 LLM-as-judge 做 CI 发布门禁，连续真实使用 N 天、日均成本 X 元。

**拆分讲法（被追问"主动式呢"时）**：推荐做成**内容层 / 触发层分离**——`daily_brief` 负责组装（内容层，P1 就完整做完），cron + sink 只负责"什么时候、往哪发"（触发层，P2）。所以它不是"没做主动推送"，而是"先把内容质量做对，再决定推送时机"。这套分层本身也是被追问时的答案。

**三个深挖故事**：
1. **检索门控的取舍**——为什么不是每轮都查记忆（慢 + 过度解读）、fail-open 的理由、成本账
2. **混合检索与推荐闭环**——为什么结构化语料不 chunk、RRF 融合、已推去重与口味加权的排序设计、golden 集命中率数字
3. **评测驱动的迭代**——"改 prompt 必跑 evals"的工作流，举一个被 evals 拦下的真实回归
4. **记忆治理 human-in-the-loop**——两级记忆（核心区三文件全量注入 / 检索区门控注入）；memory.md 作为人机共治接口、条目级 id 双向同步、"人删即软删"；"记住"指令的硬性契约与后验兜底。核心论点：个人助手的记忆，最终裁决权必须在人

**预期追问与答法**：为什么不用 LangGraph（要理解底层，且循环本身 <100 行）；记忆和 RAG 的区别（记忆是写给自己的人格/画像/情节，RAG 是对外部语料的检索）；QQ 消息的 prompt 注入防护（白名单 + 群消息忽略 + 工具白名单）；context 窗口治理（每轮现拼 + 历史窗口 + 记忆外置）；如何变成多用户（数据目录隔离 + gateway 会话映射）。

## 13. 风险与对策

| 风险 | 对策 |
|---|---|
| QQ 协议非官方，账号风控（P2 生效） | 使用小号；仅私聊低频；不做群聊、不发广告；QQ 未接入时该风险为 0 |
| NapCat 需要偶发重新扫码 | 接受；掉线重连 + 启动自检；定时晨报有补发兜底（P2） |
| 本机不常开，定时推送/在线中断 | P1 用按需形态天然规避（用户在场才触发）；P2 定时推送有唤醒补发；路线图含服务器迁移 |
| judge 与主模型同源，分数偏高 | P0 已知局限：分数只作趋势观测、可客观判断项下沉为确定性断言；拿到基线后换一家厂商并重跑校准（TECH-DESIGN §13.4、§17.2-1） |
| 语料质量影响推荐 | 结构化 API 数据源；golden 集监控命中率 |
| 时间超支 | §10 砍单顺序；评测与记忆优先 |
| "参考课程项目"同质化 | §0 来源声明 + §5.3.6 差异清单；核心自写、能白板讲解每行 |

## 14. 已定与待定

### 14.1 已定（本轮拍板，不再讨论）

| 事项 | 结论 |
|---|---|
| 项目命名 | 代号 `yixiang`（原 Momo），中文名 **以湘**；包名 / CLI / env 前缀统一（TECH-DESIGN §0.1） |
| 交付顺序 | 本地可运行优先：QQ 延后到 P2（路线图 #7），先交付 CLI 版本 |
| 流式输出 | 列为 P0（TECH-DESIGN §5.4） |
| 晨报形态 | **改按需触发**（用户想要时 agent 组装并发出）；定时推送 + 补发 + sink 投递降为 P2（路线图 #8） |
| 嵌入后端 | **fastembed**（onnx 约 100MB），默认不装 torch |
| 首期语料 | **500 部**（Bangumi 番剧约 400 + TMDb 电影约 100） |
| `data/` 版本化 | **建私有 Git 备份仓**（只版本化三文件 / skills / briefs，TECH-DESIGN §12.4） |
| 巩固阈值 | 先用 **0.9 / 0.6**，按实测调（TECH-DESIGN §7.7.2） |
| judge 模型 | P0 **先用与主模型同一模型**，分数只作趋势观测；后续换一家厂商再定型（TECH-DESIGN §13.4） |

### 14.2 待定（都有默认建议，不阻塞开工）

完整表见 `TECH-DESIGN` §17.2 / §17.3。摘要：

1. judge 换一家与主模型不同源的厂商（建议换，最晚 W4 D24；先用同源拿基线）。
2. live 用例是否进 PR 门禁（建议不进，改为 nightly + 发版前手动跑）。
3. P0~P2 不做 Web dashboard，demo 用终端录屏（建议接受）。
4. ~~`soul.md` 的 8000 字符上限是否收窄到 3000（TECH-DESIGN N-2）~~ → **已决策：收窄到 3000（W2）**。
5. 是否接受"每周一次文档回填"（TECH-DESIGN N-3）。
6. judge 用例是否公开在仓库里（建议公开 rubric、隐藏 3 条压测用例，TECH-DESIGN N-4）。
7. QQ 是否保留 P2 席位（建议保留骨架与设计，但不列交付物，TECH-DESIGN N-5）。
