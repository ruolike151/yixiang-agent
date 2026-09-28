# 三张数字卡（成本 / 命中率 / 用例数）

> 版本 v1.0 · 数据快照 **2026-09-20** · 环境：Windows 10 / Python 3.12 / `.venv`
> 演示与 CI 统一用 `YIXIANG_EMBED_BACKEND=hash`（离线确定性嵌入，不下载模型）
> 一条规矩：**数字要么指得到源头，要么不写**。每张卡都有"复算"小节，照着跑能得到同一个数。

---

## 卡 1 · 成本

### 1.1 口径（先说清口径，否则数字没有意义）

| 项 | 取值 | 来源 |
|---|---|---|
| 计价函数 | `cost_cny(model, usage)`：三段价（未命中输入 / 命中输入 / 输出），元每百万 token | `yixiang/ops/pricing.py` |
| 价目 **B** | `(2.0, 0.5, 8.0)` = `deepseek-chat`，也是 `DEFAULT_PRICE`（未知模型宁可高估） | 同上 `PRICES` |
| 价目 **A** | `(1.0, 0.2, 2.0)` 入门国产模型假设价，TECH §15.2 的对照档 | 按同一公式外推，不在 `PRICES` 里 |
| 用量口径 | 40 轮主对话 + 40 次门控 + 5 次巩固 + 1 次日报 / 天 | TECH §15.2 |
| 单轮主对话 | 6k input（静态 4.2k + 动态 1.8k）+ 0.2k output | §15.1、§4.5 |
| 前缀缓存命中率 | 70% | TECH §15.2（**[假设]**，`PRODUCT.md` 的 ≤¥0.5/天 依赖它） |

前缀缓存命中率不是"优化项"，是预算成立的前提：**静态在前、动态在后**（§4.5）这条硬约束就是为了让它成立。

### 1.2 逐段明细

| 项 | 量（每天） | 价目 A | 价目 B |
|---|---|---|---|
| 主对话 · 静态命中缓存 | 4.2k × 70% × 40 = 117.6k | ¥0.0235 | ¥0.0588 |
| 主对话 · 静态未命中 | 4.2k × 30% × 40 = 50.4k | ¥0.0504 | ¥0.1008 |
| 主对话 · 动态部分 | 1.8k × 40 = 72k | ¥0.0720 | ¥0.1440 |
| 主对话 · 输出 | 0.2k × 40 = 8k | ¥0.0160 | ¥0.0640 |
| 门控（40 次） | 28k in / 0.2k out | ¥0.0284 | ¥0.0576 |
| 巩固（5 次） | 15k in / 1.5k out | ¥0.0180 | ¥0.0420 |
| 日报（1 次） | 2.5k in / 0.6k out | ¥0.0037 | ¥0.0098 |
| **合计** | **285.5k in（其中 117.6k 命中）/ 10.3k out** | **¥0.2120 / 天** | **¥0.4770 / 天** |

### 1.3 敏感性：哪一项崩了会怎样

| 变化 | 价目 A | 价目 B | 结论 |
|---|---|---|---|
| 基准 | ¥0.2120 | ¥0.4770 | 价目 B 已经贴着 ¥0.5/天 的预算线 |
| 前缀缓存命中率 70% → **0%** | ¥0.3061 | **¥0.6534** | 缓存不是优化项，是预算成立的前提 |
| 三文件写满（静态 4.2k → 14k） | ¥0.3845 | **¥0.8494** | 记忆容量上限必须有淘汰机制（§7.11） |
| 日轮次 40 → 80 | ¥0.3739 | **¥0.8446** | 轮次是最敏感变量——"能用本地小模型顶掉的活优先本地化" |

门控 + 巩固两项加起来占基准的 **约 21%**（本卡口径：这两类请求也按全额未命中价算；TECH §15.2 按"打上缓存价"估是约 8%）。不管取哪一档，结论一样：真正的大头永远是"每轮都要发出去的那 6k input"。

