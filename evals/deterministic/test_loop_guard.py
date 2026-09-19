"""D-19 / D-20 与 guard 的三条防线（PART-1 §7、TECH §5.1）。

三条防线各防一类事故，用例也按这个顺序把账算清：

  * **工具失败**（D-19）：工具抛异常不炸 loop，用户看到的是错误码对应的人话；
  * **重复调用**（D-20）：同一轮里第 2 次相同调用不执行，第 3 次直接打断——
    否则"重复订两次日历"这类 bug 会真的写两次库；
  * **迭代上限 / 连续失败放弃**：到线就停，停下时如实报告失败，不许编造成功。
"""

from __future__ import annotations

from fake_provider import FakeProvider, text_reply, tool_round

from yixiang.errors import E_TOOL_FAILED, user_message
from yixiang.loop.agent import ITER_LIMIT_NOTICE
from yixiang.ops.tracing import build_turn_record
from yixiang.tools.registry import Tool, ToolRegistry

USER_TEXT = "记一下交材料"


def broken_memo_registry(data_dir) -> ToolRegistry:
    """一个"写盘坏了"的 add_memo：调用即抛异常（D-19 的注入点）。"""

    def boom(content: str, due_at: str | None = None) -> str:  # noqa: ARG001
        raise RuntimeError("磁盘写不进去")

    return ToolRegistry(
        [
            Tool(
                name="add_memo",
                description="加一条备忘（本用例里它坏了）",
                input_schema={
                    "type": "object",
                    "properties": {
                        "content": {"type": "string"},
                        "due_at": {"type": "string"},
                    },
                    "required": ["content"],
                },
                fn=boom,
            )
        ],
        data_dir=data_dir,
    )


# ------------------------------------------------------------------ D-19
def test_d19_tool_exception_is_reported_and_the_loop_ends_normally(
    settings, session, clock, turn
):
    registry = broken_memo_registry(settings.data_dir)
    provider = FakeProvider(
        tool_round(("add_memo", {"content": "交材料"})),
        text_reply("抱歉，这条我没记住。"),
    )
    result = turn(session, registry, provider, USER_TEXT)

    # ① 请求侧：异常变成可行动的错误文本回喂给模型（§9.1），不是把 loop 炸掉
    assert len(provider.requests) == 2
    assert provider.requests[1].messages[-1].role == "tool"
    assert provider.requests[1].messages[-1].content.startswith(
        "Error running add_memo: 磁盘写不进去"
    )
    # ② 行为侧：失败只记一次、工具本身不自动重试，loop 正常收尾
    assert [event.ok for event in result.tool_calls] == [False]
    assert result.tool_calls[0].error == E_TOOL_FAILED
    assert result.finish_reason == "stop"
    # ③ 结果侧：用户看到的是"人话 + 干净的原因"，不是异常栈、也不是错误码
    assert result.error == E_TOOL_FAILED
    assert "这个操作失败了" in result.reply
    assert "add_memo：磁盘写不进去" in result.reply
    assert "Error running" not in result.reply
    assert E_TOOL_FAILED not in result.reply
    assert user_message(E_TOOL_FAILED, detail="add_memo：磁盘写不进去") in result.reply

    # trace 记的是错误码，且不落工具原文（§11.1）
    record = build_turn_record(
        result=result,
        session_id=session.session_id,
        source="cli",
        user_text=USER_TEXT,
        clock=clock,
    )
    assert record["tool_calls"][0]["error"] == E_TOOL_FAILED
    assert record["tool_calls"][0]["ok"] is False
    assert "output" not in record["tool_calls"][0]
    assert record["error"] == E_TOOL_FAILED


# ------------------------------------------------------------------ D-20
def test_d20_three_identical_calls_are_stopped_by_the_guard(
    settings, registry, session, conn, turn
):
    same_call = ("add_memo", {"content": "取快递", "due_at": "2026-09-19T18:00"})
    provider = FakeProvider(
        tool_round(same_call),
        tool_round(same_call),
        tool_round(same_call),
        text_reply("好，记下了。"),
    )
    result = turn(session, registry, provider, "记一下取快递")

    # ① 请求侧：第三次之后模型再没有被调用（复盘时能看出"token 花在哪停的"）
    assert len(provider.requests) == 3
    assert len(provider.remaining()) == 1  # 剧本里那句 text_reply 根本没用到
    # ② 行为侧：只有第一次真的写了库；第二次被拒（ms=0，没执行）；第三次直接打断
    assert conn.execute("SELECT COUNT(*) FROM memos").fetchone()[0] == 1
    assert [event.ok for event in result.tool_calls] == [True, False]
    duplicate = result.tool_calls[1]
    assert duplicate.error == E_TOOL_FAILED and duplicate.ms == 0
    assert "完全相同的工具与参数" in duplicate.output
    # ③ 结果侧：guard 的结束原因与给用户的提示都在结果里
    assert result.finish_reason == "guard_stop"
    assert "检测到连续 3 次" in result.reply


