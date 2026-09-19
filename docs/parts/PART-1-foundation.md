# PART 1 — 基座与 Agent Loop（W1）

> 版本 v1.0 · 2026-09-19 · 阶段：W1（D1–D7）
> 依赖：无 · 被谁依赖：PART 2 / 3 / 4（全部）
> 上游设计：[TECH-DESIGN §1](../TECH-DESIGN.md)（运行时与代码结构）、§3（配置）、§4（Provider）、§5（Loop）、§9（工具）、§10.1（CLI）、§11（Ops）、§13.2（FakeProvider）、§16.2 W1
> 一句话交付：**`uv run yixiang chat` 里能流式对话、能记事排计划、每一轮都有 trace 与成本账。**

---

## 1. 目标与验收

| 验收项 | 命令 | 通过标准 |
|---|---|---|
| 项目能跑起来 | `uv run yixiang --help` | 有输出，列出 chat / serve / doctor / rag / ops / eval |
| 自检通过 | `uv run yixiang doctor` | 检查项 1~6 全部通过（含从 `templates/` 复制三文件） |
| 对话可用 | `uv run yixiang chat` → "帮我排一个两周的 RAG 复习计划" | 调 `create_plan` + `add_task`，**逐字流式**显示 |
| 确定性用例 | `pytest evals/deterministic -m "not live"` | ≥8 条全绿，且**离线、≤30 秒、零成本** |
| 可观测 | `/trace` 与 `/cost` | 每轮都有 trace 记录与 token/成本数字 |
| 演示 | `scripts/demo-week1.md` 逐条照读 | 三分钟能演完，不需要现场调试 |

## 2. 范围边界

**做**：仓库骨架、配置、Provider（含流式）、Agent Loop、工具注册、memo/plan 工具、CLI、trace/usage、FakeProvider 与首批用例、三文件模板占位。

**不做**（避免范围蔓延）：

- ❌ 记忆检索、门控、巩固 —— PART 2；
- ❌ RAG、影视工具、推荐 —— PART 3；
- ❌ judge、CI 门禁、release_gate —— PART 4；
- ❌ QQ、定时任务、sinks 投递 —— P2；
- ❌ `templates/` 三文件的**内容上限与校验逻辑** —— 本部分只放占位模板让 `doctor` 能复制，真正的读写/上限/同步归 PART 2。

## 3. 文件清单

新建（与 TECH §1.4 的目录树一致）：

| 文件 | 职责 |
|---|---|
| `pyproject.toml` | uv 管理；依赖：httpx、apscheduler、websockets、sqlite-vec、jieba、fastembed、pytest、ruff |
| `.env.example` | §3.1 全量配置项 + 注释，不含真实密钥 |
| `.gitignore` | **必须含 `data/` 与 `.env`** |
| `README.md` | 首版：一句话定位 + 安装 + 跑起来 + 数据边界表（§14.4） |
| `yixiang/__main__.py` | 命令分发（chat / serve / doctor / rag / ops / eval） |
| `yixiang/config.py` | `Settings` dataclass + `.env` 解析 + 校验 |
| `yixiang/providers.py` | `ChatModel` 协议、OpenAI-compatible 实现、`complete` / `complete_stream`、角色路由、usage 记账 |
| `yixiang/app.py` | 组装根：Settings → Provider → Memory → Tools → Loop → Gateway |
| `yixiang/runtime/models.py` | `ProviderRequest` / `ModelReply` / `LoopEvent` / `TurnResult` 等 dataclass |
| `yixiang/runtime/session.py` | `SessionManager`：工作记忆装配、历史窗口、会话生命周期 |
| `yixiang/loop/agent.py` | `run_loop`：reason → act → observe（含流式事件） |
| `yixiang/loop/guard.py` | 迭代上限、重复调用检测、工具失败计数 |
| `yixiang/tools/registry.py` | `Tool` / `ToolRegistry` / `build_registry()` |
| `yixiang/tools/memo.py` | `add_memo` / `list_memos` / `finish_memo` + 相对时间解析 |
| `yixiang/tools/plan.py` | `create_plan` / `add_task` / `list_today` / `complete_task` |
| `yixiang/gateway/cli.py` | REPL、斜杠命令、流式渲染 |
| `yixiang/ops/tracing.py` | trace JSONL 写入 + `turn_id` |
| `yixiang/ops/usage.py` | usage.jsonl 追加 + 汇总（含 `pricing.py` 价目表） |
| `yixiang/ops/show_trace.py` | 终端渲染单轮链路 |
| `templates/{soul.md,user.md,memory.md}` | 三文件初版模板（占位，PART 2 补内容与上限） |
| `evals/deterministic/{conftest.py,fake_provider.py,test_provider.py,test_tools_memo.py,test_tools_plan.py,test_loop_guard.py,test_loop_context.py}` | L1/L2 用例与测试基建 |