> **与 TECH §15.2 的差异，主动写出来**：TECH 表给的合计是 ≈¥0.20（A）/ ≈¥0.41（B）。本卡逐段复算得 ¥0.2120 / ¥0.4770，差值集中在门控 / 巩固 / 日报三行——TECH 按"这几类请求也能吃到前缀缓存价"估算，本卡按**全额未命中价**算（更保守 = 更高）。哪一档更接近现实，取决于这几类请求的 prompt 里稳定前缀占多大比例，这是**待实测项**。宁可现在偏高，也不给一个虚低的数字。

### 1.4 评测自身的成本

| 项 | 量 | 价目 A | 价目 B |
|---|---|---|---|
| judge 10 条（生成 + 裁判各一次，2k in + 0.3k out 每条；**上界口径**） | 20k in / 3k out | ¥0.0260 | **¥0.0640** |
| nightly 一个月（30 次 × 价目 B） | — | — | ≈¥1.9 |

这就是"live 不进 PR"的另一半理由：不是嫌贵，是不让成本随提交次数线性增长。

2026-09-21 起**裁判换到本机**（见卡 2 §2.3），这一行只剩生成那一半。最后一次 live 实测：

| 半边 | 发数 | 量 | 成本 |
|---|---|---|---|
| 生成（`deepseek-flash`） | 10 | 782 in / 2775 out | **¥0.0238** |
| 裁判（本机 `qwen3.5-9b-uncensored-vision:latest`） | 10 | 2815 in / 647 out | **¥0.0000** |

表里的 ¥0.0260 / ¥0.0640 是"每条 2k in"的上界估算，实测的题面只有 ~80 in，所以真实生成成本比估算低一档；**裁判那一半是真归零**。

### 1.5 实测（本机）

```text
$ python -m yixiang ops cost --day --explain
2026-09-20（day）：0 轮 · 0 次调用 · 0 in / 0 out · ¥0.0000
预算 ¥0.50/天 → 已用 0.0%
```

0 是因为这台机器上的演出**没有真模型调用**（离线入口 + FakeProvider）；`usage.jsonl` 只在真调用时追加一行。所以这一格要等真实使用一周后才有数——**这也正是它会被追问的地方**：现在的成本数字是模型算出来的，不是账单跑出来的，口径与算法先摆在这里。

### 1.6 护栏与门禁阈值

| 触发条件 | 动作 | 落地 |
|---|---|---|
| 单轮成本 > 日预算 × 1.5（¥0.50 × 1.5 = **¥0.75**） | 门禁里**只告警**，不阻止合并 | `ops/release_gate.py` `COST_BUDGET_FACTOR` |
| 单日累计 > ¥1.0 | 熔断告警：门控降级规则模式、巩固延后、提示切档 | TECH §15.2 |
| 单轮 input > 12k | 历史窗口减半并告警 | §15.2 |

成本项之所以**只告警**：超预算多半是"用户今天聊得多"，不是代码退步；用它拦合并会把门禁变成噪音。

## 卡 2 · 命中率

### 2.1 检索回归（L3 门禁）

```text
$ YIXIANG_EMBED_BACKEND=hash python -m yixiang rag eval --source local --file evals/fixtures/media_sample.json
检索评测（media.jsonl）：20 条查询 · top-3 · 语料 31 部 · 嵌入 可用（hash 假嵌入；真嵌入见下表最后两行） · 口味 关（只测相关性）
top-3 命中率：90.0%（18/20，目标 ≥60%） · MRR 0.792 → 通过
```

本机真抓一次（大盘 + 近五年增量两段，2026-09-21；出网需显式设 `HTTPS_PROXY=http://127.0.0.1:7897`）：

```text
$ YIXIANG_EMBED_BACKEND=hash python -m yixiang rag ingest --source bangumi --sort heat --min-rating 8 --min-votes 500 --pages 25 --want 200
bangumi：共 200 条 · 新增 200 · 更新 0 · 跳过 0 · 向量 231
media 表现有 231 部作品。
$ YIXIANG_EMBED_BACKEND=hash python -m yixiang rag ingest --source bangumi --sort heat --min-rating 7.5 --min-votes 500 --since 2021-09-21 --pages 30 --want 200
bangumi：共 128 条 · 新增 102 · 更新 0 · 跳过 26 · 向量 333
media 表现有 333 部作品。
$ YIXIANG_EMBED_BACKEND=hash python -m yixiang rag eval
检索评测（media.jsonl）：20 条查询 · top-3 · 语料 333 部 · 嵌入 可用（hash 假嵌入；真嵌入见下表最后两行） · 口味 关（只测相关性）
top-3 命中率：70.0%（14/20，目标 ≥60%） · MRR 0.667 → 通过
```

