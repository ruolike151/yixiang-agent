# W4 演示剧本（3 分钟，照读即可）

> 目标：证明 PART 4 的五件事——**CI 门禁真的会拦住退步 / judge 10 条有区分度 /
> 常驻任务幂等且异常隔离 / 快照能当唯一库源恢复 / 陌生人照 README 三条命令能跑起来**。
>
> 下面所有输出都是**实际跑出来的**（Windows / `.venv`）。第 2~3、5~6 节是离线入口：
> 统一 `YIXIANG_EMBED_BACKEND=hash`，不联网、不下载模型、不花钱。
>
> 演示前先设好环境变量（PowerShell）：`$env:YIXIANG_EMBED_BACKEND='hash'`、
> `$env:PYTHONUTF8='1'`。**第 4 节的检索数字要的是另一档**：先
> `Remove-Item Env:YIXIANG_EMBED_BACKEND` 走真嵌入——模型已经落在 `~/.cache/fastembed`，
> 不会再下那 100MB。两档的数字都写在 `docs/NUMBERS.md` 卡 2 里，现场**别混着引用**。

## 0. 演示前的准备（不算在 3 分钟里）

```powershell
uv sync                                              # 建 .venv（默认不装 torch）
uv run yixiang migrate                               # 期望：user_version=1
uv run yixiang rag ingest --source local --file evals/fixtures/media_sample.json
# 期望：local：共 31 条 · 新增 31 · 更新 0 · 跳过 0 · 向量 31（干净 data 目录）
#      若库里已有真抓语料，这句会变成"新增 0 · 跳过 31 · 向量 333"——也正常（source_id 幂等）
uv run yixiang doctor                                # 八项自检全绿再开始讲
```

## 1. 开场（0:00–0:20，照读）

> "前三周把它做出来了：能对话、有记忆、能查 500 部影视并给推荐。最后一周做的是**让它不许退步**——
> 一次提交想进来，得先过四步门禁；过了门禁还有五项判定，其中四项是硬的、一项只告警。
> 判定逻辑只有一份，不写在 CI 的 YAML 里。"

## 2. 四步门禁（0:20–0:55）

CI 的四步，在本机可以按同样顺序一条条跑（`release_gate` 自己会重跑一遍确定性用例，
所以本地这四步等价于 workflow 的四步）：

```powershell
uv run ruff check .
uv run pytest evals/deterministic -m "not live"
uv run yixiang skills validate
uv run python -m yixiang.ops.release_gate
```

实测输出：

```text
All checks passed!                                                    ← ① ruff
399 passed, 1 deselected in 8.8s                                      ← ② 确定性用例（耗时随负载浮动，约 8~12 秒）
校验通过：…\data\skills 下 0 个技能可用                                 ← ③ skills validate

发布门禁：硬门禁 4/4 通过                                              ← ④ release_gate
  ✓ 确定性用例通过率                   100.0% / 阈值 100.0%     [硬门禁]
      399 passed / 0 failed（pytest 退出码 0）
  ✓ judge 均分                     4.80 / 阈值 4.00       [硬门禁]
      10 条 · 均分 4.80 / 通过线 4.0（offline） · 最低：J-05=4；J-09=4
  ✓ 门控漏检率                        0.0% / 阈值 0.0%       [硬门禁]
      漏检 0.0%（硬门禁 0）· 误检 10.0%（容忍 ≤30%，实测口径 40 条标注）
  ✓ 检索 top-3 命中率                75.0% / 阈值 60.0%      [硬门禁]
      15/20 命中 · MRR 0.625 · 语料 333 部 · 嵌入 ok · 口味关（T3 口径）
告警项（不阻止合并）：
  ✓ 单轮成本上限                    ¥0.0150 / 阈值 ¥0.7500    [只告警]
      今天 2 轮 · 合计 ¥0.0236（预算 ¥0.50/天）· 最贵一轮上界 ¥0.0150
结论：全过，可以合并
```

