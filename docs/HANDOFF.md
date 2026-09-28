# 交接文档 · yixiang（以湘）

> 生成日期：2026-09-20 ｜ 对应 commit：`4b2883d`（本文件自身未提交时，以 `git log` 为准）
> 读者：接手继续做的人——或者三个月后忘了细节的你自己。
> 本文只讲「还没做的」与「需要你拍板的」；已经做完的事见 [`README.md`](../README.md) 与 [`architecture.md`](./architecture.md)。

## 0. 一分钟现状

**能跑的**：`yixiang chat`（CLI 流式）/ `yixiang web`（本机七栏控制台，含链路 trace）/ `yixiang rag eval`（检索回归）/
`yixiang doctor`（八项自检）/ `yixiang serve`（QQ 网关 + 调度器，默认关）/ `yixiang backup` + 恢复演练。四个 PART 的验收项都在。

**门禁**（当前实测）：

```text
ruff check .                                  → All checks passed
pytest evals/deterministic -m "not live" -rs  → 399 passed, 1 deselected in 8.8s
yixiang skills validate                       → 通过
python -m yixiang.ops.release_gate            → 硬门禁 4/4 通过，结论"全过，可以合并"
```

那 1 条 `deselected` 是标了 `-m live` 的真模型用例（`evals/live/`），nightly / 发版前才跑。**已无 `skip`**：
D-13（QQ 幂等）与 D-27（定时补发）随 QQ 网关与调度器落地转成实跑，见 §2.1。

**刚补的一件事**（2026-09-28，"改期根本没有出口"）：用户在 QQ 里说"把这几条挪到本周"，agent 回
"挪好了，三项都落在本周"，但那一轮 `tool_calls` 是**空的**、库里 `memos.due_at` 还是老值、
`plan_items.date` 一动不动。根因不是模型偷懒，是**结构缺口**：注册表里压根没有"改期"这个工具
（`add_memo` 只能在创建时定 `due_at`，`complete_task` 只能标完成，两个都是单向门），模型唯一
能"改期"的方式就是在回复里吹一句。补法三层：① 新增 `reschedule_memo(id, due_at)` 与
`reschedule_task(item_id, date)`（注册表 19 → **21**），各自只 `UPDATE` 那一列，内容 / 状态 /
所属计划一律不动；② 时间解析走 `parse_day`——`parse_when` 的**严格版**，"本周 / 下周 / 随便写写"
这类**说不清是哪一天**的说法直接返回可行动错误（带上今天日期与举例），**绝不替用户挑一天**
（改错比没改更难发现）；③ 复用 §7.9 那套护栏：`detect_intent` 命中 `挪到 / 改到 / 推迟 / 顺延`
就打 `RESCHEDULE`，注入 S8 契约；本轮没成功调 `reschedule_*` 时再**纠错重试一次**（`_verify_reschedule`，
与"记住"轮的 `_verify_memory_write` 同一范式）。新增 13 条用例（`test_tools_plan.py` 5 → 10、
`test_tools_memo.py` 17 → 27）。

**刚修的一件事**（2026-09-28，"QQ 上的任务在 Web 端拿不到"）：根因不在网关，在 **Web 控制台开新会话时的
命名空间**。`ConsoleAPI.new_session()` 直接调 `SessionManager.new_session()`，后者拿**当前会话的 source**
拼新 id——而 `switch()` 会刻意沿用被打开的会话的来源。于是"先在 Web 里翻看一条 `qq:` 会话、再点『新会话』"
就造出 `qq:20260928-2251-xxx` 这种**假 QQ 会话**：QQ 的往来被拆成好几条，`chat_log.source` 也说假话，
历史面板越看越乱。两处一起改：① `new_session(name, *, source=None)` 支持显式指定来源，`ConsoleAPI.new_session()`
固定传 `source="web"`（CLI 的 `/new` 不传，仍落 `cli:`，行为不变；`switch()` 沿用原 source 的逻辑是对的，
保留不动）；② `_session_row()` 顺带把 `source` 带出来（`list_sessions` / `search_sessions` 共用同一套拼装），
历史行多一个来源徽章（QQ / 命令行；网页来源不标），一眼能看出哪条是哪来的。新增 2 条用例
（`test_web.py` 35 → 36、`test_sessions.py` 5 → 6）。

