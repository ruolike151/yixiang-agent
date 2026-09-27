"""D-19 / D-20 与 guard 的三条防线（PART-1 §7、TECH §5.1）。

三条防线各防一类事故，用例也按这个顺序把账算清：

  * **工具失败**（D-19）：工具抛异常不炸 loop，用户看到的是错误码对应的人话；
  * **重复调用**（D-20）：同一轮里第 2 次相同调用不执行，第 3 次直接打断——
    否则"重复订两次日历"这类 bug 会真的写两次库；
  * **迭代上限 / 连续失败放弃**：到线就停，停下时如实报告失败，不许编造成功。
"""

from __future__ import annotations

from fake_provider import FakeProvider, text_reply, tool_round

from yixiang.errors import (
    E_LLM_TIMEOUT,
    E_LLM_TRUNCATED,
    E_TOOL_FAILED,
    ProviderError,
    user_message,
)
from yixiang.loop.agent import ITER_LIMIT_NOTICE
from yixiang.ops.tracing import build_turn_record
from yixiang.runtime.models import LoopEvent
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


# ------------------------------------------------- 输出上限（finish_reason=length）
HALF_ONE = "封面是绿色的：主色压在中低明度，留白集中在右侧"
HALF_TWO = "封面是绿色的：主色压在中低明度，留白集中在右侧，标题用衬线体……综合 6.5 / 10。"


def test_truncated_answer_is_retried_once_with_thinking_off(session, registry, turn):
    """撞输出上限 → 先自动**关掉思考**重问一次，第二轮正常收尾就不打扰用户。

    2026-09-27 实测（DeepSeek 直连探针）：会思考的模型把 ``reasoning`` 也算进同一个
    ``max_tokens`` 预算——同一句 prompt，10000 个 token 全花在思考上、正文一个字没吐。
    所以"撞线"的第一嫌疑不是正文太长，而是思考挤占了预算；把思考关掉重问一遍，
    用户看到的就是完整答案，连"被截断了"都不必提。
    """
    provider = FakeProvider(
        text_reply(HALF_ONE, finish_reason="length"),
        text_reply(HALF_TWO),
    )
    events: list[LoopEvent] = []

    result = turn(
        session, registry, provider, USER_TEXT, stream=True, observer=events.append
    )

    # ① 请求侧：第二发打的是**同一个问题**，只是带上了关思考的开关
    assert [req.thinking_disabled for req in provider.requests] == [False, True]
    assert (
        provider.requests[1].messages[-1].content
        == provider.requests[0].messages[-1].content
    )
    assert provider.requests[1].max_tokens == provider.requests[0].max_tokens
    # ② 行为侧：第一轮的半篇要**撤回**，不能让用户看到两版拼在一起
    revokes = [event for event in events if event.kind == "text_revoke"]
    assert [event.data.get("reason") for event in revokes] == ["truncated"]
    # ③ 结果侧：完整答案、无错误码、正常收尾
    assert result.reply == HALF_TWO
    assert result.error is None
    assert result.finish_reason == "stop"


def test_truncated_twice_keeps_the_answer_and_says_how_to_go_on(session, registry, turn):
    """两轮都撞线（关思考也没用）→ 半篇照发 + 末尾如实告知。

    半篇必须留住：流式期间用户明明看着它滚出来，收尾时用一句文案顶掉就是"看见了
    又没了"；而且丢掉的是**已经付过费**的输出。口径与工具失败那条一样——答案在前、
    原因在后。
    """
    provider = FakeProvider(
        text_reply(HALF_ONE, finish_reason="length"),
        text_reply(HALF_TWO, finish_reason="length"),
    )

    result = turn(session, registry, provider, USER_TEXT)

    assert result.finish_reason == "length"
    assert result.error == E_LLM_TRUNCATED
    # ① 重问的那一版更长，就发它；而且是在**最前面**（不是被错误文案顶掉）
    assert result.reply.startswith(HALF_TWO)
    # ② 末尾如实告知，并说清下一步怎么走
    assert user_message(E_LLM_TRUNCATED) in result.reply
    assert "继续" in result.reply
    # ③ 只重问一次：撞两次还接着重问就是拿用户的 token 硬顶
    assert len(provider.requests) == 2
    # ④ trace 里留得下"撞的是哪条线"，回去调 YIXIANG_MAX_TOKENS 时有据可依
    assert "max_tokens" in (result.error_detail or "")


def test_truncated_with_nothing_written_falls_back_to_the_notice(session, registry, turn):
    """一个字都没写出来就撞线（上限配得比思考量还小）：回复不能是空字符串。"""
    provider = FakeProvider(
        text_reply("", finish_reason="length"),
        text_reply("", finish_reason="length"),
    )

    result = turn(session, registry, provider, USER_TEXT)

    assert result.reply == user_message(E_LLM_TRUNCATED)
    assert result.error == E_LLM_TRUNCATED


def test_retry_that_dies_keeps_the_first_half(session, registry, turn):
    """重问本身失败（网络 / 鉴权）：第一轮那半篇不能跟着陪葬。"""
    provider = FakeProvider(
        text_reply(HALF_ONE, finish_reason="length"),
        ProviderError("网络错误：连接被重置", code=E_LLM_TIMEOUT),
    )

    result = turn(session, registry, provider, USER_TEXT)

    assert result.reply.startswith(HALF_ONE)
    assert result.error == E_LLM_TRUNCATED
    assert user_message(E_LLM_TRUNCATED) in result.reply


def test_a_model_already_configured_without_thinking_is_not_retried(
    settings, session, registry, turn
):
    """名单里的模型（``YIXIANG_NO_THINK_MODELS``）本来就没开思考：重问只会白花钱。"""
    settings.no_think_models = "deepseek-*"
    provider = FakeProvider(text_reply(HALF_ONE, finish_reason="length"))

    result = turn(session, registry, provider, USER_TEXT)

    assert len(provider.requests) == 1
    assert result.error == E_LLM_TRUNCATED


def test_main_call_carries_the_output_ceiling_from_settings(settings, session, registry, turn):
    """上限是配置项，不是写死在调用点的常量（§3）：请求里带的是 ``settings.max_tokens``。"""
    provider = FakeProvider(text_reply("好。"))

    turn(session, registry, provider, USER_TEXT)

    assert provider.requests[0].max_tokens == settings.max_tokens