语料规模：**31（离线样例）+ 302（Bangumi 真抓两段去重后：大盘 200 + 近五年增量新增 102）= 333 部**。
那 302 部来自**真网络**（不是 fixture），原始响应按"排序 + 过滤口径"分名落在 `data/raw/`（`data/` 不进库）；
这条真网络路径（真响应 + 落盘 + 幂等入库）由 `evals/live/test_real_paths.py` 守着，跑法
`pytest evals/live -m live`，**默认门禁把它整棵目录排除在外**（`-m "not live"`）。

| 考卷 | 条数 | 语料 | 嵌入 | top-3 | MRR | 跑在哪 |
|---|---|---|---|---|---|---|
| `evals/golden/media.jsonl` | 20 | 31 部 | `hash` 可用 | **90.0%**（18/20） | 0.792 | 每次 PR + CI（门禁） |
| 同上，**嵌入不可用**（纯 FTS5 降级） | 20 | 31 部 | 降级 | **85.0%**（17/20） | 0.800 | 只用于证明"降级仍可用" |
| `evals/golden/media_holdout.jsonl` | 10 | 31 部 | `hash` 可用 | **100.0%**（10/10） | 0.867 | 发版前（防调参过拟合） |
| `evals/golden/media.jsonl`，**真抓两段之后** | 20 | **333 部** | `hash` 可用 | **70.0%**（14/20） | **0.667** | 本机真抓一次（2026-09-21），**不进 CI** |
| `evals/golden/media.jsonl`，**同上语料 + 真嵌入** | 20 | **333 部** | **`fastembed` 真嵌入**（`bge-small-zh-v1.5`，512 维） | **90.0%**（18/20） | **0.792** | nightly + 发版前（真模型） |
| `evals/golden/media_holdout.jsonl`，**同上语料 + 真嵌入** | 10 | **333 部** | **`fastembed` 真嵌入**（同上） | **90.0%**（9/10） | 0.750 | nightly + 发版前（真模型） |

> 口径说明：**看数字先看两件事——哪份语料、哪张嵌入。** 31 部那三行（90.0% / 0.792 / 85.0% / 100.0%）是 CI 每次跑的门禁数字，复现要把 `YIXIANG_DATA_DIR` 指到**空目录**（见复算清单①）——本机 `data/` 里已经有 333 部语料，直接跑同一条命令不会再围着 31 部评。333 部那几行来自本机真抓一次，**不进 CI**。`hash` 是假嵌入：语料从 31 部涨到 333 部后排序质量掉下来（MRR 0.792 → 0.667，top-3 90.0% → 70.0%）；换上真嵌入 `fastembed`（`bge-small-zh-v1.5`，512 维，模型在 `~/.cache/fastembed`）后，同一份 333 部语料拿到 **90.0% / MRR 0.792**——等于把 31 部那行的水平拿了回来，这就是 Task 8 的验收条件，实测已达成。333 部 + `hash` 的 6 条 MISS 是：`时间旅行题材`、`名字里带夏天的动画`、`宫崎骏的龙猫`、`不想看打斗的科幻`、`适合全家一起看的动画电影`、`有笑点但不是纯搞笑的犯罪片`（前两条与下面那张表里的是同一类问题）。
>
> 一个容易踩的口径坑，写出来免得复算时对不上：**向量表换过模型以后，`hash` 查询会被 `ReindexRequired` 守卫挡住、静默退成纯 FTS5。** `ensure_media_vec` 一旦发现库里存的是 `BAAI/bge-small-zh-v1.5`、而当前后端要写 `hash`，宁可不比也不混着比（跨语义空间比分数比"没有结果"更糟），于是向量那一路召回为空。本机现在跑 `YIXIANG_EMBED_BACKEND=hash python -m yixiang rag eval` 得到的是 **75.0%（15/20）/ MRR 0.625**，既不是 `hash` 那一行也不是降级那一行，而是"hash 查询 + 纯 FTS5 召回"的混合口径。要复现 `hash` 那一行，得像复算清单③那样**在库副本上先 `rag reindex`**。这个状态在 trace 里记成 `rag.reindex_required`，但 `rag eval` 的横幅仍然只写"嵌入 可用"——横幅看的是"后端能不能算向量"，看不到"向量表有没有被守卫挡下"，这是**已知的显示口径缺口**。

