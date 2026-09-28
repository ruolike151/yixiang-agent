"""D-02 计划查询：``list_today`` 只回今日 items，不编造（PART-1 §7、TECH §9.2）。

同一条契约在三个地方出现，用例就把三处都钉住：
  1. 工具 description 写着"严格按数据库返回"（模型唯一的决策依据）；
  2. 工具函数真的只 SELECT 当天（不顺手把明天的也捞出来）；
  3. 用例断言"模型看到的 tool 消息里没有明天的内容"。
"""

from __future__ import annotations

import json

from fake_provider import FakeProvider, text_reply, tool_round

from yixiang import memory
from yixiang.app import App
from yixiang.memory import semantic
from yixiang.runtime.session import SessionManager, detect_intent
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