**刚做完的一件事**（2026-09-27，`memory.md` 只能看不能改）：用户说"我这边 memory.md 还是原来的"——
agent 手里确实**没有写这个文件的出口**。`data/memory.md` 每轮整份注入 S4，是人机共治的核心文件，但唯一
改动路径是"先写 facts、再靠 `sync.write_memory_doc()` 回写"，所以**手写笔记段**（无 id、不入库）与整段
重排 / 合并完全使不上劲——agent 只能在回复里列一张"改好了"的清单，文件一个字没动。现在给现有
`manage_memory` 加 `action="edit"`（**不新增工具**，仍是那 21 个；`test_web.py` 钉着 `tools == 21`），只有三个
精确操作 `add` / `replace` / `remove`，**刻意不做整份 rewrite**（`op="rewrite"` 直接报错）。全走现成机制：
`core_files.parse_memory_md` + `insert_lines` / `remove_lines` + `write_core_file`（原子写 + 150 行上限，
超限整份拒绝、文件不动）→ 紧接着 `sync.sync_memory_md(conn)`（文件为准）。带 id 的行删掉是**软删**，
`manage_memory(restore)` 能捞回来；定位用 `match`：形如 `[123]` 按 fact_id，否则在正文做子串匹配
（手写行也能命中），**唯一命中才动手**，多命中报出全部候选、零命中报错。"由用户触发"落在纪律上：
工具描述 + S8 契约写死"只有用户明确要求才用 edit，只在回复里列清单等于没改"，`detect_intent` 新增
`MEMORY_EDIT` 打标（须同时含文件词 + 动作词，且 REMEMBER 优先、只是提到 memory.md 不算）。新增 13 条
用例（`test_memory_write.py` 9 → 22）。

**刚做完的一件事**（Web 上传三处升级）：① 图片走**多模态**——`Message.images` 存相对路径，只在
provider 组 payload 时读成 `data:` URI 的 content part，模型真的看得到像素；文本里只留一行
"（附图：uploads/….png）"进历史（像素只发一次，前缀缓存与上下文预算都不答应重发）。② 单个上限
2MB → **30MB**（`MAX_UPLOAD_BYTES`，文案统一从 `MAX_UPLOAD_LABEL` 出）。③ 工作区托盘改成**服务端
列表**（`GET /api/uploads` + `DELETE /api/uploads/<name>` + `POST /api/uploads/clear`）——上一版的
列表只活在浏览器内存里，刷新即空、磁盘上的文件却还在，界面上连一行都没有，所以"传了删不掉、
工作区越堆越满"。

**刚修的一件事**（`4b2883d`）：CLI 与 Web 连续对话时第二句报 `Event loop is closed`。根因是入口每轮
`asyncio.run()` 新建并关掉 loop，而 provider 缓存的 `httpx.AsyncClient` 连接池绑在创建它的那条 loop 上。
现在三个入口共用 `runtime/eventloop.py` 的一条常驻 loop。**纪律：不要再退回 `asyncio.run`。**
（顺带：你自己起的 `yixiang web` 进程要重启才会带上这个修复。）

**刚修的另一件事**（2026-09-27，输出上限）：问一句"鉴赏一下这张封面"就回 `E_LLM_TRUNCATED`。根因是
`ProviderRequest.max_tokens` 默认 2048 而 loop 从没把它接上配置——**每一轮都被 2048 掐着**（截图里
`out 2,048 token` 正好是它）。两处一起改：① `YIXIANG_MAX_TOKENS`（默认 **8192**）接进 loop；
② 撞线时**不再把已经写好的半篇丢掉**——流式期间用户看着它滚出来，收尾却被一句错误文案顶掉，
这是"看见了又没了"。现在半篇照发，末尾接一句"回一句「继续」我接着说"，`error_detail` 里记下撞的是
哪条线。TECH-DESIGN §5.1 那句"不许把半句话发出去"就是这个决定的旧口径，已经改掉。