> 讲点一（顺序不能换）："`ruff` 先跑，因为它最便宜、最快给出反馈；判定最后跑，因为它开销最大。
> 任何一步失败即停——失败的那一步就是结论，后面的步骤没有信息增量。"
>
> 讲点二（判定逻辑只有一份）："workflow 的最后一步就是 `python -m yixiang.ops.release_gate`，
> **阈值一个字都没抄进 YAML**。把考核标准复制成两份，才会出现'用例绿了但门禁红了'这种怪事——
> 而这种怪事，我这次真的踩到了：门禁里多带一个 `-q`，pytest 连 `N passed` 汇总行都不打印，
> 门禁读到的就是'0 passed / 0 failed'。它现在有一条回归用例（`test_release_gate.py`）看着。"
>
> 讲点三（成本只告警）："五项里前四项是硬门禁，成本那项**只告警**。超预算多半是'用户今天聊得多'，
> 不是代码退步；拿它拦合并，门禁很快就会变成没人看的噪音。"
>
> 讲点四（这里的 75.0% 与第 4 节的 90.0% 为什么不是同一个数）："门禁这一趟固定走 `YIXIANG_EMBED_BACKEND=hash`
> ——离线、零下载，CI 也这么跑；而本机 `data/` 里现在是 333 部真抓语料、向量表是**真嵌入**建的。
> `hash` 查询撞上 `fastembed` 向量表会被 `ReindexRequired` 守卫挡住（宁可不比，也不跨语义空间比分数），
> 于是退成纯 FTS5，门禁读到的就是 **75.0% / MRR 0.625（15/20）**。第 4 节那一步不设 `hash`，走真嵌入，
> 才是 **90.0% / MRR 0.792**。两个数都指得到源头，但**不能混着引用**——`docs/NUMBERS.md` 卡 2 就是按
> '考卷 × 语料 × 嵌入'分行列的。"

## 3. judge：10 条 rubric 与它的区分度（0:55–1:25）

```powershell
uv run yixiang eval judge
```

实测输出（节选）：

```text
10 条 · 均分 4.80 / 通过线 4.0（offline） · 最低：J-05=4；J-09=4
  · J-03 [推荐] 5/5 — 硬约束全过（离线只判 must_have / must_not_have / 长度，语气归 --live）
  · J-05 [计划] 4/5 — 超长：326 字 > 上限 300 字
  · J-09 [情绪陪伴] 4/5 — 缺 must_have：难受
  · J-10 [情绪陪伴] 5/5 — 硬约束全过（…）
```

> 讲点一（有区分度）："10 条里有两条**故意不满分**：J-05 内容全中但超长，J-09 走的是'接住情绪'
> 而不是复述关键词。如果十条全 5 分，说明打分器是橡皮图章——它证明不了任何事。"
>
> 讲点二（同源局限主动交底）："P0 的 judge 与 main 是同一族模型，自评有偏好偏差。我不打算装作
> 没有这个问题，而是同时上三条缓解：① 这个均分**只当回归警报**，不当质量结论；② 能客观判断的
> 部分（工具调没调、参数对不对、有没有编造）**早就下沉成确定性断言**了，judge 只判语气与合理性；
> ③ 换 judge 模型时要重跑全部历史分数校准，否则新旧分数不可比。"
>
> 讲点三（离线 vs live）："这条命令默认跑**离线基线**：用题面里录好的候选回复过一遍确定性 rubric。
> 它证明的是'题面、`{score,reasons[]}` 解析器、留痕管线没坏'——零成本、可重复、进 CI。
> 真模型打分走 `--live`，只跑 nightly 与发版前；解析失败那一条计 0 并把原始输出留在报告里。"

## 4. 检索回归与门控的不对称（1:25–1:55）

```powershell
uv run yixiang rag eval            # 门禁挂在这个数上（不设 YIXIANG_EMBED_BACKEND=hash 就是真嵌入）
uv run yixiang rag eval --holdout  # 发版前防过拟合
```

实测输出（**真嵌入 `bge-small-zh-v1.5` + 333 部真抓语料**）：

```text
检索评测（media.jsonl）：20 条查询 · top-3 · 语料 333 部 · 嵌入 可用 · 口味 关（只测相关性）
top-3 命中率：90.0%（18/20，目标 ≥60%） · MRR 0.792 → 通过
  ✗ 轻松治愈的日常番 —— 期望 轻音少女/白箱/别对映像研出手！/给桃子的信/龙猫/紫罗兰永恒花园，实际 top-3：悠哉日常大王 Nonstop、悠哉日常大王、悠哉日常大王 Repeat
  ✗ 时间旅行题材 —— 期望 命运石之门/重启咲良田/夏日重现/星际穿越，实际 top-3：奇诺之旅、比宇宙更远的地方、古诺希亚

检索评测（media_holdout.jsonl）：10 条查询 · top-3 · 语料 333 部 · 嵌入 可用 · 口味 关（只测相关性）
top-3 命中率：90.0%（9/10，目标 ≥60%） · MRR 0.750 → 通过
  ✗ 日常校园生活的番 —— 期望 凉宫春日的忧郁/别对映像研出手！/轻音少女/声之形，实际 top-3：跃动青春、悠哉日常大王 Nonstop、悠哉日常大王 Repeat
```

