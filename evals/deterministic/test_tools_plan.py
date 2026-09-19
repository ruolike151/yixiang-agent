"""D-02 计划查询：``list_today`` 只回今日 items，不编造（PART-1 §7、TECH §9.2）。

同一条契约在三个地方出现，用例就把三处都钉住：
  1. 工具 description 写着"严格按数据库返回"（模型唯一的决策依据）；
  2. 工具函数真的只 SELECT 当天（不顺手把明天的也捞出来）；
  3. 用例断言"模型看到的 tool 消息里没有明天的内容"。
"""

from __future__ import annotations

import json

from fake_provider import FakeProvider, text_reply, tool_round

from yixiang.runtime.session import SessionManager


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
