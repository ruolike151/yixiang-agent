"""D-02 计划查询：``list_today`` / ``list_range`` 只回库里的东西，不编造（PART-1 §7、TECH §9.2）。

同一条契约在三个地方出现，用例就把三处都钉住：
  1. 工具 description 写着"严格按数据库返回"（模型唯一的决策依据）；
  2. 工具函数真的只 SELECT 那一段（不顺手把下周的也捞出来）；
  3. 用例断言"模型看到的 tool 消息里没有别天的内容"。

2026-09-28 的真实事故让这份用例多了三组：

  * **看不见的入口等于不存在**：``list_today`` 早就支持 ``date``，但 description 写着
    "无参数"、schema 是空的，模型根本不知道能问别的日子 → 补 ``date`` 入口 + ``list_range``；
  * **模型自己打的工具痕迹**：QQ 那轮没调任何工具，却回了 ``[tools used: list_today]``
    （那是它自己写的字，不是程序拼的）→ 剥掉，工具痕迹只认程序那一行；
  * **"排了"必须真落库**：上一轮说"写一份任务清单"只是回了段聊天文字，``plans`` /
    ``plan_items`` 一直是空的 → PLAN 契约 + 后验纠错。

``your_plan.md`` 是给人看的那一面：DB 权威、文件可手改、改完同步回库（§7.3/§7.4 同一范式）。
"""

from __future__ import annotations

import json
from datetime import date

from fake_provider import FakeProvider, text_reply, tool_round

from yixiang import memory, plan_doc
from yixiang.app import App
from yixiang.memory import semantic
from yixiang.runtime.session import SessionManager, detect_intent
from yixiang.tools.memo import parse_range
from yixiang.tools.registry import build_registry


def seed(registry) -> dict[str, int]:
    """两周 RAG 计划：今天一条、明天一条、今天中午一条备忘。"""
    plan = json.loads(
        registry.run(
            "create_plan",
            {
                "title": "RAG 复习",
                "goal": "两周过一遍",
                "start_date": "2026-09-19",
                "end_date": "2026-10-02",
            },
        ).output
    )
    today = json.loads(
        registry.run(
            "add_task",
            {
                "plan_id": plan["plan_id"],
                "date": "2026-09-19",
                "content": "读 RAG 论文",
                "est_minutes": 60,
            },
        ).output
    )
    tomorrow = json.loads(
        registry.run(
            "add_task",
            {"plan_id": plan["plan_id"], "date": "2026-09-20", "content": "写向量检索 demo"},
        ).output
    )
    memo = json.loads(
        registry.run("add_memo", {"content": "交材料", "due_at": "2026-09-19T12:00"}).output
    )
    return {
        "plan": plan["plan_id"],
        "today": today["item_id"],
        "tomorrow": tomorrow["item_id"],
        "memo": memo["id"],
    }


def test_d02_list_today_returns_only_today(registry, session, conn, turn):
    ids = seed(registry)
    provider = FakeProvider(
        tool_round(("list_today", {})),
        text_reply("今天只有一件事：读 RAG 论文（60 分钟），另外中午前要交材料。"),
    )
    result = turn(session, registry, provider, "今天要干什么？")

    # ① 请求侧：模型看到的工具输出只有今日条目
    first, second = provider.requests
    assert first.messages[0].content == "今天要干什么？"
    assert "list_today" in [item["function"]["name"] for item in first.tools]
    seen = second.messages[-1].content
    assert second.messages[-1].role == "tool"
    assert seen.startswith("2026-09-19 的安排：")
    assert "读 RAG 论文" in seen and "60 分钟" in seen
    assert "到期备忘" in seen and "交材料" in seen
    assert "写向量检索 demo" not in seen  # 明天的任务不许出现在今天的结果里

    # ② 行为侧：只查了一次，没有任何写操作
    assert [event.tool for event in result.tool_calls] == ["list_today"]
    assert result.tool_calls[0].ok is True

    # ③ 结果侧：DB 没被改动，回复就是模型这一句话
    today_row = conn.execute(
        "SELECT status FROM plan_items WHERE id = ?", (ids["today"],)
    ).fetchone()
    assert today_row["status"] == "todo"
    assert conn.execute("SELECT COUNT(*) FROM plan_items").fetchone()[0] == 2
    assert result.reply.startswith("今天只有一件事")