两条已知 MISS（都是"用近义说法描述一部片"）：

| 查询 | 期望 | 实际 top-3 | 说明 |
|---|---|---|---|
| `名字里带夏天的动画` | 夏日重现 / 夏日大作战 | 你的名字。、头脑特工队、声之形 | 关键词路被"名字"带偏；hash 是**假嵌入**，没有真语义 |
| `宫崎骏的龙猫` | 龙猫 | 你的名字。、疯狂的石头、凉宫春日的忧郁 | 语料里没有"宫崎骏"这个字段，只有导演名 |

> 这张表是 **31 部语料 + `hash`** 那一档的两个 MISS（同一份 20 条考卷）；同一份考卷在 **333 部语料 + 真嵌入**下是 90.0%（18/20）· MRR 0.792——`名字里带夏天的动画` 与 `宫崎骏的龙猫` **两条都翻成了 HIT**，真嵌入换来的另外两条 MISS 是 `轻松治愈的日常番` 与 `时间旅行题材`。口径差异在这里，别混着引用。

两条口径必须一起看，否则数字不可比：

1. **口味关**（`use_taste=False`）：门禁量的是"相关性排序有没有退步"，数字不能随 `user.md` 漂移。带口味的排序只出现在 `ops explain-search` 与 `yixiang brief` 里；
2. **去重关**（`exclude_recent_days=0`）：评测不是推荐——7 天去重窗口会把正确答案挡在门外。

### 2.2 门控（硬门禁：漏检 = 0）

```text
门控漏检率 0.0% / 阈值 0.0%  [硬门禁]
  漏检 0.0%（硬门禁 0）· 误检 10.0%（容忍 ≤30%，实测口径 40 条标注）
```

| 指标 | 值 | 分母 | 为什么是这个阈值 |
|---|---|---|---|
| 漏检率 | **0 / 20 = 0%** | 20 条"该检索" | 漏检 = 失忆，用户直接感知，是产品级事故 |
| 误检率 | 2 / 20 = **10%** | 20 条"不该检索" | 误检只是多注入几段记忆（延迟 + token），容忍 ≤30% |
| 规则层直接跳过率 | 8 / 40 = 20% | 40 条标注 | 明显无状态的短句不进模型，省一次调用 |

不对称是**故意**的：调优方向永远是"宁可多检索"。

### 2.3 judge（均分 ≥4.0，只当回归警报）

| 项 | 值 |
|---|---|
| 条数 / 覆盖 | 10 条，五类各 2 条：闲聊 / 推荐 / 计划 / 记忆管理 / 情绪陪伴 |
| 离线均分 | **4.80 / 5**（门线 4.0）——题面 + 解析器 + 留痕管线自检 |
| live 均分（2026-09-21） | **4.60 / 5**——生成 `deepseek-flash`，裁判 **本机 `qwen3.5-9b-uncensored-vision:latest`** |
| 最低两条 | 离线 `J-05` = 4、`J-09` = 4；live `J-02` = 4、`J-04` = 4 |
| 解析失败 | 0 条（裁判换本机后的第三轮起） |

那两条**故意不满分**：如果 10 条全 5 分，说明打分器是橡皮图章，没有区分度。`J-05` 的基线写得啰嗦（内容全中但超长）、`J-09` 走的是"接住情绪"而不是复述关键词——它们证明 rubric 真的在扣分。

离线分数**不代表回复质量**，只代表"题面、`{score,reasons[]}` 解析器、留痕管线没坏"。语气与合理性只有 `--live` 能判，而同族模型自评有偏好偏差（§13.4），所以这个数只当**回归警报**。