**接着修的一件事**（2026-09-27，"改完 8192 还是被截断"）：**2048 不是病根，思考 token 才是。**
直连 `POST /chat/completions` 探针（`max_tokens=10000`、prompt 让它一直输出到预算用尽）：
`completion_tokens=10000` / `completion_tokens_details.reasoning_tokens=10000` / `content_len=0`
——`max_tokens` 是**正文 + 思考的总预算**，deepseek-flash 把整个上限烧在 `reasoning` 上，正文一个字
都没吐。关掉思考后同一句 prompt 只花 **1395 token** 就自然收尾（答案本身约 1400~1600 token，
长度根本不是问题）。三个开关的对照：`reasoning_effort="none"` **生效**（`content_len=2918`，
`reasoning_len=0`）；`thinking={"type":"disabled"}` 只是不返回 reasoning_content，**token 照烧**
（假象）；`chat_template_kwargs` / `think=false` 则完全不透传。于是：

1. `.env` 的 `YIXIANG_NO_THINK_MODELS` 从 `qwen3.5-*` 扩成 `qwen3.5-*,deepseek-*`（云端那家也关）；
2. loop 的 `finish_reason=length` 分支多一道自愈：**自动关掉思考重问一次**（`ProviderRequest.
   thinking_disabled`，重问前发 `text_revoke` 撤回已流出去的那半篇）。第二轮正常收尾就静默用完整答案，
   用户连"被截断了"都看不到；只有**两轮都撞线**才落到"半篇 + 附告知"的收尾。只重问一次，且名单里
   已经关思考的模型不重问（参数一个字节都不会变，纯粹白花钱）。
3. `E_LLM_TRUNCATED` 文案改成"回答比较长，我分两段说；回一句「继续」我接着说下面的"。

**纪律**：以后看到"截断了"，先把 `usage.completion_tokens_details.reasoning_tokens` 捞出来看——
思考占大头就是"关思考"，不是"抬上限"。

**刚做完的一件事**（2026-09-27，QQ 发图看不了）：QQ 里发一张图，agent 回"这张图我这边看不到内容"。
根因不是模型不支持多模态，而是**图根本没到链路里**：`segments_to_text()` 把 `image` 段降级成字面量
`[图片]`，`handle_event()` 也从没给 `App.handle_message(images=…)` 传过东西（那条形参早就有了，
Web 上传正是靠它）。三处一起改：① `image` 段不再降级——`data.url` 单独返回，网关去下载；
② 下载按文件头认类型（`runtime/media.sniff_image_mime`）、落盘走 Web 同一条口径
（`runtime/uploads.store_upload`，仍 30MB 上限、同名让位），相对路径交主链路内联成 content part；
③ "先发图、再打字"是 QQ 上的常态，所以只有图的那条**不跑模型**，回一句短回执
（`IMAGE_ACK`）并把图替下一条文字留着（`PendingImages`，10 张封顶 / 5 分钟作废 / 取走即清）。
取不到（过期、断网、不是图、超限）一律降级：**不说自己看见了**，把"有 N 张图没取到"如实拼进
这一轮。新增 9 条用例（`test_qq.py` 13 → 22，多一条钉 CQ 串格式），全部门禁仍绿。

---

## 1. 你问的三件事

### 1.1 RAG 资料库还要不要你提供？

**结论：不需要你手写语料，但需要你拍一次板——"要不要联网跑一次真实入库"。**

先把事实说清楚，这里有个容易误会的地方：RAG 的"资料"不是你写的文档，是 **Bangumi / TMDb 的公开元数据**
（片名、年份、类型、评分、简介）。抓取管线已经写完了，`yixiang/rag/ingest/`（Task 24 拆包）里有单线程 + `sleep(1.0)`
限速 + 退避重试 3 次 + `data/raw/` 缓存 + 断点续跑。**你不需要提供语料，那个决定也已经拍过并跑完了。**

当前库里的真实状态（查过 `data/state.db`）：

| 项 | 实际值 |
|---|---|
| `media` 表条数 | **333 部**（31 条离线 fixture + 两段真抓去重后的并集） |
| 来源 | `evals/fixtures/media_sample.json` 的 31 条（`local:001`~`local:030` + 1 条 `local:900` 恶意样本）+ `rag ingest --source bangumi` 真抓两段 |
| 设计目标 | 300~500 部（TECH §8.1）——**已进区间** |
| 真实抓取是否跑过 | **跑过**：Task 7 先抓一页，Task 26 两段真跑（大盘 + 增量，去重后把语料从 31 部抬到 333 部）；`evals/live/` 里 2 条 `-m live` 用例守着 |

复现方式（两条路，任选）：

