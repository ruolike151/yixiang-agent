# 交接待办：PART 4 之后再执行

> 来源：PART 3 交付时留下的 6 个决策点（2026-09-20，commit `4230a8e`）。
> 触发时机：**PART 4 全部验收通过、`release_gate` 跑绿之后**再逐条过。
> 在此之前这 6 条都是「已知且已接受」的状态，**不阻塞任何门禁**——每条都写了当前默认值，不改也能交。

## 怎么用

- 每条都带「默认 / 动作 / 验收」三节：默认是现在已经在跑的行为，动作是真正要动手的事。
- 做完一条就把下面的 `- [ ]` 打成 `- [x]`，commit message 里引用编号（例：`chore(todo): 关掉 T1 真实抓取验证`）。
- **先做 T1**：它可能引发 T2 / T3 的数字变化；其余几条互相独立，顺序随意。
- T6 是 P2 可选项，不进交付门禁；真没时间，把它一直留在这里也是正确结果。

## 清单

- [ ] **T1 联网验证真实抓取与真实嵌入** —— 无网络只验过离线等价入口，抓取代码与嵌入后端一次都没在真网络 / 真模型上跑过
- [ ] **T2 golden 集两条已知 MISS 定案** —— 「宫崎骏的龙猫」是语料缺导演字段、「名字里带夏天的动画」是 hash 假嵌入失手，要么修要么把结论写进 `note`
- [ ] **T3 评测口径（关口味）正式化** —— 确认 `use_taste=False` 进 PART 4 回归说明；想量化口味就另开一份口味评测集，别混进相关性门禁
- [ ] **T4 `templates/user.md` 偏好写法固化为契约** —— 决定旧写法（「喜欢悬疑、科幻题材」）的兼容期什么时候收口
- [ ] **T5 D-24 降级标记的位置确认** —— `E_EMBED_UNAVAILABLE` 记在 trace 的 `rag` 字段、不进 `result.error`，确认巡检够用
- [ ] **T6 定时晨报推送（P2，可选项）** —— `sinks.py` 三通道 + APScheduler cron + 唤醒补发，不进任何门禁

## 明细

### T1 · 联网验证真实抓取与真实嵌入

**默认**：只验过离线等价入口（`--source local --file evals/fixtures/media_sample.json`，31 部）。
抓取代码（单线程 + `sleep(1.0)` + 退避重试 3 次 + `data/raw/` 缓存）与嵌入后端（fastembed
`bge-small-zh-v1.5`）是完整的，但一次都没在真网络 / 真模型上跑过。

**动作**：在有网的机器上跑

```bash
uv run yixiang rag ingest --source bangumi --tags 悬疑,科幻 --pages 5    # ≥500 部，约 5 分钟
uv run yixiang rag ingest --source bangumi --tags 悬疑,科幻 --pages 5    # 第二遍应全是「跳过」
uv run yixiang rag eval                                                  # 真实嵌入的指标
```

有 TMDb key 时再补一遍 `--source tmdb --genres <id>`。

**验收**：`rag eval` 走 `embed=ok` 而非降级路，golden top-3 命中率 ≥60%；实测数字回填到
`README.md` 的 PART 3 段与 `scripts/demo-week3.md` 第 3、6 步。

**涉及**：`yixiang/rag/ingest.py`、`yixiang/rag/embed.py`、`README.md` 边界段、PART-3 §7 离线纪律。

### T2 · golden 集两条已知 MISS 定案

**默认**：20 条人工标注里有 2 条故意留作已知 MISS，`evals/golden/media.jsonl` 的 `note` 已写明原因。

**动作**：复核这两条该修哪一边——

- `宫崎骏的龙猫`：是**语料字段缺导演**（补 `media` 字段 / 换一条语料），不是检索逻辑错；
- `名字里带夏天的动画`：hash 假嵌入失手、纯 FTS5 命中，真模型路（T1）本该命中，跑完 T1 再定去留。

**验收**：要么修掉，要么在 `note` 里把「为什么不修」写成结论；golden 集数量变化时同步更新
`README.md` 与 `evals/deterministic/test_retrieval.py` 里的断言（`len(cases) == 20`）。