def test_list_today_says_so_when_nothing_is_scheduled(registry):
    seed(registry)
    empty = registry.run("list_today", {"date": "2026-09-22"})
    assert empty.ok is True
    assert "没有安排" in empty.output
    assert "读 RAG 论文" not in empty.output


def test_complete_task_marks_done_and_rejects_unknown_id(registry):
    ids = seed(registry)
    done = registry.run("complete_task", {"item_id": ids["today"]})
    assert done.ok is True and json.loads(done.output)["status"] == "done"
    assert "（60 分钟，done）" in registry.run("list_today", {}).output

    missing = registry.run("complete_task", {"item_id": 9999})
    assert missing.ok is False
    assert missing.output.startswith("Error") and "list_today" in missing.output

    bad_status = registry.run("complete_task", {"item_id": ids["tomorrow"], "status": "later"})
    assert bad_status.ok is False and "done / skipped" in bad_status.output


def test_adding_a_task_to_an_unknown_plan_is_refused(registry):
    outcome = registry.run(
        "add_task", {"plan_id": 9999, "date": "2026-09-20", "content": "读论文"}
    )
    assert outcome.ok is False
    assert outcome.output.startswith("Error") and "create_plan" in outcome.output


def test_reschedule_task_moves_the_date_and_list_today_follows(registry, conn):
    """改期只动 ``date``：内容、状态、所属计划一个都不许被顺手改掉。

    结果侧故意查两处：库里那一列，以及 ``list_today`` 看不看得见——只改库、
    面板还按老日期过滤的话，"挪好了"就只是句空话。
    """
    ids = seed(registry)

    moved = registry.run("reschedule_task", {"item_id": ids["tomorrow"], "date": "2026-09-19"})

    assert moved.ok is True
    payload = json.loads(moved.output)
    assert payload["date"] == "2026-09-19" and payload["from"] == "2026-09-20"
    row = conn.execute(
        "SELECT content, status, plan_id FROM plan_items WHERE id = ?", (ids["tomorrow"],)
    ).fetchone()
    assert row["content"] == "写向量检索 demo" and row["status"] == "todo"
    assert row["plan_id"] == ids["plan"]
    assert "写向量检索 demo" in registry.run("list_today", {}).output
    assert "写向量检索 demo" not in registry.run("list_today", {"date": "2026-09-20"}).output


def test_reschedule_task_parses_natural_language_and_refuses_to_guess(registry):
    ids = seed(registry)

    moved = registry.run("reschedule_task", {"item_id": ids["today"], "date": "明天"})
    assert json.loads(moved.output)["date"] == "2026-09-20"

    missing = registry.run("reschedule_task", {"item_id": 9999, "date": "2026-09-20"})
    assert missing.ok is False
    assert missing.output.startswith("Error") and "list_today" in missing.output

    # "本周"只说了哪一周、没说哪一天：宁可让模型回去说明确，也不替用户挑一天写进库
    vague = registry.run("reschedule_task", {"item_id": ids["today"], "date": "本周"})
    assert vague.ok is False and vague.output.startswith("Error")


def test_a_reschedule_turn_lands_in_the_database(registry, session, conn, turn):
    """触发断言：用户说"挪"，这一轮就得真调到 ``reschedule_task``。

    上一轮的真实事故是模型回了"挪好了"而 ``tool_calls`` 为空、库里没动——
    所以这里断言的不是回复文案，而是 DB 那一列真的换了值。
    """
    ids = seed(registry)
    provider = FakeProvider(
        tool_round(("reschedule_task", {"item_id": ids["tomorrow"], "date": "2026-09-19"})),
        text_reply("挪好了：写向量检索 demo 改到今天。"),
    )

    result = turn(session, registry, provider, "把明天那条挪到今天")

    assert [event.tool for event in result.tool_calls] == ["reschedule_task"]
    assert result.tool_calls[0].ok is True
    row = conn.execute("SELECT date FROM plan_items WHERE id = ?", (ids["tomorrow"],)).fetchone()
    assert row["date"] == "2026-09-19"


