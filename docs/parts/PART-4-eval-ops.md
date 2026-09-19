# PART 4 — 评测 · 运维 · 交付（＋P2 延伸）（W4）

> 版本 v1.0 · 2026-09-19 · 阶段：W4（D22–D28）
> 依赖：PART 1（trace / usage / FakeProvider）、PART 2（门控与记忆用例、`gate.jsonl`）、PART 3（`media.jsonl` 与检索回归）
> 被谁依赖：无（这是终点）· 唯一后续是文末的 P2 延伸
> 上游设计：[TECH-DESIGN §11](../TECH-DESIGN.md)（Ops 与发布门禁）、§13（评测策略全章）、§14（安全）、§15（成本）、§12.4（迁移与备份）、§16.2 W4、§16.3、§17
> 一句话交付：**CI 门禁真的会拦住退步（确定性 100% / judge ≥4.0 / 门控漏检 0 / top-3 ≥60%），陌生人照 README 三条命令能跑起来，demo 素材与三张数字卡齐。**

---

## 1. 目标与验收

| 验收项 | 命令 | 通过标准 |
|---|---|---|
| CI 门禁 | push / PR → GitHub Actions | 四步全绿：`ruff` → `pytest -m "not live"` 100% → `skills validate` → `release_gate` |
| judge 评测 | `uv run yixiang eval judge` | 10 条均分 **≥4.0 / 5**；每条输出可解析的 `{score, reasons[]}` |
| 门控门禁 | `uv run yixiang eval gate` | **漏检率 = 0**（误检容忍 ≤30%） |
| 检索门禁 | `uv run yixiang rag eval` | `media.jsonl` **top-3 ≥60%** |
| 成本 | `uv run yixiang ops cost --day` | 日均 ≤¥0.5；`--explain` 能打印各段 token 占比 |
| 陌生人可跑 | 干净环境 clone → 照 README | **≤3 条命令**跑通 `yixiang chat`；数据边界表在 README 里 |
| 记忆巡检 | `uv run yixiang memory verify` | 无漂移（DB 与三文件逐条对得上） |
| 备份 | `uv run yixiang backup` / `backup gc` | 生成当日 `state-YYYYMMDD.db`；`data/` 私有仓有一次 commit；**演练过一次恢复** |
| 演示 | `scripts/demo-week4.md` + 录屏 | 三分钟演完；**三张数字卡**（成本 / 命中率 / 用例数）能直接截图 |

## 2. 范围边界

**做**：`release_gate`、GitHub Actions、judge 10 条与 rubric、常驻调度（巩固兜底 / 每日汇总 / 周巡检）、备份与保留策略、README 重写、架构图、录屏素材、三张数字卡、一周真实使用暴露问题的收口、P2 延伸（可选，文末）。

**不做**（避免范围蔓延）：

- ❌ **Web dashboard** —— 明确不做（§17.2-3）。demo 用终端录屏，若时间富余最多做一个**只读** trace 页；
- ❌ **live 用例进 PR 门禁** —— 默认不进（§17.2-2）：外部 API 抖动会阻塞开发，也会把每天的成本从零变成几十次真调用。改为 nightly + 发版前手动跑；
- ❌ **本部分不写新功能** —— W4 是收口周。任何新工具一律走 §9.3 三步并配用例，不做"顺手加个能力"；
- ❌ **P2 的任何东西都不进本部分门禁** —— 定时晨报推送、QQ 入口、B站/Pixiv 工具都不参与 `release_gate` 的判定。

## 3. 文件清单