同一份 20 条考卷、同一份 333 部语料，换成 `hash` 假嵌入是 **70.0%（14/20）/ MRR 0.667**——真嵌入把 MRR 拉回 0.792
（换的是"排序质量"，不是"有没有结果"）。31 部离线语料那一档见 `docs/NUMBERS.md` 卡 2：`hash` 90.0% / MRR 0.792，
holdout 100% / MRR 0.867。

> 讲点一（两条口径）："评测**关口味**（`use_taste=False`）也**关去重**——门禁量的是'相关性排序有没有
> 退步'，数字不能随 `user.md` 内容漂移，也不能被 7 天去重窗口把正确答案挡在门外。带口味的排序
> 只出现在 `ops explain-search` 与 `yixiang brief` 里。"
>
> 讲点二（降级仍可用）："把嵌入拿掉只走 FTS5，同一份考卷在 **31 部语料**下还有 **85.0%（17/20）**、
> MRR 0.800，在 **333 部语料**下是 75.0%（15/20）、MRR 0.625——功能不会因为模型没下下来就不可用，
> trace 里记 `E_EMBED_UNAVAILABLE`，用户无感。"
>
> 讲点三（不对称阈值）："门控是**漏检 = 0**、误检容忍 ≤30%（实测 10%）。漏检 = 该检索没检索 =
> 失忆，用户直接感知；误检只是多注入几段记忆，代价是延迟和 token。调优方向永远是'宁可多检索'。"

## 5. 常驻调度：幂等 + 异常隔离（1:55–2:25）

三个 job 的排期与幂等键（写在 `yixiang/scheduler/jobs.py` 的模块 docstring 里）：

| job | cron | 幂等键 | 说明 |
|---|---|---|---|
| 巩固兜底 | `30 23 * * *` | 水印（§7.7.1） | 当天没凑够阈值也要蒸馏一次 |
| 每日汇总 | `50 23 * * *` | `usage:YYYY-MM-DD` | 写 `data/reports/usage-YYYY-MM-DD.json` |
| 记忆巡检 | `0 22 * * 0` | `verify:YYYY-Www` | 周日三方对账 |

```powershell
uv run yixiang eval scheduler -v
```

实测输出：

```text
evals\deterministic\test_scheduler.py .............s                     [100%]
======================== 13 passed, 1 skipped in 0.30s ========================
```

> 讲点一（幂等靠表，不靠记忆）："`scheduled_runs` 的 `UNIQUE(job, run_date)` 是唯一事实来源。
> 已经 `ok` 的当天任务再触发一次 = 直接跳过。**失败的 job 不占位**——只有 `ok` 才挡住重试，
> `failed` 留着让下一次补上（这正是 D-27 的补发语义，P2 才实跑）。"
>
> 讲点二（失败可见但不杀主链路）："`run_job` 捕获一切异常，写一行 `data/logs/jobs-*.jsonl`，
> 然后正常返回。巩固失败绝不能让用户发不出消息。但'失败可见'不等于'允许天天失败'——
> '今天没有新事实可写'是正常结果，记 detail 就够了，不当 failed。把安静的每一天标成失败，
> 第二天就没人看这一列了。"
>
> 讲点三（为什么时间一律注入）："这一层没有任何 `sleep`——`now()` 从 Clock 注入，所以'同日重复触发'
> '跨周补跑'这些场景能在 0.3 秒里跑完且完全确定。假时钟是定时任务可测的唯一前提。"

## 6. 备份与恢复演练（2:25–2:50）

```powershell
uv run yixiang backup                    # VACUUM INTO 日快照 + data/ 私有仓提交
uv run yixiang backup gc --keep-days 30  # 回收过期快照
uv run python scripts/restore_drill.py   # 演练：把快照当唯一库源启动一次
```

实测输出：