**涉及**：`evals/golden/media.jsonl`、`evals/fixtures/media_sample.json`、PART-3 §13.5「只增不改」。

### T3 · 评测口径（关口味）正式化

**默认**：`rag eval` 走 `use_taste=False`，只测相关性——数字不随 `user.md` 漂移，PART 4 的 L3
回归拿它当门禁；带口味的排序只出现在 `ops explain-search` 与 `yixiang brief`。

**动作**：确认这个口径写进 PART 4 的回归说明；如果还想量化「口味有没有起作用」，**另开一份
口味评测集**（期望是「用户会点开的作品」而不是「相关的作品」），不要混进相关性门禁。

**验收**：`evals/deterministic/test_retrieval.py` 里
`test_eval_metric_ignores_taste_while_explain_still_uses_it` 保持绿；L3 回归的数字与 `user.md` 内容无关。

**涉及**：`yixiang/rag/evaluate.py`、`yixiang/rag/retrieve.py`（`use_taste` 开关）。

### T4 · `templates/user.md` 偏好写法固化为契约

**默认**：约定写法 `- 喜欢：悬疑、科幻` / `- 不喜欢：恐怖`；旧的自由写法（「喜欢悬疑、科幻题材」）
仍能解析，兼容保留。

**动作**：决定兼容期什么时候收口——若确认只保留约定写法，就删掉 `taste.py` 里的旧写法分支与
对应用例；若保留，把兼容规则写进 `templates/user.md` 的注释（现状已写）。

**验收**：`test_preference_lines_accept_the_convention_and_the_old_writing` 与模板注释一致，
改完不留「文档说能解析、代码说不能」的错位。

**涉及**：`yixiang/rag/taste.py`、`templates/user.md`、PART-3 §8.5。

### T5 · D-24 降级标记的位置确认

**默认**：`E_EMBED_UNAVAILABLE` 记在 trace 的 `rag` 字段（`retrieve.trace_info()`），**没塞进
`result.error`**——否则 CLI 每轮都会顶一条 error，和「用户无感」的要求矛盾；用户侧只看到
`rag: 已降级（纯 FTS5）`。

**动作**：在 PART 4 的运维章节（doctor / trace 巡检）里确认这个位置够用——巡检要能靠 trace 发现
「这台机器一直在降级跑」；如不够，再加一条 doctor 检查项，而不是改回 `result.error`。

**验收**：`test_d24_user_notices_nothing_but_the_trace_says_it` 保持绿；trace 里能查到降级原因。

**涉及**：`yixiang/rag/retrieve.py`、`yixiang/app.py`、`yixiang/ops/show_trace.py`、TECH §8.4。

### T6 · 定时晨报推送（P2，可选项，不进交付门禁）

**默认**：**不做**。PART 3 只交付内容层 `daily_brief`，被对话、`yixiang brief`、未来定时任务三种
触发源复用；`gateway/sinks.py`（cli / file / toast）与 APScheduler cron + 唤醒补发都还没写。

**动作**（真要做时）：按 TECH §10.3 / §10.3.1 实现 `sinks.py` 三个投递通道 + `scheduler.py` 的
晨报 job + 补发算法，接 `YIXIANG_BRIEF_CRON` / `YIXIANG_BRIEF_CATCHUP_UNTIL` / `YIXIANG_BRIEF_SINK`，
并按 D-27 补「补发标注、第二次启动不重复发」的用例。

**验收**：D-27 用例绿；同一份组装逻辑不复制（触发源可换、内容层不 fork）。

**触发条件**：W4 收口、笔试面试有空档；预估约 1 天。QQ 接入（约 2 天）是同一批的 P2，另行决定。

**涉及**：`yixiang/gateway/sinks.py`、`yixiang/runtime/scheduler.py`、TECH §10.3 / §16.2 P2 段。

## 不在本清单里的（避免重复记录）

- **PART 4 本体**：judge 评测、L3 回归集、CI `release_gate`、README 陌生人能跑、录屏素材 ——
  见 [`parts/PART-4-eval-ops.md`](./parts/PART-4-eval-ops.md)。
- **已修复项**：`/cost` 跨零点算错（改用 App 时钟）已在 PART 3 提交内修掉，不在此列。
