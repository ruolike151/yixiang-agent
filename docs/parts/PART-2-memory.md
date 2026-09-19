# PART 2 — 记忆系统（W2）

> 版本 v1.0 · 2026-09-19 · 阶段：W2（D8–D14）
> 依赖：PART 1（`Settings` / `Tool` / `run_loop` / `SessionManager` / `FakeProvider`） · 被谁依赖：PART 3 / 4
> 上游设计：[TECH-DESIGN §7](../TECH-DESIGN.md)（记忆系统全章）、§13.2–§13.3（FakeProvider 与用例）、§16.2 W2
> 一句话交付：**跨会话记忆生效（"我上周说喜欢什么来着"能答对），人直接改 `memory.md` 立即生效，"记住"指令 100% 落库。**

---

## 1. 目标与验收

| 验收项 | 命令 | 通过标准 |
|---|---|---|
| 跨会话记忆 | `uv run yixiang chat` → `/new` 后问"我上周说喜欢什么来着" | 答对，且系统里注入了检索段 |
| 门控门禁 | `pytest evals/deterministic/test_gate.py` + `yixiang eval gate` | **漏检率 = 0**；误检率 ≤30%；规则跳过率 ≥15% |
| 人工编辑生效 | 手改 `data/memory.md`（删一行/改一行/加一行）→ 重启 | 删除的记忆不再注入与检索；新增的被导入并分配 id |
| "记住"契约 | "记住我周末喜欢睡到十点" | 必调 `save_memory`；已有相近记忆走 update 不重复新增；回复含"已记住：…" |
| 巩固质量 | `yixiang eval consolidation` | 不重复率 ≥95%；不臆造（负样本断言通过） |
| 自检 | `uv run yixiang doctor` | 三文件存在、上限内；`memory verify` 无漂移 |

## 2. 范围边界

**做**：三文件读写与上限、`facts` 语义记忆（FTS5 + 向量）、`episodes` 情景记忆、`skills` 程序性记忆、检索门控、巩固、`memory.md ⟷ facts` 双向同步、记忆治理工具、`"记住"` 硬契约。

**不做**：

- ❌ 影视语料的检索与推荐 —— PART 3（本部分只实现 `retrieve_memory`，与媒体的 `retrieve_media` 共用 RRF 函数）；
- ❌ judge 评测与 CI 门禁 —— PART 4（本部分只保证确定性用例绿）；
- ❌ 每周巡检的自动化 job —— 本部分实现 `memory verify` 命令，定时触发在 PART 4/P2 接；
- ❌ 记忆的可视化 dashboard —— 明确不做（PRODUCT §11 #4 只在路线图）。

## 3. 文件清单

| 文件 | 职责 | 关键点 |
|---|---|---|
| `yixiang/memory/core_files.py` | `soul.md` / `user.md` / `memory.md` 读写、上限校验、**原子写**（临时文件 + rename） | 三文件是 `data/` 下的运行时数据 |
| `yixiang/memory/semantic.py` | `facts`：FTS5 + vec 混合检索、去重、软删、恢复 | 与 PART 3 共用 `rrf()` |
| `yixiang/memory/episodic.py` | `chat_log` / `episodes` | episodes 只用向量检索（§7.6） |
| `yixiang/memory/procedural.py` | `SKILL.md` 加载、关键词匹配、installer | `skills/<slug>/SKILL.md` |
| `yixiang/memory/gate.py` | 检索门控（prompt、解析、规则预过滤、fail-open） | §7.5 |
| `yixiang/memory/consolidate.py` | 每 N 轮蒸馏 + watermark + 三档阈值 | §7.7 |
| `yixiang/memory/sync.py` | `memory.md ⟷ facts` 双向同步（**文件为准**） | §7.4，本项目最容易出 bug 的地方 |
| `yixiang/memory/memory_admin.py` | `manage_memory` 的实现（search / update / delete / restore） | 与 `sync.py` 共用双写路径 |
| `yixiang/tools/memory_admin.py` | 三个工具的 `Tool` 包装：`save_memory` / `manage_memory` / `create_skill`（+ `update_soul` / `update_user`） | 写进 `build_registry()` |
| `yixiang/runtime/session.py`（改） | 装配段接入门控检索结果与 skill 匹配 | 每轮现拼，不缓存 |
| `evals/deterministic/{test_memory_write.py,test_memory_sync.py,test_memory_capacity.py,test_gate.py}` | L1/L2 用例 | 见 §7 |
| `evals/golden/{gate.jsonl,dedup.jsonl}` | 门控标注集 40 条、相似/不相似对 | §7.5.3、§7.10 |

## 4. 接口契约

**Consumes**（来自 PART 1）：`Settings`、`Tool` / `ToolRegistry.build_registry()`、`ChatModel.complete()`（非流式）、`FakeProvider`、`ops/tracing` 的 `turn_id`。