| 文件 | 职责 | 关键点 |
|---|---|---|
| `.github/workflows/ci.yml` | 四步门禁 + 嵌入模型缓存 + 失败时落 trace artifact | 步骤顺序不能换：lint 先，判定最后 |
| `yixiang/ops/release_gate.py` | 五项检查的汇总判定，输出退出码 | 成本项**只告警不改退出码**（§11.2） |
| `yixiang/scheduler/jobs.py` | 巩固兜底（23:30）、每日汇总（23:50）、记忆巡检（周日 22:00） | `run_job` 异常隔离：**失败可见，但不能杀主进程**（§10.3.2） |
| `yixiang/ops/backup.py` | `VACUUM INTO` 日快照 + `data/` 私有仓提交 + `backup gc`（30 天） | 备份目录永不外传 |
| `yixiang/ops/usage.py`（改） | 日汇总口径 + `ops cost --explain` 分段占比 | 汇总是 23:50 job 的写入目标 |
| `yixiang/scheduler/brief_job.py`（**P2 才启用**） | cron + 补发 + sinks 投递 | 本部分只保证接口预留，不接进 `release_gate` |
| `evals/judge/cases.yaml` | 10 条 rubric（闲聊 / 推荐 / 计划 / 记忆管理 / 情绪陪伴 五类） | `must_have` 与 `must_not_have` 分开写 |
| `evals/judge/run_judge.py` | 打分、结构化解析、失败留痕 | 解析失败该条计 0 并记录 |
| `evals/deterministic/test_scheduler.py` | job 幂等、异常隔离、补发（补发部分 P2 skip） | 时间一律注入假时钟 |
| `evals/deterministic/test_security.py` | 路径逃逸、注入包裹、超长输入 | 对齐 T-2 / T-3 / T-8 |
| `README.md`（重写） | 一句话定位 + 3 条命令跑起来 + **§14.4 数据边界表** + 目录说明 | 这份 README 是"陌生人能跑"的唯一证明 |
| `docs/architecture.md`（或 mermaid 内嵌） | 架构图：入口 / loop / 记忆 / RAG / 评测四支柱 | 直接进简历与答辩 PPT |
| `scripts/demo-week4.md` | 演示剧本 + 录屏分镜 | 录屏前先照读一遍 |
| `docs/NUMBERS.md` | 三张数字卡的原始数据与算法 | 数字要能追溯到 `usage.jsonl` / golden 报告 |

## 4. 接口契约

**Consumes**：PART 1 的 `ops/tracing` / `ops/usage` / `FakeProvider`；PART 2 的 `gate.jsonl` 与记忆用例；PART 3 的 `media.jsonl` 与 `retrieve_media`；全部模块的 `Settings`。

**Produces**（CI 与运维依赖）：

```python
# ops/release_gate.py
@dataclass
class Check:
    name: str; value: float; threshold: float
    passed: bool; blocking: bool

@dataclass
class GateResult:
    checks: list[Check]; passed: bool

def run_gate(...) -> GateResult: ...
    # CLI 入口: python -m yixiang.ops.release_gate
    # 退出码: 0 = 全过; 1 = 有 blocking 检查失败

# scheduler/jobs.py
@dataclass
class JobSpec:
    name: str; cron: str; idempotency_key_tmpl: str; fn: Callable[[], Awaitable[None]]

async def run_job(name: str, fn: Callable[[], Awaitable[None]]) -> None: ...
JOBS: list[JobSpec]      # 巩固兜底 / 每日汇总 / 周巡检（brief push 是 P2 追加项）

# ops/backup.py
def backup_now(data_dir: Path) -> Path: ...            # VACUUM INTO + git commit
def gc_backups(data_dir: Path, keep_days: int = 30) -> int: ...
```

**冻结约定**（门禁语义，改动等于改考核标准）：

| 约定 | 值 |
|---|---|
| `release_gate` 五项 | 确定性 100% / judge ≥4.0 / 门控漏检 = 0 / top-3 ≥60% / 单轮成本 ≤预算×1.5 |
| 硬门禁 vs 告警 | **前四项 blocking**；成本项**只告警**，不阻止合并 |
| 退出码 | `0` = 全过；`1` = 有 blocking 失败。CI 直接依赖，不要在 CI 里重写判定逻辑 |
| live 标记 | `-m live` 不进 PR；nightly 与发版前跑 |
| CI 步骤顺序 | `ruff` → `pytest -m "not live"` → `skills validate` → `release_gate`，失败即停 |
| job 幂等键 | `usage:YYYY-MM-DD` / `verify:YYYY-Www`（brief 为 `brief:YYYY-MM-DD`，P2） |
| 备份保留 | 30 天；`data/` 私有仓**只版本化** `soul.md` / `user.md` / `memory.md` / `skills/` / `briefs/` |
| golden 集 | **只增不改**；确需改则旧版另存 `*.v1.jsonl`，提交信息里写理由 |