## 4. 接口契约

**Consumes**：无（这是第一块）。

**Produces**（后面三个部分都依赖这些，改动即破坏性变更）：

```python
# config.py —— 后面所有模块只读这个对象，不直接读 os.environ
@dataclass
class Settings:
    main_model: str; gate_model: str; judge_model: str; utility_model: str
    api_base: str; api_key: str
    embed_backend: str; embed_model: str
    data_dir: Path
    history_turns: int; loop_max_iter: int; tool_retry_max: int
    llm_timeout: float; gate_timeout: float
    ...

# providers.py
class ChatModel(Protocol):
    async def complete(self, req: ProviderRequest) -> ModelReply: ...
    async def complete_stream(self, req: ProviderRequest, observer: Observer) -> ModelReply: ...

# tools/registry.py
@dataclass
class Tool:
    name: str; description: str; input_schema: dict
    fn: Callable[..., str]          # 永远返回 str，错误也返回字符串
    side_effect: bool = True; timeout_s: float = 10.0

def build_registry(settings: Settings, deps: Deps) -> ToolRegistry: ...

# loop/agent.py
async def run_loop(session: SessionManager, registry: ToolRegistry,
                   provider: ChatModel, observer: Observer) -> TurnResult: ...

# runtime/session.py
class SessionManager:
    def assemble(self) -> list[Message]: ...     # 每轮现拼，不缓存
    def add_exchange(self, user: str, reply: str, tools: list[ToolCall]): ...
```

**冻结约定**（改这些要同步改三个下游）：

| 约定 | 值 |
|---|---|
| 工具返回值类型 | 永远是 `str`；错误也返回字符串，不抛异常 |
| 工具结果长度上限 | 2000 字符，超出截断并注明 `(已截断，共 N 字)` |
| 角色名 | `main` / `gate` / `utility` / `judge` / `embed` |
| env 前缀 | `YIXIANG_`；字段名与 env 名一一对应（`YIXIANG_MAIN_MODEL` → `settings.main_model`） |
| 错误码 | 见 TECH §11.3 表，`E_` 前缀 |
| 时间格式 | ISO8601 带时区；存本地时间字符串，比较用 UTC |

## 5. 关键设计点（硬约束）

这几条是"照着写就不会错、写错就要返工"的，深度理由见 TECH 对应章节：

1. **不做框架**：核心链路不引入 LangChain/LangGraph，loop 本体应 <100 行（§ADR-1）。这是面试第一个会被问的点。
2. **流式只在"无工具轮"发生**：`tool_calls` 分片组装易错且对用户没信息量，用户侧只显示"正在查询…"（§5.4）。
3. **流式增量只进内存缓冲，整轮结束才写 `chat_log`**——否则半截回复会进入历史，后续轮次读到残缺上下文（§5.4）。
4. **工具活动折叠**：`add_exchange` 把工具调用记录成 `[tools used: …]` 写进 assistant 历史，防"重复订两次日历"类 bug（§5.3）。
5. **时间戳不能放在 prompt 开头**：否则前缀缓存永远不命中，日成本接近翻倍（§4.5、§15.2）。动态部分（当前时间）放 system 末尾。
6. **门控/巩固/judge 一律用非流式** `complete()`，只有主对话用 `complete_stream()`（§5.4）。
7. **FakeProvider 是第一周的交付物，不是"以后补"**（§13.2）。没有它，Agent 行为不可复现地测试，后面三部分的评测全部建不起来。
8. **安全默认保守**：`YIXIANG_QQ_ALLOWED` 为空 = 拒绝所有外部消息（§3.1）。

## 6. 任务分解