def test_reschedule_intent_injects_the_contract(settings, conn, clock):
    """S8 契约只在说了"挪"的轮次注入，且写明"只在回复里说挪好了等于没挪"。"""
    asked = SessionManager(settings, store=conn, session_id="cli:test", clock=clock)
    asked.begin_turn("把周五那条挪到本周")

    contract = asked.turn_contract_block()

    assert detect_intent("把周五那条挪到本周") == "RESCHEDULE"
    assert "reschedule_task" in contract and "reschedule_memo" in contract
    assert "已经挪好了" in contract

    chatty = SessionManager(settings, store=conn, session_id="cli:test", clock=clock)
    chatty.begin_turn("今天有点累，随便聊聊")
    assert chatty.turn_intent == ""
    assert chatty.turn_contract_block() == ""


def test_a_reschedule_claim_without_a_tool_call_is_retried(settings, conn, clock, deps):
    """把上一轮真实事故变成一条回归：模型回"挪好了"却没调工具 → 纠错重试一次。

    契约（S8）只是"让模型更可能调"，真正兜底的是这条后验校验：本轮没成功调
    ``reschedule_task`` / ``reschedule_memo`` 就追加提醒重跑一遍 loop，与
    "记住"轮（``_verify_memory_write``）同一个范式。
    """
    ids = seed(build_registry(settings, deps))
    provider = FakeProvider(
        text_reply("挪好了：写向量检索 demo 改到今天。"),  # 第一次：没调工具
        tool_round(
            ("reschedule_task", {"item_id": ids["tomorrow"], "date": "2026-09-19"})
        ),
        text_reply("挪好了：写向量检索 demo 改到今天。"),
    )
    app = App.from_settings(
        settings, provider=provider, clock=clock, conn=conn, embedder=semantic.HashEmbedder()
    )
    try:
        result = app.ask("把明天那条挪到今天", stream=False)

        assert [event.tool for event in result.tool_calls] == ["reschedule_task"]
        assert result.tool_calls[0].ok is True
        row = conn.execute(
            "SELECT date FROM plan_items WHERE id = ?", (ids["tomorrow"],)
        ).fetchone()
        assert row["date"] == "2026-09-19"
        assert len(provider.requests) == 3  # 1 次只说话 + 1 次工具 + 1 次收尾
    finally:
        app.close()
        memory.reset()


def test_tool_trail_is_folded_into_history_not_the_raw_output(registry, session, conn, turn):
    """§5.3：历史里只留 ``[tools used: …]``，完整 tool result 不进历史。"""
    seed(registry)
    provider = FakeProvider(
        tool_round(("list_today", {})),
        text_reply("好，我看了今天的安排。"),
    )
    result = turn(session, registry, provider, "今天要干什么？")
    folded = result.fold_into_history()
    session.add_exchange("今天要干什么？", folded, result.tool_calls)

    assert "[tools used: list_today]" in folded
    # 工具原文（item_id / 排期标题）不进历史：省 token、防前缀缓存击穿；模型自己那句话照留
    assert "item_id" not in folded
    assert "2026-09-19 的安排" not in folded
    assert "好，我看了今天的安排。" in folded

    row = conn.execute("SELECT user_text, reply_text, tools_json FROM chat_log").fetchone()
    assert row["user_text"] == "今天要干什么？"
    assert "[tools used: list_today]" in row["reply_text"]
    assert json.loads(row["tools_json"])[0]["tool"] == "list_today"

    # 新会话从同一份 chat_log 重建历史时，看到的是折叠后的文本
    rebuilt = SessionManager(
        session.settings, store=conn, session_id="cli:test", clock=session.clock
    )
    assert rebuilt.history[-1].content == folded