## 5. 关键设计点（硬约束）

1. **分层纪律：依赖越多跑得越少**（§13.1）。L1 单元（每次保存）/ L2 集成用假 Provider（PR + CI）/ L3 检索回归（PR + CI，需缓存嵌入模型）/ L4 judge（nightly + 发版前）。把这条说清楚，比"我写了 40 个测试"更能说明工程判断力。
2. **假 Provider 是整个体系的地基**（§13.2）。它让"Agent 的行为"第一次变成可测对象：全量 L1+L2 跑完 ≤30 秒、零成本、零抖动，还能稳定复现真模型难复现的失败路径（超时、`finish_reason=length`、工具参数是坏 JSON）。
3. **judge 同源局限要主动交底**（§13.4）。P0 用与 main 同一模型自评，存在偏好偏差，所以三条缓解必须同时存在：① 阈值**只当回归警报**，不当质量结论；② 把可客观判断的部分（工具是否调用、参数是否正确、是否编造事实）**下沉为确定性断言**，judge 只负责语气与合理性；③ 换 judge 模型时**重跑全部历史分数**校准，否则新旧分数不可比。
4. **门控集的不对称权重**（§13.5）：误判 true（多检索一次）只损失延迟与 token；误判 false（该检索不检索）是产品级事故。所以门禁是"漏检 = 0"，调优方向永远是**宁可多检索**。
5. **时间相关用例一律注入假时钟**（`now()` 从 Settings / Clock 注入），测试里不许出现 `sleep`。这是后面定时 job 能被测试的唯一前提。
6. **flaky 的处理顺序不能跳**（§13.6）：① 先确认是不是假 Provider 覆盖不足；② 把非确定性部分改成假 Provider；③ 确实必须真模型的，移进 live 层。**禁止用 `reruns=3` 掩盖抖动**——那等于把门禁关掉。
7. **调度任务的失败必须可见，但不能影响主链路**（§10.3.2）：`run_job` 捕获一切异常 → trace + 日志 + 次日日报汇总。绝不能因为巩固失败让用户发不出消息。
8. **上线修一个 bug，必补一条回归用例，并在提交信息里写出用例编号**（§13.3）。这条纪律是"评测驱动"能落地的唯一保证。
9. **备份是资产保护，不是可选项**：记忆是本项目最有价值的产物。`data/state.db` 用 `VACUUM INTO` 日快照（WAL 下安全）；三文件进 `data/` 私有仓。T-9 明确写了泄露风险——**仓库必须是私有的，且只版本化三文件 / skills / briefs**。
10. **README 里的数据边界表必须诚实**（§14.4）：写"数据完全本地"而实际上每轮都把记忆片段发给云端模型，是答辩时最容易被戳穿的一句话。宁可主动写清"哪些出去、哪些不出去"，并顺势给出"缩小出网面"的路线图。

## 6. 任务分解

| 日 | 任务 | 产出 | 验收 |
|---|---|---|---|
| **D22** | 记忆巡检与巩固兜底落地（23:30 兜底 + 周日 `memory verify`）+ 修掉一周真实使用暴露的问题 | `scheduler/jobs.py` + `test_scheduler.py` | `memory verify` 无漂移；真实使用记录归档 |
| **D23** | 口味画像 + 推荐去重收口（连续两日无交集） | §8.5 收口，接 PART 3 遗留 | **D-12 绿** |
| **D24** | judge 评测（10 条）+ `release_gate` + GitHub Actions | §11.2 / §13.4 落地 | **CI 全绿；judge 均分 ≥4.0** |
| **D25** | README（含 §14.4 数据边界表）、架构图、demo 脚本 | 可对外交付的文档面 | **陌生人照 README 能跑起来** |
| **D26** | 备份命令（`backup` / `backup gc`）+ 恢复演练 + 安全用例补全 | §12.4 落地 | 快照生成、私有仓有 commit；**恢复演练成功** |
| **D27** | 真实使用补用例（一周里出现过的 bug 各补 1 条）+ 三张数字卡 | 数字卡数据可追溯 | 用例数、成本、命中率三个数都能指到源头 |
| **D28** | 录屏 + 简历条目 + 演示彩排 | 演示视频 | **三分钟录屏一遍过** |