```bash
# 路 A：只跑离线 fixture（31 条）——够讲清检索链路，零成本零风险
uv run yixiang rag ingest --source local --file evals/fixtures/media_sample.json

# 路 B：联网拉真实语料（本机就是这么做的，约 5 分钟，Bangumi 免 key）
uv run yixiang rag ingest --source bangumi --tags 悬疑,科幻 --pages 5   # 第一遍：新增
uv run yixiang rag ingest --source bangumi --tags 悬疑,科幻 --pages 5   # 第二遍：应该全是"跳过"
uv run yixiang rag eval                                                 # 真实嵌入下的指标
```

**真抓之前有三件事必须先知道**（本机踩过了，留给你重跑时对照）：

1. **golden 集 20 条的期望集合是按那 31 部语料标注的**。例如「看过《排球少年》还想看运动番」的 `note`
   写着"离线语料里的运动番只有这三部"。语料换成 333 部后这些假设失效，命中率会变，**不是检索退步**——
   本机跑完就是 70.0% / MRR 0.667（hash 口径）。golden 集纪律是"只增不改"，所以要么先做 T2（复核两条已知 MISS）
   再跑，要么跑完把结论写进 `note`；
2. 真实嵌入要先下模型（`BAAI/bge-small-zh-v1.5`，fastembed ≈100MB）。**换了嵌入模型/维度，旧向量必须全量重算**，
   启动时会检查 `meta.embed_model` / `embed_dim` 并拒绝启动，提示 `yixiang rag reindex`——这是保护，不是 bug；
3. README 上写的 **90% 命中率 / MRR 0.792 是 hash 假嵌入 + 31 部语料的数字**，它证明的是"检索链路没坏"，
   不是"语义检索质量好"。真抓两段之后（333 部语料）同一份考卷是 **70.0% / MRR 0.667**（仍是 hash 假嵌入），
   换上真嵌入 `fastembed` 后同一份 333 部语料回到 **90.0% / MRR 0.792**——三个口径都在
   [`NUMBERS.md`](./NUMBERS.md) 卡 2，引用前先看清是"哪份语料 + 哪张嵌入"。

**T1 的状态**：联网跑一次真实抓取 + 真实嵌入（Task 7~9、Task 26）**已经完成**，所以面试官问
"你这个抓取真跑过吗"，答案是"跑过，333 部语料、两段真抓，数字在 §2.2 与 [`NUMBERS.md`](./NUMBERS.md) 卡 2"。
仍只验过离线入口的是 TMDb 那一路，以及 judge 的 `--live`（见 §2.2）。

### 1.2 模型的配置选择

四个角色分开配（`.env` 或 Web「模型配置」栏），只有一个默认值是实的，其余留空回落：

| 角色 | `.env` 键 | 默认 | 调用频率 | 建议 |
|---|---|---|---|---|
| main | `YIXIANG_MAIN_MODEL` | `deepseek-chat` | 每轮 1~8 次 | 唯一需要强能力的角色，**必须支持 function calling** |
| gate | `YIXIANG_GATE_MODEL` | 空 = 同 main | 每轮 1 次 | **优先换便宜档或本地小模型**（输出固定 JSON，容错高） |
| judge | `YIXIANG_JUDGE_MODEL` | 空 = 同 main | 每条评测 1~3 次 | **优先换另一家**（同源自评有偏好偏差，见下） |
| utility | `YIXIANG_UTILITY_MODEL` | 空 = 同 judge | 每 20 轮 1 次 | 换便宜档（巩固/摘要，容错最高） |

四个真实的选择点，按踩坑概率排序：

1. **main 必须支持工具调用（function calling）**。工具是主链路的骨架（`add_memo` / `search_media` /
   `read_file`…共 21 个），模型不会调工具 = 整个 Agent 只剩聊天。⚠️ **`deepseek-reasoner` 在
   `pricing.py` 的价目表里有，但它不支持工具调用**，换上去主链路会哑掉——**代码里没有任何拦截**，
   这是最容易踩的一个坑。换 main 之前先确认候选模型支持 OpenAI 风格的 `tools` + 流式 `tool_calls` 分片。