# -------------------------------------------------------- 迭代上限 / 放弃
def test_iteration_limit_ends_the_turn_with_a_notice(registry, session, turn):
    provider = FakeProvider(
        tool_round(("list_today", {"date": "2026-09-20"})),
        tool_round(("list_today", {"date": "2026-09-21"})),
        tool_round(("list_today", {"date": "2026-09-22"})),
    )
    result = turn(session, registry, provider, "帮我把这几天的安排翻一遍", max_iter=2)

    assert len(provider.requests) == 2  # 到上限就停，不烧第三次
    assert result.iterations == 2
    assert result.finish_reason == "iter_limit"
    assert result.reply == ITER_LIMIT_NOTICE


def test_repeated_failures_of_one_tool_end_with_give_up(settings, session, turn):
    """同一工具连续失败超过 ``tool_retry_max`` → 放弃并要求如实报告（§5.1）。"""
    seen: list[str] = []

    def flaky(note: str) -> str:
        seen.append(note)
        raise RuntimeError("网络不通")

    registry = ToolRegistry(
        [
            Tool(
                name="flaky",
                description="一个总坏的工具",
                input_schema={
                    "type": "object",
                    "properties": {"note": {"type": "string"}},
                    "required": ["note"],
                },
                fn=flaky,
            )
        ],
        data_dir=settings.data_dir,
    )
    provider = FakeProvider(
        tool_round(("flaky", {"note": "1"})),
        tool_round(("flaky", {"note": "2"})),
        tool_round(("flaky", {"note": "3"})),
        text_reply("这个操作没成功，我不编造。"),
    )
    result = turn(session, registry, provider, "试三次")

    # 参数不同 → 不是"重复调用"；三次都真执行了，失败计数才累得起来
    assert seen == ["1", "2", "3"]
    assert [event.ok for event in result.tool_calls] == [False, False, False]
    # 第三次回喂给模型的是"放弃"话术（要求它如实告知用户）
    third_output = provider.requests[3].messages[-1].content
    assert third_output.startswith("Error: flaky 连续失败 2 次，已放弃调用")
    assert "不要编造成功" in third_output
    # loop 正常收尾；失败仍然如实写进结果与用户可见文案
    assert result.finish_reason == "stop"
    assert "这个操作没成功，我不编造。" in result.reply
    assert result.error == E_TOOL_FAILED
    assert "这个操作失败了" in result.reply


def test_a_success_resets_the_consecutive_failure_counter(settings, session, turn):
    """失败计数是"连续"的：成功一次就清零，不能把偶发失败攒成放弃。"""
    attempts: list[str] = []

    def moody(note: str) -> str:
        attempts.append(note)
        if note == "ok":
            return "成功"
        return "Error: 这次不行"

    registry = ToolRegistry(
        [
            Tool(
                name="moody",
                description="时好时坏",
                input_schema={
                    "type": "object",
                    "properties": {"note": {"type": "string"}},
                    "required": ["note"],
                },
                fn=moody,
            )
        ],
        data_dir=settings.data_dir,
    )
    provider = FakeProvider(
        tool_round(("moody", {"note": "bad-1"})),
        tool_round(("moody", {"note": "bad-2"})),
        tool_round(("moody", {"note": "ok"})),
        tool_round(("moody", {"note": "bad-3"})),
        text_reply("好了。"),
    )
    result = turn(session, registry, provider, "来四次")

    assert attempts == ["bad-1", "bad-2", "ok", "bad-3"]
    # 中间那次成功把计数清零 → 第 4 次失败只算"第 1 次失败"，不该出现放弃话术
    assert all("已放弃调用" not in message.content for message in provider.requests[4].messages)
    assert result.finish_reason == "stop"