> **D26~D28 的原计划含 B 站工具（P2，可选）**：它是砍单顺序第 3 条，且与影视推荐主线叙事重复度低。**默认不做**，只有在 D22~D27 全部收口且笔试面试有空档时才动手。

## 7. 测试与用例

| 编号 | 内容 | 断言要点 |
|---|---|---|
| **D-12** | 推荐去重 | 连续两次按需推荐 → 集合交集为空；`recommend_log` 新增 2 条 |
| **D-22** | 路径逃逸 | 工具参数含 `..\..\` 或绝对路径 → 拒绝执行；错误码写入 trace |
| **D-26** | 迁移 | 空库 → `migrate()` → `user_version` = 最新；所有表与外键齐备 |
| **D-13** | QQ 幂等（**P2 生效**，本阶段 `skip`） | 同 `message_id` 投递两次 → 只产生 1 条回复、1 条 `chat_log` |
| **D-27** | 定时补发（**P2 生效**，本阶段 `skip`） | 8:00 未运行、9:30 启动 → 补发一次并标注"（补发）"；二次启动不重发 |
| **J-01~J-10** | judge 10 条 | 均分 ≥4.0；`{score, reasons[]}` 可解析；解析失败该条计 0 并留痕 |
| — | `test_scheduler.py` | job 幂等键生效（同日重复触发只执行一次）；job 抛异常不影响主进程；假时钟注入 |
| — | `test_security.py` | 路径逃逸被拒；`<external_content>` 包裹生效；超长输入被截断且记 trace |

两条纪律：

1. **每条 golden 分数变化都要能在 PR 描述里解释**——改了哪几条、分数怎么变（§13.5）。
2. **live 用例单独标记**，`pytest evals/deterministic -m "not live"` 必须**离线、≤30 秒、零成本**。

## 8. 风险与砍单

| 风险 | 对策 |
|---|---|
| judge 分数抖动，把好改动判成退步 | 阈值只当回归警报；可客观判断项下沉为确定性断言；换模型时重跑历史分数校准 |
| CI 时间过长（嵌入模型下载） | 缓存 `~/.cache/fastembed`；`-m "not live"`；L3 只跑检索子集 |
| 最后一周还在写功能 | **冻结功能，只修 bug 与写文档**。按 §16.3 的砍单顺序执行，不再接新需求 |
| README 拖到最后写，质量塌 | 支线 D 每周更新 README；W4 只做收口与校对，不做从零撰写 |
| 录屏现场翻车（对话没反应 / trace 空） | 先跑 3 遍 `demo-week4.md`；用 `ops tail` 保证画面有信息量；关键数字提前截图兜底 |
| 时间超支 | 先砍 judge 到 **5 条**（§16.4）；再砍则只保留确定性的四步门禁，judge 降到本地手动跑 |

**不可砍**：FakeProvider + 确定性用例、`release_gate`、trace / usage、README 数据边界表、三张数字卡。前两样是"评测驱动"的证据，后三样是"这个项目真的在跑"的证据。

## 9. 面试讲点

1. **四层评测的分工**：`依赖越多的层跑得越少`。L1 每次保存、L2+L3 进 PR、L4 一天最多一次。能顺势讲清"为什么 live 用例不进 PR 门禁"——不是偷懒，是不让外部 API 抖动阻塞开发，也不让成本随提交次数线性增长。
2. **judge 同源局限的主动交底**（比被问出来更值钱）：我知道同族模型自评有偏好偏差，所以阈值只当回归警报、把可客观判断的部分下沉成确定性断言、换模型时重跑历史分数。这三条一说完，这个问题就从"漏洞"变成"我知道边界在哪"。
3. **门禁阈值的不对称设计**：为什么漏检是 0 而误检容忍 30%——漏检 = 失忆（用户直接感知），误检只是多注入几条记忆。
4. **成本模型**：能现场拆 `¥0.20（价目 A）/ ¥0.41（价目 B）` 是怎么算出来的；能说出敏感性分析里最关键的两条——前缀缓存命中率 70% → 0% 涨到 ¥0.58（**缓存不是优化项，是预算成立的前提**），三文件写满涨到 ¥0.80。门控 + 巩固只占 8%，**真正的大头永远是每轮都要发出去的那 6k input**。
5. **安全的分层防御**：能对着一张表指出"这条威胁对应哪个文件、哪条用例"。一句话原则最好用——**模型可以建议，但只有代码做决定**：LLM 输出永远不能直接变成 shell 命令、文件路径或 SQL 片段。
6. **收尾一句**：这个项目不是"调通了 API 的聊天机器人"，而是**有记忆、有评测、有成本账、连续在用的个人 Agent**。

## 10. 交接检查表（DoD）

- [ ] GitHub Actions 四步全绿；`release_gate` 退出码语义正确（成本超预算只告警）
- [ ] judge 10 条均分 ≥4.0；解析失败有留痕
- [ ] 门控漏检 = 0；`media.jsonl` top-3 ≥60%；确定性用例 100%
- [ ] 日均成本 ≤¥0.5，`ops cost --explain` 能看到分段占比
- [ ] 干净环境照 README **三条命令**跑通 `yixiang chat`；数据边界表在 README 里
- [ ] `memory verify` 无漂移；调度 job 幂等且异常隔离（有对应用例）
- [ ] `yixiang backup` 生成快照、`data/` 私有仓有 commit、**恢复演练成功一次**
- [ ] 三张数字卡 + 录屏 + `scripts/demo-week4.md` 齐
- [ ] 一周真实使用暴露的 bug 各补了回归用例，提交信息含用例编号
- [ ] 实现与 `TECH-DESIGN.md` 的差异已回填（对应 §17.3 N-3 的每周回填约定）

---

## 附录 A — P2 延伸（可选，不参与任何门禁）

W4 收口且笔试面试有空档时才做。两块设计都已就位，**动手前不需要再写设计文档**。

| 延伸 | 内容 | 依据 | 工作量 |
|---|---|---|---|
| A-1 定时晨报推送 | APScheduler cron（`YIXIANG_BRIEF_CRON`，默认 8:00）+ 唤醒补发（12:00 前）+ `gateway/sinks.py` 三个投递通道（cli / file / toast） | §10.3、§10.3.1 | 约 1 天 |
| A-2 QQ 入口 | NapCat + OneBot v11 反向 WebSocket、白名单、CQ 码解析、幂等表、断线重连 | §10.2、§10.4 | 约 2 天 |

两条前提：

1. **A-1 的复用性已经验证过**——`daily_brief` 是内容层，A-1 只补触发层与投递层，不会重写组装逻辑（§10.3）。这也是"晨报改按需不浪费代码"这句话的兑现。
2. **A-2 的安全性默认最保守**：`YIXIANG_QQ_ALLOWED` 为空 = 拒绝所有外部消息；群消息直接丢弃、不进模型；工具按来源白名单（`create_skill` / 下载类工具仅 CLI 可用）。接 QQ 前先确认小号与低频使用策略（T-7）。

做完之后能补一句新的产品叙事：**同一个大脑，白天在手机上问，晚上在终端里问，记忆是同一份**——这是"入口可插拔"的可验证证据。

> 注意：P2 完成后**不回头改 `release_gate`**。D-13（QQ 幂等）与 D-27（补发）从 `skip` 转为实跑即可，门禁五项的阈值不动。