2. **价目表是手工维护的，换模型必须同时改 `yixiang/ops/pricing.py`**。现在是
   `deepseek-chat: (2.0, 0.5, 8.0)`（元/百万 token，查询日期 2026-09-19）。未知模型**静默回落到默认价**
   （宁可高估不掩盖成本）。这不是理论风险——`data/usage.jsonl` 里已经有 `model: "deepseek-flash"` 的记录
   （你在 Web 里试配置时留下的），而价目表**没有这一项**，那几笔成本实际是按 deepseek-chat 的价格算的。
3. **judge 现在是"同族模型自评"**（P0 临时方案，TECH §13.4 已交底）。它的分数只能当**回归警报**，
   不能当质量结论。阈值 ≥4.0 的全部意义是"某天突然掉到 3.2 说明有东西坏了"。要拿它说话，先换一家模型重跑历史分数。
4. **api_base 什么端点都行**（DeepSeek / GLM / vLLM / Ollama），但 payload 里带了
   `stream_options: {include_usage: true}`——这是 OpenAI/DeepSeek 的口径，**部分第三方兼容实现会 400**。
   要走本地推理（vLLM / Ollama）得先验这一处，没测过。

两套现成组合：

| 场景 | main | gate | judge | 成本量级 |
|---|---|---|---|---|
| 最省事（现状，`.env` 就是这么配的） | `deepseek-chat` | 留空 | 留空 | 实测一轮 ~¥0.008 |
| 压成本 + 去掉自评偏差 | `deepseek-chat` | 免费档 / 本地小模型 | **换另一家** | gate 的调用次数 = main，是全链路最值得本地化的一环 |

出网边界（README 有完整表，这里只提决策相关的一条）：**每轮拼好的 prompt（含注入的记忆片段与检索到的语料）都会发给所选供应商**。
缩小出网面的路线是"gate / utility / judge 换本地模型"——配置位已经就绪（`.env` 里那三个空键），
main 长期难以本地化。

### 1.3 Web UI 优化

先说清楚**它现在不糙**：暗色 OLED 主题、颜色全走 token、内联 SVG 图标、骨架屏、`aria-live`、
1024/768/420 三档响应式断点、`prefers-reduced-motion`、skip-link，而且**一律用 DOM API 建节点、
不用 `innerHTML` 拼数据**。所以下面的优化都是"从能用到好用"，不是"从零到一"。

按性价比排序（前三条是手感瓶颈）：

| 优先级 | 缺什么 | 现状 | 怎么补 |
|---|---|---|---|
| **P1** | **对话没有"停止生成"** | `sendMessage()` 把 submit 和 input 都 `disabled`，只能等这轮跑完；没有取消通道 | 前端加 `AbortController` + `POST /api/chat/{id}/cancel`，服务端在 loop 上取消任务（`eventloop` 已经能取消残留任务） |
| **P1** | **回复是纯文本** | 回复塞进 `<p>` 的 `textContent`，markdown（列表 / 代码块 / 加粗）原样显示 | 服务端或前端加一层 markdown → DOM 渲染。**注意纪律**：`app.js` 顶部写着"一律 DOM API 建节点、不用 `innerHTML` 拼数据"，别为了省事破这条 |
| **P1** | **上传只有"点选"** | 文件落 `data/uploads/`，但传完要手动点"填进输入框" | 加拖拽区 + 粘贴上传 + 传完自动把 `read_file` 调用填进输入框 |
| P2 | 历史面板只读 | 能翻、能切会话，不能删 / 重命名 / 导出 / 搜索 | 加会话删除（`DELETE /api/session`）+ 标题编辑 + 导出 JSON |
| P2 | 没有 trace 面板 | 看得到工具名与耗时（`toolChips`），点不进详情 | 接 `ops/show_trace.py` 的 `render_trace_detail`，做成第六+一个面板 |
| P2 | 只有深色主题 | `color-scheme: dark` 写死，`prefers-color-scheme: light` 没接 | 加一套 light token（CSS 变量已经全 token 化了，成本低） |
| P2 | 管理面缺口 | `rag ingest` / `reindex` / `memory verify` / `backup` 都只能在命令行做 | 各加一个"执行 + 流式回显"的按钮，测试时不用来回切终端 |
| P3 | 无快捷键 | 切面板、`/new`、切会话全靠点 | `Ctrl+Enter` 发送、`Ctrl+K` 命令面板 |
| P3 | 成本只看总额 | 顶栏显示今日成本，没有 `ops cost --explain` 那种分段占比 | 把 explain 的分段表搬进一个卡片 |