**judge 换家（§13.4 第三条缓解"换模型 → 重跑历史分数"）已在 2026-09-21 执行一次**：裁判从"与 main 同源的 DeepSeek"换成**本机 Ollama 的 qwen3.5**（`YIXIANG_JUDGE_API_BASE=http://127.0.0.1:11434/v1`，无需密钥，成本 ¥0）。新旧均分一起记：离线基线 **4.80** → live 换家后 **4.60**，分差 **0.20 < 0.5**，按规矩不需要"分差归因"，但仍要写清两者**不同口径**（离线只判 `must_have` / `must_not_have` / 长度，live 判语气与合理性），别混着引用。

同一轮还修掉两条"格式歪当判不出来"的假失败（都是格式问题、不是判断问题，详见 `evals/deterministic/test_judge_parse.py`）：

| 现场 | 现象 | 处置 |
|---|---|---|
| `J-01` | 内容全对，收尾把 `}]` 写成 `]]` 且漏了 `}` | 按平衡括号修回来，理由里留一句"已修复" |
| `J-02` | 把候选回复原文的引号抄进 `reasons`，JSON 不成形 | 带叮嘱**重问一次**（只许输出 JSON、理由里不许出现引号），留一句"已重问" |

加固前两轮 live 分别只得 4.20 / 4.30（各 1 条解析失败），加固后 10 条全解析出来。

### 2.4 推荐去重

| 项 | 值 |
|---|---|
| 去重窗口 | 7 天（对话与日报**共用**同一张 `recommend_log`） |
| 断言 | 连续两次按需推荐 → 集合交集为空（D-12） |
| 校准资产 | `evals/golden/dedup.jsonl` 30 对（20 同义 / 10 不同义），用于巩固去重阈值校准 |

## 卡 3 · 用例数

```text
$ python -m pytest evals/deterministic -o addopts= -q -m "not live" -rs
399 passed, 1 deselected in 8.79s
```

| 维度 | 数字 | 说明 |
|---|---|---|
| 收集总数 | **400** | 含 1 条 `-m live`（真模型用例） |
| `-m "not live"` 选中 | **399** | 每次 PR + CI 跑的就是这一档 |
| 通过 | **399** | 通过率 100%（门禁硬指标） |
| 跳过 | **0** | D-13（QQ 幂等）与 D-27（定时补发）随 QQ 网关与调度器落地**转成实跑**，不再 `skip` |
| 排除 | **1** | 标了 `-m live`，nightly / 发版前才跑 |
| 全量耗时 | **8.8 s** | 离线、零成本（耗时随负载浮动，约 8~12 秒；目标 ≤30 秒） |

逐文件分布（`-m "not live"` 收集数，合计 399，36 个文件）：