```text
快照：…\data\backups\state-20260920.db（2604.0 KB）
私有仓：…\data · 最近提交 c0428f6 snapshot 2026-09-20
回收 0 份超过 30 天的快照（只动 backups/state-*.db）

恢复演练：快照 state-20260920.db（2604.0 KB）→ %TEMP%\yixiang-restore-d0niypwc
① 重建：state.db · integrity_check = ok · sqlite-vec 已加载
② 表结构：27 张表 / user_version 1 与现役一致
③ 行数：逐表与现役一致
④ 三方对账：memory.md / 数据库 / FTS 索引一致
恢复演练通过：这份快照可以当作唯一的库源启动
```

> 讲点一（备份不是可选项）："记忆是这个项目最有价值的产物。`VACUUM INTO` 在 WAL 下是安全的，
> 不需要停写；`data/` 同时是一个**私有仓**，只版本化三文件 / `skills/` / `briefs/`——**永不推远端**。
> `state.db` 里有用户说过的一切，它本身不进任何远端仓库。"
>
> 讲点二（演练过才叫能恢复）："建快照容易，敢说'快照能当唯一库源'很难。演练脚本在**临时目录**里重建一次
> （不动真实 `state.db`），然后逐项对账：表集合与 `user_version`、逐表行数、以及在恢复目录里跑一次
> `memory verify` 的三方对账。三步全绿才叫演练通过。"

## 7. 收尾三句（2:50–3:00，照读）

1. **退步会被拦住**：CI 四步 + `release_gate` 五项（确定性 100% / judge ≥4.0 / 门控漏检 0 /
   top-3 ≥60% / 成本告警），判定逻辑只有一份，退出码就是结论；
2. **每条数字都能指到源头**：完整实测 6.5 秒（随负载浮动）、零成本；检索 90.0% / MRR 0.792
   （333 部真抓语料 + 真嵌入；同语料 `hash` 假嵌入 70.0% / 0.667，holdout 90.0% / 0.750）；
   judge 4.80；成本口径与算法在 `docs/NUMBERS.md`——不是截图，是复算命令；
3. **收口这一周没加功能**：四个子系统各自冻结，P2（定时推送 / QQ 入口）只留接口、不进任何门禁；
   收口之后补的 Web 控制台只是**范围外的入口层**（第 10 节加演，不进任何门禁）。
   这个项目不是"调通了 API 的聊天机器人"，是**有记忆、有评测、有成本账、连续在用的个人 Agent**。

## 8. 会被问到的问题（答案在仓库里）

| 问题 | 一句话答案 | 深挖时翻到 |
|---|---|---|
| 这 40 个测试能证明什么？ | 证明的层次不同：L1/L2 证明行为确定，L3 证明检索没退步，L4 只当回归警报 | `docs/architecture.md` §5 |
| judge 自己给自己打分，不算自说自话吗？ | 算，所以我把它降级成回归警报，客观项下沉成确定性断言 | TECH §13.4 / `evals/judge/run_judge.py` 头部注释 |
| live 用例为什么不进 PR？ | 外部 API 抖动会阻塞开发，成本还会随提交次数线性增长 | `.github/workflows/ci.yml` 头部注释 |
| 成本数字是账单还是估算？ | 是模型估算（口径 + 算法 + 敏感性都在文档里）；真实账单要等一周使用 | `docs/NUMBERS.md` 卡 1 |
| 门禁为什么不拦成本？ | 超预算多半是用户聊得多，不是代码退步；拦了会把门禁变成噪音 | `ops/release_gate.py` `_cost_check` |
| 为什么 CI 用假嵌入？ | 门禁量的是检索链路有没有退步，不是嵌入模型的质量；真嵌入不下载=CI 不赌网络 | `ci.yml` / `rag/embed.py` |
| 巩固任务天天失败怎么办？ | 失败可见（`scheduled_runs` + `logs/jobs-*.jsonl`），但不占位，下一次会补 | `scheduler/jobs.py` |
| 快照能恢复，凭什么信？ | 演练脚本在临时目录重建 + 结构/行数/三方对账逐项断言，退出码 0 才叫过 | `scripts/restore_drill.py` |
| 一周里真实用出来的 bug 补用例了吗？ | 补了：门禁双 `-q` 导致读不到汇总行、按路径加载模块未登记 `sys.modules` | `evals/deterministic/test_release_gate.py` |
| 陌生人真能跑起来吗？ | README 三条命令 + 数据边界表；离线入口连 key 都不需要 | `README.md` |