改之前先读两处注释：`app.js` 的第 1~11 行（前端纪律）与 `yixiang/web/server.py` 的模块 docstring
（HTTP 层只管按路径读静态文件，**没有构建步骤**，别引入打包工具）。

---

## 2. 未做清单

### 2.1 有意留到 P2 的（已接受，不阻塞任何门禁）

| 编号 | 事项 | 现状 | 缺口 | 预估 |
|---|---|---|---|---|
| T6 | **定时晨报推送** | **已落地**（Task 10 / Task 11）：`gateway/sinks.py` 三个投递通道（cli / file / toast）+ `scheduler/brief_job.py` 的 cron job 与补发算法，`brief_job` 已在 `scheduler.JOBS` 里 | 无（D-27 已转实跑）。默认关：`YIXIANG_SCHEDULER_ENABLED=1` 或 `yixiang serve --scheduler` 才开 | ✅ |
| — | **QQ 网关** | **已落地**（Task 11）：`yixiang/gateway/qq.py` 存在，`yixiang serve` 起反向 WS；`processed_messages` 表有人写了 | 无（D-13 已转实跑）。默认关：`YIXIANG_QQ_ENABLED=1` + 非空 `YIXIANG_QQ_ALLOWED`，或 `yixiang serve --qq` | ✅ |
| — | **B 站 / Pixiv 工具** | 只在 TECH §9.2 表里出现（`bilibili_search` / `pixiv_download`） | 未实现，可选 | — |

> ✅ QQ 监听与 Web 端口的撞车**已修掉**：`YIXIANG_QQ_LISTEN` 默认改为 `127.0.0.1:8766`
> （`config.py` / `.env.example` / 本机 `.env` 三处一致），**8765 留给 Web 控制台**（`web.server.DEFAULT_PORT`）。
> 这条撞车只在"网关与控制台同时开"时发作，报错却指向"端口被占用"，所以由 `test_doctor.py` 的
> `test_the_qq_listen_default_does_not_collide_with_the_web_default_port` 钉住默认值。

> ✅ **Bangumi 直连不通已给出配置位**：本机直连 `api.bgm.tv:443` 是**超时**（17.5s 后
> `timed out`，开着 VPN 也一样——VPN 是代理模式、不接管直连），而设全局 `HTTPS_PROXY=http://127.0.0.1:7897`
> 会把模型端点与 TMDb 一起改道，粒度太粗。所以新增 `YIXIANG_BANGUMI_PROXY`（`.env.example` 的
> Bangumi 段），**只喂**四条 Bangumi 链路：实时搜索 / 条目详情 / 收藏画像 / `rag ingest --source bangumi`。
> 实测走代理搜索 1.08s、详情 0.82s；不填 = 老行为一个字节不变。接线由
> `evals/deterministic/test_bangumi_proxy.py` 的 8 条用例守着（探针只记 Client 的 kwargs，
> **一条网络请求都不发**）。查不到"Bangumi 为什么全 500/超时"时先看这一项。

### 2.2 形态完整、但真机验证程度不一的真实路径

| 项 | 现状 | 影响 |
|---|---|---|
| **真实抓取**（Bangumi / TMDb） | **已跑过**（Task 7 抓一页；Task 26 两段真跑：大盘 `--sort heat --min-rating 8 --min-votes 500 --pages 25 --want 200` + 增量 `--min-rating 7.5 --since 2021-09-21 --pages 30 --want 200`，并集去重后把语料从 31 部抬到 **333 部**）；`evals/live/` 里 2 条 `-m live` 用例守着 | 面试问"真跑过吗"现在答得出，且能给出 333 部那一行的 top-3 **70.0% / MRR 0.667**（hash 口径，数字口径见 §1.1）；TMDb 那一路仍只验过离线入口 |
| **真实嵌入**（fastembed `bge-small-zh-v1.5`） | **已跑过**（Task 8）：模型落 `~/.cache/fastembed`，333 部语料真嵌入 **90.0% / MRR 0.792**（31 部那行是 `hash` 假嵌入）；CI 仍固定 `YIXIANG_EMBED_BACKEND=hash` | 真实语义指标已回填进 [`NUMBERS.md`](./NUMBERS.md) 卡 2；真模型只在 nightly / 发版前跑 |
| **judge `--live`** | 只跑过离线基线（均分 4.80）；`data/usage.jsonl` 里**没有一条 `role=judge` 的记录** | "真假模型评分"这条路没验过 |
| **nightly 工作流** | **不存在**——`.github/workflows/` 下只有 `ci.yml` | README 与 TECH §13.6 都写"live 走 nightly"，实际没有这个定时任务 |