# ── 看不见的入口等于不存在：list_today 的 date + list_range ──
def test_list_today_exposes_the_date_argument_it_always_supported(registry):
    """函数本来就吃 ``date``，可 description 写着"无参数"、schema 是空的。

    模型唯一的决策依据就是这两样，所以"参数存在但看不见"与"参数不存在"等价——
    2026-09-28 那轮 Web 会话因此只会答"我这边只能看到今天"。
    """
    tool = registry.get("list_today")
    assert tool is not None
    assert "date" in tool.input_schema["properties"]
    assert "无参数" not in tool.description
    assert "date" in tool.description


def test_parse_range_turns_week_words_into_monday_to_sunday(clock):
    """2026-09-19 是周六：本周 = 09-14~09-20，下周 = 09-21~09-27。"""
    now = clock.now()
    assert parse_range("本周", None, now) == (date(2026, 9, 14), date(2026, 9, 20))
    assert parse_range("这周", None, now) == (date(2026, 9, 14), date(2026, 9, 20))
    assert parse_range("下周", None, now) == (date(2026, 9, 21), date(2026, 9, 27))
    # 具体一天起算 → 默认往后一周；首尾都给 → 就用这两头
    assert parse_range("2026-09-01", None, now) == (date(2026, 9, 1), date(2026, 9, 7))
    assert parse_range("明天", "周五", now) == (date(2026, 9, 20), date(2026, 9, 25))
    # 说不清就不猜：由调用方给可行动的错误
    assert parse_range("随便写写", None, now) is None
    assert parse_range(None, None, now) is None


def test_list_range_answers_a_whole_week_from_one_call(registry):
    """用户问"这周有什么任务"，一次调用就要能答——只报库里有的，按天分组。"""
    seed(registry)
    outcome = registry.run("list_range", {"start_date": "本周"})

    assert outcome.ok is True
    text = outcome.output
    assert "2026-09-14" in text and "2026-09-20" in text
    assert "## 2026-09-19" in text and "## 2026-09-20" in text
    assert "读 RAG 论文" in text and "写向量检索 demo" in text
    assert "交材料" in text  # 当天到期的备忘也要进周视图
    assert "2026-09-21" not in text  # 下周的不许顺带捞出来


def test_list_range_says_so_when_the_window_is_empty(registry):
    seed(registry)
    outcome = registry.run("list_range", {"start_date": "2026-10-05", "end_date": "2026-10-11"})
    assert outcome.ok is True
    assert "没有安排" in outcome.output
    assert "读 RAG 论文" not in outcome.output


def test_list_range_refuses_a_window_it_cannot_parse(registry):
    vague = registry.run("list_range", {"start_date": "随便写写"})
    assert vague.ok is False
    assert vague.output.startswith("Error") and "YYYY-MM-DD" in vague.output

    backwards = registry.run("list_range", {"start_date": "2026-09-20", "end_date": "2026-09-14"})
    assert backwards.ok is False and backwards.output.startswith("Error")


def test_a_week_question_makes_the_agent_query_the_database(registry, session, conn, turn):
    """问排期 → 必须真查库再答；库是空的就如实说空。"""
    provider = FakeProvider(
        tool_round(("list_range", {"start_date": "本周"})),
        text_reply("这周库里没有安排。"),
    )
    result = turn(session, registry, provider, "我这周有什么任务？")

    assert [event.tool for event in result.tool_calls] == ["list_range"]
    assert result.tool_calls[0].ok is True
    seen = provider.requests[-1].messages[-1].content
    assert "没有安排" in seen
    assert conn.execute("SELECT COUNT(*) FROM plan_items").fetchone()[0] == 0


