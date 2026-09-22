# 交接文档 · yixiang（以湘）

> 生成日期：2026-09-20 ｜ 对应 commit：`4b2883d`（本文件自身未提交时，以 `git log` 为准）
> 读者：接手继续做的人——或者三个月后忘了细节的你自己。
> 本文只讲「还没做的」与「需要你拍板的」；已经做完的事见 [`README.md`](../README.md) 与 [`architecture.md`](./architecture.md)。

## 0. 一分钟现状

**能跑的**：`yixiang chat`（CLI 流式）/ `yixiang web`（本机六栏控制台）/ `yixiang rag eval`（检索回归）/
`yixiang doctor`（六项自检）/ `yixiang backup` + 恢复演练。四个 PART 的验收项都在。

**门禁**（当前实测）：

```text
ruff check .                                  → All checks passed
pytest evals/deterministic -m "not live" -rs  → 185 passed, 2 skipped, 1 deselected in 6.51s
yixiang skills validate                       → 通过
python -m yixiang.ops.release_gate            → 硬门禁 4/4 通过，结论"全过，可以合并"
```

2 条 `skip` 是**设计如此**：D-13（QQ 幂等）、D-27（定时补发）属于 P2，见 §2.1。

**刚修的一件事**（`4b2883d`）：CLI 与 Web 连续对话时第二句报 `Event loop is closed`。根因是入口每轮
`asyncio.run()` 新建并关掉 loop，而 provider 缓存的 `httpx.AsyncClient` 连接池绑在创建它的那条 loop 上。
现在三个入口共用 `runtime/eventloop.py` 的一条常驻 loop。**纪律：不要再退回 `asyncio.run`。**
（顺带：你自己起的 `yixiang web` 进程要重启才会带上这个修复。）

---

## 1. 你问的三件事

### 1.1 RAG 资料库还要不要你提供？

**结论：不需要你手写语料，但需要你拍一次板——"要不要联网跑一次真实入库"。**

先把事实说清楚，这里有个容易误会的地方：RAG 的"资料"不是你写的文档，是 **Bangumi / TMDb 的公开元数据**
（片名、年份、类型、评分、简介）。抓取管线已经写完了，`yixiang/rag/ingest.py` 里有单线程 + `sleep(1.0)`
限速 + 退避重试 3 次 + `data/raw/` 缓存 + 断点续跑。**你不需要提供语料，你需要提供的是网络和一次决定。**

当前库里的真实状态（查过 `data/state.db`）：

| 项 | 实际值 |
|---|---|
| `media` 表条数 | **31 部** |
| 来源 | 全部来自 `evals/fixtures/media_sample.json`（`local:001`~`local:030` + 1 条 `local:900` 恶意样本） |
| 设计目标 | 300~500 部（TECH §8.1） |
| 真实抓取是否跑过 | **一次都没有**（TODO T1） |

两条路，二选一：

```bash
# 路 A：保持现状（31 部离线语料）——够讲清检索链路，零成本零风险
uv run yixiang rag ingest --source local --file evals/fixtures/media_sample.json

# 路 B：联网拉真实语料（约 5 分钟，Bangumi 免 key）
uv run yixiang rag ingest --source bangumi --tags 悬疑,科幻 --pages 5   # 第一遍：新增
uv run yixiang rag ingest --source bangumi --tags 悬疑,科幻 --pages 5   # 第二遍：应该全是"跳过"
uv run yixiang rag eval                                                 # 真实嵌入下的指标
```

**如果你选修 T1，有三件事必须先知道**（这是真的会踩的）：

1. **golden 集 20 条的期望集合是按 31 部语料标注的**。例如「看过《排球少年》还想看运动番」的 `note`
   写着"离线语料里的运动番只有这三部"。语料换成 500 部后这些假设失效，命中率会变，**不是检索退步**。
   golden 集纪律是"只增不改"，所以要么先做 T2（复核两条已知 MISS）再跑，要么跑完把结论写进 `note`；