### 2.3 待决策点（来自 [`TODO-AFTER-PART-4.md`](./TODO-AFTER-PART-4.md)）

| 编号 | 决策内容 | 一句话 |
|---|---|---|
| T1 | 联网验证真实抓取与真实嵌入 | 见 §1.1 |
| T2 | golden 集两条已知 MISS 定案 | 「宫崎骏的龙猫」是语料缺导演字段、「名字里带夏天的动画」是 hash 假嵌入失手——**要么修，要么把"为什么不修"写成结论** |
| T3 | 评测口径关口味 | **已收口**：README 评测段与 `NUMBERS.md` 都写了 `use_taste=False` |
| T4 | `templates/user.md` 偏好写法的兼容期 | 新写法「`- 喜欢：悬疑、科幻`」与旧自由写法都能解析；决定什么时候删掉旧分支 |
| T5 | D-24 降级标记的位置 | **已确认（Task 8）**：`E_EMBED_UNAVAILABLE` 只记 trace、不进 `result.error`（用户无感），这条够用；但同源的 `rag.reindex_required` 会**静默退成纯 FTS5 而 `rag eval` 横幅仍写"嵌入 可用"**——结论是 trace 够用、**横幅不够，巡检不能只看横幅** |
| T6 | 定时晨报推送 | 见 §2.1 |

### 2.4 文档与实现不一致（← **已全部修掉**，留档备查）

上一轮点名的四处，本轮已改完（连同三处同源口径一起对齐）：

| 位置 | 原来写 | 现在写 |
|---|---|---|
| `docs/parts/PART-4-eval-ops.md` §2 | "❌ **Web dashboard —— 明确不做**" | "⚠️ 计划里不做，收口后补了**范围外**的 `yixiang web`（不进任何门禁）"，并在 §3 文件清单下补了"范围外产出"注 |
| `docs/TECH-DESIGN.md` §10.2 | "`gateway/qq.py` **骨架留在目录里**" | "**没有落盘**——空文件证明不了入口可插拔"，改为指向 `YIXIANG_QQ_*` 配置位 / `doctor` 白名单自检 / `brief_job` 接口预留；§1.4 目录树的两行也标了"设计位，尚未落盘" |
| `docs/TECH-DESIGN.md` §10.3 | "`sinks.py` 定义 `Sink` 协议…P1 只落协议与 cli / file" | 改成"表是**设计**、`sinks.py` 尚未落盘"，并说明按需形态只需 `tools/brief.py` 直接写 `data/briefs/` |
| `scripts/demo-week4.md` | `162 passed … in 4.51s`（3 处）+ 门禁块 `162 passed / 0 failed` + 成本 `¥0.0000` | `185 passed, 2 skipped, 1 deselected in 6.51s`、`185 passed / 0 failed`、成本行换成实测 `¥0.0150 / 今天 2 轮 ¥0.0236` |
| `scripts/demo-week4.md` | 全篇没有 Web 控制台一节 | 新增第 10 节**加演**（面板动作表 + 上传路径 + 讲点），开头标明**不计入 3 分钟**；后来前端扩到七栏，表里补了"链路 trace"一行 |
| `docs/PRODUCT.md` §14.2-3、`docs/TECH-DESIGN.md` §17.2-3、N-5 | 同源的"不做 Web dashboard" / "保留 qq.py 骨架" | 都补了"已偏离 / 未落盘"的更正，三处口径与上面一致 |

> 后续进展（2026-09-23）：上表点名的"未落盘 / 骨架"**都已落盘**——`gateway/qq.py`（OneBot v11
> 反向 WS + 白名单 + 幂等 + 重连）、`gateway/sinks.py`（cli / file / toast 三通道）与
> `scheduler/`（jobs.py / brief_job.py / runtime.py，晨报 job 已在 `JOBS` 里）。表格里的"原来写 /
> 现在写"是当时的留档，**不代表当前状态**；当前状态看 §0 / §2.1 / §2.2。

### 2.5 设计里没写、但迟早会撞上的