# ── 模型自己打的工具痕迹不算数 ──
def test_a_tools_used_line_written_by_the_model_is_stripped(registry, session, turn):
    """2026-09-28 的 QQ 轮：``tool_calls`` 为空、``iterations`` 为 1，回复末尾却挂着
    ``[tools used: list_today]``——那是模型自己写的字。工具痕迹只认程序拼的那行，
    否则用户看到的是"它查过了"，而事实上它什么都没查。"""
    provider = FakeProvider(
        text_reply("今天有两件事：拍证件照、投简历。\n\n[tools used: list_today]")
    )
    result = turn(session, registry, provider, "今天要干什么？")

    assert result.tool_calls == []
    assert "[tools used" not in result.reply
    assert result.reply == "今天有两件事：拍证件照、投简历。"
    assert result.fold_into_history() == result.reply


def test_a_real_tool_round_still_gets_the_program_written_trail(registry, session, turn):
    seed(registry)
    provider = FakeProvider(tool_round(("list_today", {})), text_reply("今天一条任务。"))
    result = turn(session, registry, provider, "今天要干什么？")
    assert "[tools used: list_today]" in result.fold_into_history()


# ── "排了计划"必须真落库 ──
PLAN_LIST_TEXT = "现在写一份下周的任务清单：1、拍证件照；2、熟悉简历上的项目。"


def test_a_plan_request_is_marked_and_gets_a_contract(settings, conn, clock):
    asked = SessionManager(settings, store=conn, session_id="cli:test", clock=clock)
    asked.begin_turn(PLAN_LIST_TEXT)
    contract = asked.turn_contract_block()

    assert detect_intent(PLAN_LIST_TEXT) == "PLAN"
    assert "create_plan" in contract and "add_task" in contract
    assert "等于没排" in contract
    # 既有打标不受影响："记一下周五交材料"是备忘（memo），不是排期
    assert detect_intent("记一下我周五中午前交材料") == ""
    assert detect_intent("把周五那条挪到本周") == "RESCHEDULE"


def test_asking_for_a_plan_lands_in_the_database(registry, session, conn, turn):
    """触发断言：清单只写在回复里 = 没排——库里必须有 plans / plan_items。"""
    provider = FakeProvider(
        tool_round(
            ("create_plan", {"title": "秋招准备", "start_date": "2026-09-21", "end_date": "2026-09-27"}),
            ("add_task", {"plan_id": 1, "date": "2026-09-21", "content": "拍证件照"}),
            ("add_task", {"plan_id": 1, "date": "2026-09-22", "content": "熟悉简历项目"}),
        ),
        text_reply("排好了：下周一拍证件照，周二熟悉简历项目。"),
    )
    result = turn(session, registry, provider, PLAN_LIST_TEXT)

    assert [event.tool for event in result.tool_calls] == [
        "create_plan",
        "add_task",
        "add_task",
    ]
    assert all(event.ok for event in result.tool_calls)
    assert conn.execute("SELECT COUNT(*) FROM plans").fetchone()[0] == 1
    rows = conn.execute("SELECT date, content FROM plan_items ORDER BY date").fetchall()
    assert [(row["date"], row["content"]) for row in rows] == [
        ("2026-09-21", "拍证件照"),
        ("2026-09-22", "熟悉简历项目"),
    ]


def test_a_plan_claim_without_a_tool_call_is_retried(settings, conn, clock, deps):
    """契约让模型更可能调工具，真正兜底的是后验校验：没落库就追加提醒重跑一次。"""
    provider = FakeProvider(
        text_reply("下周的任务清单：1、拍证件照；2、熟悉简历上的项目。"),  # 第一次：只在回复里列
        tool_round(
            ("create_plan", {"title": "秋招准备"}),
            ("add_task", {"plan_id": 1, "date": "2026-09-21", "content": "拍证件照"}),
        ),
        text_reply("排好了：下周一拍证件照。"),
    )
    app = App.from_settings(
        settings, provider=provider, clock=clock, conn=conn, embedder=semantic.HashEmbedder()
    )
    try:
        result = app.ask(PLAN_LIST_TEXT, stream=False)

        assert [event.tool for event in result.tool_calls] == ["create_plan", "add_task"]
        assert all(event.ok for event in result.tool_calls)
        assert conn.execute("SELECT COUNT(*) FROM plan_items").fetchone()[0] == 1
        assert len(provider.requests) == 3  # 1 次只说话 + 1 次落库 + 1 次收尾
    finally:
        app.close()
        memory.reset()