## 9. 兜底：没有网络 / 没有 key 时怎么演

第 2~6 步**全部不需要网络和 API key**（第 3 步的离线基线就是为这个场景准备的）：

```powershell
$env:YIXIANG_EMBED_BACKEND='hash'; $env:PYTHONUTF8='1'
uv run ruff check .
uv run pytest evals/deterministic -m "not live" -rs        # 399 passed, 1 deselected
uv run yixiang eval judge                                  # 4.80（offline）
uv run yixiang rag eval                                    # hash 口径：75.0% / MRR 0.625（333 部语料）
uv run python -m yixiang.ops.release_gate                  # 硬门禁 4/4
uv run python scripts/restore_drill.py                     # 恢复演练通过
```

上面把 `YIXIANG_EMBED_BACKEND` 去掉再跑 `uv run yixiang rag eval` 就是真嵌入那一档：**90.0% / MRR 0.792**
（模型已在 `~/.cache/fastembed`，不会再下载）。

整段演示的真正门槛只有一个：`.venv` 建好、数据目录里有一份 `data/state.db`（本机是 333 部真抓语料）。
没有语料时 `release_gate` 会自己按离线 fixture 入库再评测（`rag eval --source local --file` 的同一条路径），
所以**干净环境里第一次跑门禁也能出真实数字**，而不是"没有语料 → 假装通过"。

现场翻车时的动作顺序：① `uv run yixiang doctor` 看八项自检；② `uv run yixiang ops tail` 看今天有没有 trace；
③ 实在不行，切到第 9 步的离线五条命令——**它们和在线路径共用同一份代码**，只是把真模型换成假时钟与假 Provider。

## 10. 加演：Web 控制台（可选，**不计入 3 分钟**）

这是 W4 收口**之后**补的**范围外入口**（`parts/PART-4-eval-ops.md` §2 已回填说明）。它的定位要说在前面：
**不是交付物、不参与 `release_gate`、不进 CI 门禁**；存在的意义是让"人设 / 记忆 / 配置"这些平时只躺在文件里的
东西能当场点开——**远程共享屏幕时的加分项**。

```powershell
uv run yixiang web            # 只绑 127.0.0.1:8765；标准库实现，无前端构建步骤
```

打开 `http://127.0.0.1:8765/`，七栏从左到右，每栏一句话就能讲完：

| 栏 | 现场动作 | 想证明什么 |
|---|---|---|
| 对话 | **连着发两句**（第二句必须也正常） | `4b2883d` 修的 bug 现场：入口共用一条常驻事件循环，第二句不会再 `Event loop is closed` |
| 历史对话 | 切到上一个会话，从旧到新翻一遍 | 会话不是"聊完就扔"，`chat_log` 真的落了盘 |
| 人设与记忆 | 改一行 `soul.md` 保存 → 看容量条 | 上限校验（`soul` 3000 / `user` 4000 字符、memory 活跃区 150 行）；超限**一个字节都不落盘**，不是静默截断 |
| 模型配置 | 看 key 只出掩码；改一个非密配置 | 密钥永不回前端（T-4），改完写回 `.env` 且保留原有注释 |
| 提示词 | 看 system 段的拼装顺序 | 静态在前、动态在后（TECH §4.5）——前缀缓存能不能命中就看它 |
| QQ 设置 | 开 QQ、白名单留空 → 保存被拒 | 安全默认值：空白名单 = 拒绝一切外部消息（TECH §3.1） |
| 链路 trace | 点开最后一轮 turn → 看迭代 / 工具调用 / tokens / 成本 | 与 `yixiang ops show-trace <turn_id>` 同源：终端能看到的，前端不再写第二份 |

> 讲点（一句话）："前端是**手写的、没有构建步骤**的，它只是把已经存在的适配层（`web/console.py`）
> 接到 HTTP + SSE 上；要讲的东西（门控 / 检索 / 记忆 / 成本）还是终端里那套代码，没有为演示写第二遍逻辑。"

> 上传那条路径也可以顺手演：拖一个 `.md` 进去 → 它走的是 `read_file` 工具的同一份白名单与路径校验，
> 文件名会被消毒、同名不覆盖（`evals/deterministic/test_web.py` 24 条用例盯着这些边界）。

> 录屏取舍：3 分钟正片用终端就够；加演这一段建议只在**远程面试**、或者对方主动问"有没有界面"时展开。