- **前缀缓存命中率只有 9.0%**，而成本模型的假设是 ≥60%（TECH §4.5，`PRODUCT.md` 的 ¥0.5/天依赖它）。
  小样本下正常（`ops cost --explain` 会打印这个数），但**没有任何门禁在盯它**——想守住成本目标，这条得进巡检；
- **语料规模已到 333 部**（真抓两段，Task 26），落在设计目标 300~500 部区间；剩下的差距只是"还没到上限"，见 §1.1 / §2.2；
- **judge 的"离线基线自检"没有区分度压力**：10 条里 2 条故意不满分（J-05 / J-09），机制是好的，
  但真正区分度只有 `--live` 才能量，见 §2.2；
- `data/` 是独立私有仓且 `gitignore`，**备份链路只在本地**，换机器要手动搬 `data/`。

---

## 3. 交接核对清单

接手第一天照这个顺序走一遍，能确认"这台机器上一切正常"：

```powershell
$env:PYTHONIOENCODING="utf-8"          # Windows 上中文输出不乱码（必须）

.venv\Scripts\ruff.exe check .                                        # ① All checks passed
.venv\Scripts\python.exe -m pytest evals/deterministic -o addopts= -q -m "not live" -rs
                                                                       # ② 399 passed, 1 deselected
$env:YIXIANG_EMBED_BACKEND="hash"
.venv\Scripts\python.exe -m yixiang.ops.release_gate                   # ③ 硬门禁 4/4
.venv\Scripts\python.exe -m yixiang doctor                             # ④ 八项自检
.venv\Scripts\python.exe -m yixiang rag eval                           # ⑤ top-3 ≥60%（离线口径）
.venv\Scripts\python.exe -m yixiang memory verify                      # ⑥ 记忆三方对账，无漂移
.venv\Scripts\python.exe -m yixiang web                                # ⑦ 打开 http://127.0.0.1:8765/
```

第 ⑦ 步之后**连着发两句话**——这是 `4b2883d` 修的那个 bug 的现场复现方式，第一句正常、第二句
整轮失败就说明跑的是旧代码（重启进程）。

## 4. 环境与纪律（踩过的坑，别再踩）

| 项 | 说明 |
|---|---|
| 解释器 | **只有 `.venv\Scripts\python.exe`**；`uv` 不在 PATH 上（README 里的 `uv run` 是给干净环境写的） |
| 中文输出 | PowerShell 默认编码会把中文打成乱码，先 `$env:PYTHONIOENCODING="utf-8"` |
| 测试纪律 | 禁真 `sleep`、禁真模型调用；golden 集**只增不改**；确定性用例总耗时目标 ≤30 秒 |
| 密钥 | `.env` 里有真实 API key（trace / 日志 / 接口都只出掩码）；`.env` 与 `data/` 都在 `.gitignore` 里，**永不提交、永不外传** |
| 事件循环 | 入口不要再退回 `asyncio.run`（`4b2883d`）；一条线程一条常驻 loop，谁建谁关 |
| 门禁改动 | 阈值只写在 `yixiang/ops/release_gate.py` 一处，**别把数字抄进 YAML** |
| 新工具 | 走 TECH §9.3 的三步（定义 + 注册 + 用例），不做"顺手加个能力" |
| Web 前端 | 不用 `innerHTML` 拼数据、颜色只走 CSS token、z-index 只用四个阶梯、**不引入构建步骤** |

## 5. 想继续往下做，三条最划算的路

1. ~~**补 T1**（联网跑一次真实抓取 + 真实嵌入）~~——**已做完**（Task 7~9、Task 26）：333 部语料、真嵌入 90.0% / MRR 0.792；
2. ~~**补 T6 + QQ**（约 3 天）~~——**已做完**（Task 10 / Task 11）：D-13 / D-27 两条 `skip` 用例已转绿；
3. ~~**Web 三条 P1**（停止生成 / markdown / 拖拽上传）~~——**已做完**（`58db07c` / `ad4d359` / `f6cd1e6`）；
4. 还没做的：**T2**（golden 两条已知 MISS 定案）、**T4**（`templates/user.md` 偏好写法兼容期）、
   以及 §2.5 那三条"迟早会撞上"的（前缀缓存命中率进巡检、judge `--live` 跑一次、`data/` 私有仓换机器）。