def test_a_schedule_answer_without_a_query_is_retried(settings, conn, clock, deps):
    """同一类事故的另一半：问"这周有什么"却凭历史背清单 → 追加提醒后必须真查库。"""
    registry = build_registry(settings, deps)
    seed(registry)
    provider = FakeProvider(
        text_reply("这周有三件事：拍证件照、投简历、熟悉项目。"),  # 第一次：一条都没查
        tool_round(("list_range", {"start_date": "本周"})),
        text_reply("这周只有两件事：读 RAG 论文、写向量检索 demo。"),
    )
    app = App.from_settings(
        settings, provider=provider, clock=clock, conn=conn, embedder=semantic.HashEmbedder(),
        registry=registry,
    )
    try:
        result = app.ask("我这周有什么任务？", stream=False)

        assert [event.tool for event in result.tool_calls] == ["list_range"]
        assert result.tool_calls[0].ok is True
        assert "读 RAG 论文" in provider.requests[-1].messages[-1].content
        assert len(provider.requests) == 3
    finally:
        app.close()
        memory.reset()


# ── your_plan.md：DB 权威 + 文件可手改 ──
def test_your_plan_md_renders_dates_and_tasks(settings, conn, registry, clock):
    ids = seed(registry)
    # 工具路径已经写过一次（"agent 也可以同步该文档"）：文件此刻就是库的渲染视图
    path = settings.data_dir / plan_doc.PLAN_FILE
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    assert plan_doc.PLAN_FORMAT_MARKER in text
    assert "## 2026-09-19（周六）" in text
    assert f"- [item_id={ids['today']}] 读 RAG 论文（60 分钟，todo）" in text
    assert f"- [item_id={ids['tomorrow']}] 写向量检索 demo（未估时，todo）" in text
    assert "交材料" not in text  # 备忘有自己的出口，不混进排期文件
    assert plan_doc.validate_plan_doc(text) == []

    # 幂等：文件没动过、也和库一致，同步直接跳过
    assert plan_doc.sync_plan_doc(conn, settings.data_dir, now=clock.now()).skipped is True

    # 另一头（Web / QQ 是两个进程，共用同一份 state.db）直接改了库：
    # 同步要跟得上，不能因为"文件自己没动"就把过期视图留着
    with conn:
        conn.execute(
            "UPDATE plan_items SET content = ? WHERE id = ?",
            ("读完 RAG 论文并做笔记", ids["today"]),
        )
    report = plan_doc.sync_plan_doc(conn, settings.data_dir, now=clock.now())
    assert report.changed is True
    assert "读完 RAG 论文并做笔记（60 分钟，todo）" in path.read_text(encoding="utf-8")