| 文件 | 条数 | 覆盖 |
|---|---|---|
| `test_retrieval.py` | 32 | 入库幂等、FTS 分词、RRF、硬过滤、口味加权、降级、注入包裹 |
| `test_provider.py` | 31 | 流式解析、超时、`finish_reason=length`、关思考开关（名单 / 单次请求）、坏 JSON、用量记账、多模态附图（越界 / 超大 / 单张坏图不拖垮整轮） |
| `test_web.py` | 36 | 七栏控制台：配置/人设/记忆读写、multipart 上传（30MB）、上传件列表 / 删除 / 清空 / 原图、`read_file` 越界、SSE 分帧、链路 trace 面板、Web 新会话固定落 `web:` 命名空间 |
| `test_tools_memo.py` | 27 | 备忘的幂等键 / 到期 / 去重 / 改期（严格解日 + 拒绝瞎猜） |
| `test_bangumi_collections.py` | 16 | Bangumi 收藏 → 口味画像（假 client，不出网） |
| `test_gate.py` | 16 | 门控规则层、模型层、fail-open、标注集指标 |
| `test_ingest_fetch.py` | 16 | 抓取的限速 / 退避 / 缓存 / 游标（假 client，不出网） |
| `test_scheduler.py` | 14 | 四个 job 的幂等、异常隔离、假时钟、补发 |
| `test_security.py` | 14 | 路径逃逸（D-22）、注入包裹、超长截断、QQ 幂等 |
| `test_doctor.py` | 14 | 八项自检、离线嵌入后端白名单、8766/8765 端口不撞车、输出上限默认值与手滑拦截 |
| `test_qq.py` | 22 | QQ 网关：分片、白名单、幂等、重连、**收图（多模态 / 暂存 / 降级 / CQ 串）**（D-13） |
| `test_bangumi_tools.py` | 10 | `bangumi_search` / `bangumi_subject` 的契约与降级 |
| `test_memory_write.py` | 22 | "记住"硬契约、纠错重试、失败可见、`memory.md` 正文编辑（add / replace / remove，唯一命中才动手） |
| `test_bangumi_proxy.py` | 8 | 仅 Bangumi 出口走代理，别的链路一字节不碰 |
| `test_docs_consistency.py` | 8 | doc 口径防漂移：项数 / 栏数 / "还不存在"句 / 目录树 |
| `test_event_loop.py` | 8 | 一条线程一条常驻 loop：两轮同 loop、收尾关闭、keep-alive 复用 |
| `test_judge_parse.py` | 8 | judge 判词解析容错 |
| `test_loop_guard.py` | 11 | 迭代上限、防绕圈、工具失败；输出截断的收尾口径（撞线先关思考重问一次 → 仍撞线才半篇 + 告知） |
| `test_sinks.py` | 8 | 晨报三通道（cli / file / toast）各自可断言，一次真通知都不弹 |
| `test_memory_sync.py` | 7 | 三方对账、手改生效、漂移检测 |
| `test_pricing.py` | 7 | 价目表覆盖、退役模型名守门 |
| `test_cli_gateway.py` | 5 | 斜杠命令、会话切换、流式渲染 |
| `test_db.py` | 5 | 迁移（D-26） |
| `test_docs_bangumi.py` | 5 | Bangumi 接入的文档口径（工具数、§9.2 清单） |
| `test_embed_contract.py` | 5 | 文档里的模型 / 维度与代码常量一致 |
| `test_memory_capacity.py` | 5 | 三文件上限与淘汰 |
| `test_sessions.py` | 6 | 会话改名 / 搜索 / 导出 / 删除 / 来源标注 |
| `test_tools_plan.py` | 10 | 计划工具（含 `reschedule_task` 改期 + 改期纠错重试）与工具痕迹折叠 |
| `test_consolidation.py` | 4 | 巩固三档阈值与质量门禁 |
| `test_release_gate.py` | 4 | 门禁判定自身 |
| `test_backup_secrets.py` | 3 | `.env` 可恢复副本（第 7 项自检的证据） |
| `test_golden_contract.py` | 3 | golden 集"只增不改"冻结契约 |
| `test_loop_context.py` | 3 | 上下文预算与裁剪顺序（D-21） |
| `test_serve.py` | 3 | `yixiang serve` 的接线：APScheduler 挂 JOBS + 两个开关 |
| `test_judge_live.py` | 2 | judge 的 `--live` 路径（跨用例复用 provider） |
| `test_markdown_js.py` | 1 | 前端 Markdown 渲染器（`node --test` 挂进 pytest） |

编号覆盖（PART-4 §7 的用例表）：

| 编号 | 内容 | 落在哪 | 状态 |
|---|---|---|---|
| D-12 | 推荐去重（连续两次无交集） | `test_retrieval.py` | 绿 |
| D-13 | QQ 幂等 | `test_security.py` / `test_qq.py` | 绿（网关落地后转实跑） |
| D-22 | 路径逃逸 | `test_security.py` | 绿 |
| D-26 | 迁移到最新 `user_version` | `test_db.py` | 绿 |
| D-27 | 定时补发 | `test_scheduler.py` | 绿（晨报 job 落地后转实跑） |
| J-01~J-10 | judge 10 条 rubric | `evals/judge/cases.yaml` | 均分 4.80 |
| — | job 幂等 / 异常隔离 / 假时钟 | `test_scheduler.py` | 绿 |
| — | 门禁判定与 `pytest` 参数口径 | `test_release_gate.py` | 绿 |

## 卡 4 · 备份与恢复演练（运维数字）