**Produces**（PART 3/4 依赖）：

```python
# memory/semantic.py
def retrieve_memory(query: str, top_k: int = 5, ep_k: int = 3) -> MemoryHits: ...
def save_fact(subject: str, content: str) -> tuple[int, str]: ...   # (id, "insert"|"update")
def soft_delete_fact(fact_id: int) -> None: ...

# memory/gate.py
async def should_retrieve(message: str, provider: ChatModel) -> GateDecision: ...
@dataclass
class GateDecision:
    retrieve: bool; query: str; reason: str
    source: Literal["rule", "model", "fail_open"]

# memory/sync.py
def sync_memory_md(conn: sqlite3.Connection) -> SyncReport: ...     # 文件为准

# memory/core_files.py
def read_core_files(data_dir: Path) -> CoreFiles: ...
def write_core_file(data_dir: Path, name: str, text: str) -> None:   # 原子写 + 上限校验

# 共用（PART 3 也 import）
def rrf(rankings: list[list[Hit]], k: int = 60) -> list[Hit]: ...
def preprocess_for_fts(text: str) -> str: ...                        # jieba 分词
```

**数据结构约定**（冻结，改动影响 PART 3/4）：

| 对象 | 约定 |
|---|---|
| `memory.md` 条目 | `- [12] 内容`；`[12]` 即 `facts.id`；无 id 行为人工手写导入 |
| `facts` | `id / subject / content / deleted / updated_at`；`deleted=1` 为软删 |
| `memory.md` 分区 | `## 用户` / `## 偏好` / `## 待确认` / `## 手写笔记`（后两者不注入核心区） |
| 巩固输出 | 严格 JSON：`{episode:{summary}, candidates:[{section,content,confidence}]}` |
| 三档阈值 | ≥0.9 进正文；0.6~0.9 进 `## 待确认`；<0.6 丢弃（**已拍板，先用**，按 `dedup.jsonl` 实测再调） |
| 水印 | `meta.last_consolidated_chat_id`，**只在整个批次成功后推进** |
| 文件上限 | `soul.md` 8000 字符 / `user.md` 4000 字符 / `memory.md` 150 行 |

## 5. 关键设计点（硬约束）

1. **`memory.md` 与 `facts` 的同步必须走"条目级 id"**。没有 id 就必然丢数据——文件与 DB 各自增删后无法对齐（§7.4）。这是本项目自定义的设计，也是面试最值得讲的一处。
2. **冲突时文件为准**。人删一行 = 对应 fact 软删（不注入、不检索、回收站可恢复）；人改一行 = 更新 fact 内容；人加无 id 行 = 导入新 fact。无法解析的行**原样保留为手写笔记**，不报错不丢内容。
3. **yixiang 侧一切修改必须走工具**，工具原子地同时更新 DB 与文件；不允许只写一侧。
4. **门控的阈值不对称是刻意的**：漏检率必须为 0（漏检 = 失忆，用户直接感知），误检率容忍到 30%（只是多注入几条记忆）。所以门控失败一律 **fail-open**。
5. **门控解析要"第一个 `{` 到最后一个 `}`"再 `json.loads`**，且 `max_tokens=600`——带思考链的模型会先输出思考块；没有 `{` 视为"模型没给可用答案"而非"不需要检索"（§7.5.1）。
6. **只有确定性判定才走规则预过滤**，模糊判断一律交模型。规则命中率要进 trace，用来评估规则是否还值得保留。
7. **巩固只提炼"跨会话仍成立"的事实**。`"今天学了 3 小时"` 是 episode，不是 fact；情绪化表达（"今天好累"）不总结；不猜测对话里没明说的偏好。
8. **巩固失败不能静默**：LLM 失败 / JSON 解析失败 → 水印不动，下次重试；连续失败 3 次 → 日志与 usage 日报告警。
9. **`"记住"` 指令要有后验兜底**：已打标但本轮未调写入工具 → 强提示重试 1 次，仍失败则**如实报告"没记住"**，绝不假装记住。
10. **原子写**：三文件写入必须走"临时文件 + rename"，避免崩在写一半时留下半截记忆文件。

## 6. 任务分解

| 日 | 任务 | 验收 |
|---|---|---|
| **D8~D9** | 三文件读写 + 原子写 + 上限校验；`facts` 表 + FTS5 + 向量表 + `sqlite-vec` 加载 | D-14、D-15、D-26 绿 |
| **D10** | 检索门控：prompt（§7.5.1）、解析容错、规则预过滤、fail-open | D-09、D-10、D-16 绿；`gate.jsonl` 漏检 = 0 |
| **D11** | `memory.md ⟷ facts` 双向同步（条目级 id、文件为准、原子双写） | **D-06、D-07 绿（本项目最容易出 bug 的地方，留足时间）** |
| **D12** | `"记住"` 三阶段 + `save_memory` / `manage_memory` / `update_soul` / `update_user` | D-04、D-05、D-08 绿 |
| **D13** | 巩固（水印、三档阈值、质量约束）+ skills 加载与匹配 | D-03、D-17、D-18 绿 |
| **D14** | 用例补到 ~20 条 + `evals/golden/gate.jsonl` 40 条标注 + `scripts/demo-week2.md` | **跨会话演示剧本跑通**；`yixiang memory verify` 无漂移 |