def test_editing_your_plan_md_by_hand_is_absorbed_into_the_database(
    settings, conn, registry, clock
):
    """人改文件是权威（与 ``memory.md`` 同一条纪律）：改文字 / 删整行 / 加一行。"""
    ids = seed(registry)
    plan_doc.sync_plan_doc(conn, settings.data_dir, now=clock.now())
    path = settings.data_dir / plan_doc.PLAN_FILE
    lines = path.read_text(encoding="utf-8").splitlines()
    edited: list[str] = []
    for line in lines:
        if f"item_id={ids['today']}" in line:
            edited.append(line.replace("读 RAG 论文", "读完 RAG 论文并做笔记"))
            continue
        if f"item_id={ids['tomorrow']}" in line:
            continue  # 删整行 = 把这条移出排期
        edited.append(line)
        if line.startswith("## 2026-09-20"):
            edited.append("")  # 手写一条没有 id 的新任务
            edited.append("- 复习项目里最难的两个问题（90 分钟，todo）")
    path.write_text("\n".join(edited) + "\n", encoding="utf-8")

    report = plan_doc.sync_plan_doc(conn, settings.data_dir, now=clock.now())

    assert report.updated == 1 and report.removed == 1 and report.inserted == 1
    row = conn.execute(
        "SELECT content FROM plan_items WHERE id = ?", (ids["today"],)
    ).fetchone()
    assert row["content"] == "读完 RAG 论文并做笔记"
    row = conn.execute(
        "SELECT status FROM plan_items WHERE id = ?", (ids["tomorrow"],)
    ).fetchone()
    assert row["status"] == "skipped"  # 库里留痕，不物理删
    written = conn.execute(
        "SELECT id, date, content, est_minutes FROM plan_items WHERE content LIKE '复习项目%'"
    ).fetchone()
    assert written is not None and written["date"] == "2026-09-20"
    assert written["est_minutes"] == 90

    # 新任务拿到 id 并回写文件；被移出排期的那条不再出现在文件里
    text = path.read_text(encoding="utf-8")
    assert f"- [item_id={written['id']}] 复习项目里最难的两个问题（90 分钟，todo）" in text
    assert f"item_id={ids['tomorrow']}" not in text


def test_your_plan_md_without_the_format_marker_is_left_alone(settings, conn, registry, clock):
    """文件被写坏（少了格式标记）时**一个字节都不动**：宁可不同步，也不能抹掉人手写的东西。"""
    seed(registry)
    path = settings.data_dir / plan_doc.PLAN_FILE
    path.write_text("# 我自己的排期\n\n随便写点什么\n", encoding="utf-8")

    report = plan_doc.sync_plan_doc(conn, settings.data_dir, now=clock.now())

    assert report.changed is False
    assert report.warnings and "格式标记" in "".join(report.warnings)
    assert path.read_text(encoding="utf-8").startswith("# 我自己的排期")


def test_plan_tools_write_the_file_after_each_change(settings, deps, clock):
    """工具路径上多了一跳：计划一变，文件立刻跟上（"agent 也可以同步该文档"）。"""
    registry = build_registry(settings, deps)
    plan = json.loads(registry.run("create_plan", {"title": "秋招准备"}).output)
    registry.run(
        "add_task", {"plan_id": plan["plan_id"], "date": "2026-09-21", "content": "拍证件照"}
    )
    text = (settings.data_dir / plan_doc.PLAN_FILE).read_text(encoding="utf-8")
    assert "- [item_id=1] 拍证件照（未估时，todo）" in text  # 条目级 id 是"人改文件"对齐库的锚点

    assert registry.run("list_range", {"start_date": "下周"}).ok is True  # 只是确认它不炸
    registry.run("complete_task", {"item_id": 1})
    assert "拍证件照" not in (settings.data_dir / plan_doc.PLAN_FILE).read_text(encoding="utf-8")


def test_app_startup_renders_your_plan_md_from_the_database(settings, conn, registry, clock):
    """启动同步（`App.__post_init__`）：视图文件不在了就从库里重渲染一份。

    Web 控制台与 QQ 网关是两个进程、共用一份 `state.db`：后起的那个进程启动时
    必须把这份视图补上，否则用户看到的是上一台机器留下的旧排期。
    """
    ids = seed(registry)
    path = settings.data_dir / plan_doc.PLAN_FILE
    path.unlink()
    (settings.data_dir / plan_doc.HASH_FILE).unlink(missing_ok=True)

    app = App.from_settings(settings, clock=clock, conn=conn, embedder=semantic.HashEmbedder())
    try:
        assert path.is_file()
        text = path.read_text(encoding="utf-8")
        assert f"- [item_id={ids['today']}] 读 RAG 论文（60 分钟，todo）" in text
        assert plan_doc.validate_plan_doc(text) == []
    finally:
        app.close()
        memory.reset()