```text
$ python -m yixiang backup
快照：…\data\backups\state-20260920.db（2604.0 KB，随使用增长，下同）
私有仓：…\data · 最近提交 c0428f6 snapshot 2026-09-20

$ python scripts/restore_drill.py
恢复演练：快照 state-20260920.db（2604.0 KB）→ %TEMP%\yixiang-restore-d0niypwc
① 重建：state.db · integrity_check = ok · sqlite-vec 已加载
② 表结构：27 张表 / user_version 1 与现役一致
③ 行数：逐表与现役一致
④ 三方对账：memory.md / 数据库 / FTS 索引一致
恢复演练通过：这份快照可以当作唯一的库源启动
```

| 项 | 值 |
|---|---|
| 快照方式 | `VACUUM INTO`（WAL 下安全，不需要停写） |
| 保留策略 | 30 天（`backup gc --keep-days 30`，本次回收 0 份） |
| 私有仓版本化范围 | `soul.md` / `user.md` / `memory.md` / `skills/` / `briefs/`——**只这些** |
| 演练目录 | 临时目录，**不动真实 `state.db`** |
| 演练断言 | 表集合 + `user_version` 一致 · 逐表行数一致 · 恢复目录里 `memory verify` 三方对账一致 |

记忆是这个项目最有价值的产物，所以"快照能当唯一库源启动"必须是演练出来的，不是声称的。

## 复算清单

```bash
# 卡 1：成本
python -m yixiang ops cost --day --explain

# 卡 2：命中率（先分清"哪份语料 + 哪张嵌入"，数字才有意义）
# ① 31 部离线语料（CI 门禁口径）。必须把 data 指到空目录：本机 data/ 已有 333 部，
#    同一条命令会围着库里的语料评，不再是 31 部那几行。
YIXIANG_DATA_DIR=/tmp/yx-eval31 YIXIANG_EMBED_BACKEND=hash python -m yixiang rag eval --source local --file evals/fixtures/media_sample.json  # 31 部：90.0% / MRR 0.792
YIXIANG_DATA_DIR=/tmp/yx-eval31 YIXIANG_EMBED_BACKEND=hash python -m yixiang rag eval --holdout  # 31 部：100.0% / MRR 0.867

# ② 333 部真抓语料 + 真嵌入（本机 data/ 的现役口径；不加 YIXIANG_EMBED_BACKEND=hash 就是 fastembed 真模型）
python -m yixiang rag eval            # 90.0% / MRR 0.792
python -m yixiang rag eval --holdout  # 90.0% / MRR 0.750

# ③ 同一份 333 部语料 + hash 假嵌入：hash 查询撞上 fastembed 向量表会被 ReindexRequired 挡住，
#    所以要先拷一份库、在副本上 reindex 出 hash 向量，再评（直接在主库上换后端跑不出这一行）。
cp data/state.db /tmp/yx333/state.db
YIXIANG_DATA_DIR=/tmp/yx333 YIXIANG_EMBED_BACKEND=hash python -m yixiang rag reindex
YIXIANG_DATA_DIR=/tmp/yx333 YIXIANG_EMBED_BACKEND=hash python -m yixiang rag eval  # 333 部：70.0% / MRR 0.667

# ④ judge
YIXIANG_EMBED_BACKEND=hash python -m yixiang eval judge          # 4.80（离线基线：题面 + 解析器自检）
python -m yixiang eval judge --live                              # 4.60（生成走 DeepSeek，裁判走本机 Ollama qwen3.5，需先在跑 ollama serve）

# 卡 3：用例数
python -m pytest evals/deterministic -o addopts= -q -m "not live" -rs

# 卡 4：备份与恢复
python -m yixiang backup && python scripts/restore_drill.py

# 五项汇总（门禁阈值只有这一处）
YIXIANG_EMBED_BACKEND=hash python -m yixiang.ops.release_gate
```

数字的源头文件：`data/usage.jsonl`（成本）、`evals/golden/*.jsonl` + `evals/judge/reports/judge-YYYYMMDD.json`（命中率）、`evals/deterministic/`（用例数）、`data/backups/`（备份）。