## 7. 测试与用例

| 编号 | 内容 |
|---|---|
| D-03 | 巩固：跨会话提取事实，episode 生成 |
| D-04 | "记住"指令：必调 `save_memory`；重复时走 update（`facts` 行数不变） |
| D-05 | 记忆/备忘边界："记一下周五交材料" → `add_memo` 而非 `save_memory` |
| D-06 | 人工同步（删）：删除 `memory.md` 带 id 的行 → 重启 → 软删、不再注入/检索 |
| D-07 | 人工同步（增）：新增无 id 行 → 重启 → 导入为新 fact、分配 id 并回写 |
| D-08 | 对话式治理："列出记忆 → 改第 2 条 → 删第 3 条" → DB 与文件**逐字符比对**一致 |
| D-09 | 门控跳过："1+1=?" → false；system 里无检索段 |
| D-10 | 门控命中："我上周说喜欢什么来着" → true；检索段含对应 fact |
| D-14 | 三文件边界：`update_soul` 尝试删除既有规则 → 拒绝、文件未变 |
| D-15 | `user.md` 超限：写入超上限 → 拒绝并返回上限值、文件未变 |
| D-16 | 门控 fail-open：门控抛异常 → 仍检索；trace 记 `E_GATE_FAIL_OPEN`；用户无感 |
| D-17 | 巩固不重复：同批对话跑两次 → 第二次不写入、`facts` 行数不变 |
| D-18 | 巩固不臆造：对话未出现偏好 → `candidates` 不得含新偏好（负样本断言） |
| — | `test_memory_capacity.py`：容量上限、淘汰、置顶保护 |

## 8. 风险与砍单

| 风险 | 对策 |
|---|---|
| **双向同步丢数据**（最大风险） | 条目级 id + 单独一个测试文件专门覆盖；D-06/D-07/D-08 三条用例是硬门禁 |
| 记忆污染（巩固写入噪声） | 三档阈值 + `## 待确认` 缓冲区 + "不重复率 ≥95%" 门禁；**宁可先保守（少写）**——记忆缺失下次再说一次就行，污染会让助手"记错你" |
| 门控漏检 | 阈值不对称 + `gate.jsonl` 40 条标注 + 漏检率 = 0 的硬门禁 |
| `soul.md` 写满占 token | 上限校验 + 容量淘汰；8000 字符是否收窄见 TECH §17.3 N-2（**待你决策**） |
| 时间超支 | 砍 skills（程序性记忆）的自动 installer，只保留手写 `SKILL.md` 加载；`update_soul` 的"只追加"校验不可砍 |

**不可砍**：三文件 + 人机共治同步、检索门控、`"记住"` 硬契约。

## 9. 面试讲点

1. **两级记忆的分工**：核心区（三文件，每轮全量注入，人机共治）vs 检索区（SQLite 长尾，门控命中才注入）。为什么不全量注入（token 账）也不全放检索（延迟账 + 关键人格信息不能"检索不到"）。
2. **`memory.md` 的条目级 id 双向同步**：为什么没有 id 就必然丢数据；"文件为准"这个规则怎么让"人删即软删"成立。
3. **门控的非对称风险设计**：为什么 fail-open 而不是 fail-closed；为什么漏检率门禁是 0 而误检率容忍 30%。
4. **记忆与 RAG 的边界**（高频对比题）：记忆是写给自己的（人格/画像/情节），RAG 是读外部的（语料库）；两者的检索实现共用 RRF，但**写入路径完全不同**——记忆写得极谨慎，语料是批量灌入。
5. **`"记住"` 的硬契约**：用户明确说"记住"时不能静默失败，所以要有"打标 → 强制工具契约 → 后验校验 → 如实报告"四步。

## 10. 交接检查表（DoD）

- [ ] `gate.jsonl` 40 条标注完成，**漏检率 = 0**
- [ ] D-03 ~ D-10、D-14 ~ D-18 全绿；`pytest -m "not live"` 仍 ≤30 秒
- [ ] `yixiang memory list` / `memory show <id>` / `memory sync` / `memory verify` 可用
- [ ] 手改 `data/memory.md` 后重启，效果符合 §5 第 2 条
- [ ] `dedup.jsonl` 生成，不重复率 ≥95%
- [ ] `retrieve_memory` / `rrf` / `preprocess_for_fts` 的签名与本文 §4 一致（PART 3 按此开工）
- [ ] `skills/` 目录能被加载并注入（关键词匹配）