2. 真实嵌入要先下模型（`BAAI/bge-small-zh-v1.5`，fastembed ≈100MB）。**换了嵌入模型/维度，旧向量必须全量重算**，
   启动时会检查 `meta.embed_model` / `embed_dim` 并拒绝启动，提示 `yixiang rag reindex`——这是保护，不是 bug；
3. README 上写的 **90% 命中率 / MRR 0.792 是 hash 假嵌入 + 31 部语料的数字**，它证明的是"检索链路没坏"，
   不是"语义检索质量好"。真实数字要等 T1。

**我的建议**：先把 31 部的状态当作已交付（够用），把 T1 排进"有时间就做"。但要清楚代价——面试官问
"你这个抓取真跑过吗"，现在只能答"离线等价入口验过"。这条是**唯一一处形态完整但没上过真网**的代码。

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
   `read_file`…共 16 个），模型不会调工具 = 整个 Agent 只剩聊天。⚠️ **`deepseek-reasoner` 在
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
| T6 | **定时晨报推送** | `scheduler/brief_job.py` 只有接口预留，`deliver_brief()` 是 `# pragma: no cover`；`brief_job` 不在 `scheduler.JOBS` 里 | 要写 `gateway/sinks.py` 三个投递通道（cli / file / toast）+ cron job + 补发算法；D-27 用例现在 `skip` | ~1 天 |
| — | **QQ 网关** | `yixiang/gateway/qq.py` **不存在**；`yixiang serve` 只打印"还没实现"退非零；`processed_messages` 表已建好但没人写 | NapCat + OneBot v11 反向 WS、白名单、CQ 码、幂等、重连；D-13 用例现在 `skip` | ~2 天 |
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
| **真实抓取**（Bangumi / TMDb） | **已跑过**（Task 7）：真网络抓一页并幂等入库，`evals/live/` 里 2 条 `-m live` 用例守着；本机已抓 333 部 | 面试问"真跑过吗"现在答得出；TMDb 那一路仍只验过离线入口 |
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
| `scripts/demo-week4.md` | 全篇没有 Web 控制台一节 | 新增第 10 节**加演**（六栏动作表 + 上传路径 + 讲点），开头标明**不计入 3 分钟** |
| `docs/PRODUCT.md` §14.2-3、`docs/TECH-DESIGN.md` §17.2-3、N-5 | 同源的"不做 Web dashboard" / "保留 qq.py 骨架" | 都补了"已偏离 / 未落盘"的更正，三处口径与上面一致 |

### 2.5 设计里没写、但迟早会撞上的

- **前缀缓存命中率只有 9.0%**，而成本模型的假设是 ≥60%（TECH §4.5，`PRODUCT.md` 的 ¥0.5/天依赖它）。
  小样本下正常（`ops cost --explain` 会打印这个数），但**没有任何门禁在盯它**——想守住成本目标，这条得进巡检；
- **语料规模停在 31 部**，与设计目标 300~500 部差一个量级，见 §1.1；
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
                                                                       # ② 185 passed, 2 skipped
$env:YIXIANG_EMBED_BACKEND="hash"
.venv\Scripts\python.exe -m yixiang.ops.release_gate                   # ③ 硬门禁 4/4
.venv\Scripts\python.exe -m yixiang doctor                             # ④ 六项自检
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

1. **补 T1**（联网跑一次真实抓取 + 真实嵌入）——把项目里唯一"没上过真网"的形态补上，顺手回填 README 与 demo 剧本的数字；
2. **补 T6 + QQ**（约 3 天）——把"入口可插拔"从设计变成事实，D-13 / D-27 两条 `skip` 用例就能转绿；
3. **Web 三条 P1**（停止生成 / markdown / 拖拽上传）——投入最小、演示时最直观的加分项，
   尤其是"停止生成"，它是现在唯一一处"卡住只能等"的交互。