| 日 | 任务 | 产出 | 验收 |
|---|---|---|---|
| **D1** | 仓库骨架：`pyproject.toml`（uv）、ruff、pytest、`.env.example`、`data/` + gitignore、`templates/` 三文件占位、首次提交 | 目录结构与 §1.4 一致 | `uv run yixiang --help` 有输出 |
| **D2** | `config.py` + `providers.py`（角色路由、重试矩阵 §4.3、usage 记账） | §3 / §4 落地 | `yixiang doctor` 自检项 1~6 通过 |
| **D3** | `loop/agent.py` + `FakeProvider` + 前 3 条用例（D-01 / D-19 / D-20） | §5 落地 | `pytest -m "not live"` 绿 |
| **D4** | `tools/registry.py` + `add_memo` / `list_memos` / `finish_memo` + 相对时间解析 | §9 落地 | D-01、D-02 绿 |
| **D5** | plan 三件套 + CLI 斜杠命令（`/help` `/trace` `/cost` `/new` `/exit`） | §10.1 落地 | 手工排出一周计划 |
| **D6** | `ops/tracing.py` + `ops/usage.py` + `yixiang ops tail` / `ops cost` | §11 落地 | 每轮都有 trace 与成本记录 |
| **D7** | 用例补到 ≥8 条、README 首版、`scripts/demo-week1.md`、录屏 | — | **CLI 演示剧本跑通** |

## 7. 测试与用例

本部分要覆盖的用例（编号与 TECH §13.3 一致）：

| 编号 | 内容 |
|---|---|
| D-01 | 备忘触发："记一下周五中午前交材料" → 必调 `add_memo`，`due_at` 解析正确 |
| D-02 | 计划查询：`list_today` 只返回今日 items，不编造 |
| D-19 | 工具失败注入：`add_memo` 抛异常 → 用户可见 `E_TOOL_FAILED`，loop 正常结束 |
| D-20 | 循环防护：连续 3 次调同一工具同参数 → guard 打断 |
| D-21 | 上下文裁剪：注入 50 轮长历史 → input ≤ 预算，最近 8 轮完整保留 |
| D-22 | 路径逃逸：参数含 `..\..\` 或绝对路径 → 拒绝执行 |
| D-26 | 迁移：空库 → `migrate()` 后 `user_version` 为最新、表齐全 |
| — | `test_provider.py`：重试、超时、usage 记账、角色路由、"假流"分片组装 |

L1/L2 的纪律：**用例只断言三件事，顺序不要混**——① 请求侧（模型看到了什么）② 行为侧（该调的调了、不该调的没调）③ 结果侧（DB 与文件的最终状态）。

## 8. 风险与砍单

| 风险 | 对策 |
|---|---|
| 时间超支 | 先砍"demo 润色"（花哨终端 UI、动效）——不影响任何面试问答 |
| 流式实现踩坑（分片、半截失败） | 单独一组"假流"用例覆盖，别混进主用例 |
| 想一次性把 §5.1 状态机写全 | 只实现主链路 + guard 三条；状态机图是给面试讲思路用的 |
| 依赖装不上（torch 类） | 已定 fastembed，**默认不装 torch**；CI 缓存依赖 |

**不可砍**：loop、工具注册契约、trace/usage、FakeProvider。这四样砍了后面全塌。

## 9. 面试讲点

1. **为什么不用 LangGraph**：循环本体 <100 行，但要能讲清 iteration guard、重复调用检测、工具失败计数各自防的是什么事故。
2. **流式的复杂度在边界，不在"逐字打印"**：主动说出三条——工具调用轮不流式、半截失败如何收尾、增量与落盘的一致性。比说"我用了 `stream=True`"有说服力得多。
3. **前缀缓存**：为什么把时间戳从 prompt 开头挪到末尾。这是"我算过钱"的直接证据（§15.2 敏感性分析：缓存命中率 70% → 0%，日成本从 ¥0.41 涨到 ¥0.58）。
4. **工具 description 的三条规矩**：写清什么时候用/不用、写清返回什么、有顺序依赖时明说。附上 `save_memory` vs `add_memo` 的边界例子。

## 10. 交接检查表（DoD）

- [ ] `uv run yixiang chat` 能流式对话，`/trace` `/cost` 有数据
- [ ] `pytest evals/deterministic -m "not live"` ≥8 条全绿、离线、≤30 秒
- [ ] `FakeProvider` 可用，能注入超时/截断/坏 JSON
- [ ] `yixiang doctor` 六项全过；`templates/` → `data/` 的复制路径可用
- [ ] `Tool` / `Settings` / `run_loop` / `SessionManager` 的签名与本文 §4 一致（PART 2/3/4 按此开工）
- [ ] `git log` 能看出每天一次提交，提交信息含用例编号
