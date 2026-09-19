"""D-21 上下文裁剪：50 轮长历史 → input ≤ 预算、最早的历史被裁掉、最近 8 轮完整保留。

§6.2 的三条纪律一起钉住：

  1. 裁剪以**整轮**为单位（user+assistant 成对丢），绝不切半轮；
  2. 从最旧处开始丢，最近的轮次完整保留；
  3. 预算是**真的**：``ProviderRequest.input_chars()`` 才是模型看到的东西，
     所以断言打在这里，而不是打在被裁剪的中间变量上。

顺带把装配顺序钉死（§6.1 / §4.5）：S1…S8 共 8 段、静态在前、当前时间在最后——
时间戳一旦跑到开头，自动前缀缓存就永远不命中。
"""

from __future__ import annotations

from fake_provider import FakeProvider, text_reply

from yixiang.runtime.session import SessionManager

TOTAL_TURNS = 50
RECENT_TURNS = 8          # 最近 8 轮必须完整保留（§15.1 的成本口径）
OLD_TURNS = TOTAL_TURNS - RECENT_TURNS
USER_TEXT = "现在几点了？"


def seed_history(session: SessionManager) -> None:
    """42 轮"很久以前"的超大轮次 + 8 轮"最近"的适中轮次。"""
    for index in range(1, TOTAL_TURNS + 1):
        if index <= OLD_TURNS:
            text = f"很久以前{index}：" + "旧" * 480
        else:
            text = f"最近{index}：" + "新" * 96
        session.add_exchange(text, text, [])


def test_d21_old_history_is_dropped_whole_turns_and_recent_eight_survive(
    settings, conn, clock, registry, turn
):
    settings.history_turns = TOTAL_TURNS  # 窗口先开到 50 轮，裁剪才有活干
    seeded = SessionManager(settings, store=conn, session_id="cli:test", clock=clock)
    seed_history(seeded)

    session = SessionManager(settings, store=conn, session_id="cli:test", clock=clock)
    assert len(session.history) == TOTAL_TURNS * 2  # 从 chat_log 重建出 50 轮

    system_chars = len("\n\n".join(block for block in session.system_blocks() if block))
    recent_chars = RECENT_TURNS * 2 * len("最近50：" + "新" * 96)
    session.budget_chars = system_chars + recent_chars + len(USER_TEXT)

    provider = FakeProvider(text_reply("现在是 2026-09-19 10:00（周六）。"))
    result = turn(session, registry, provider, USER_TEXT)

    # ① 请求侧：模型看到的输入没超预算，且超窗的旧内容一个字都不在里面
    request = provider.requests[0]
    assert request.input_chars() <= session.budget_chars
    assert "很久以前" not in request.system_text()
    assert not any("很久以前" in (message.content or "") for message in request.messages)
    assert request.messages[-1].role == "user"
    assert request.messages[-1].content == USER_TEXT

    # ② 行为侧：裁掉的是整轮（成对），最近 8 轮按原顺序完整保留
    kept = request.messages[:-1]
    assert len(kept) == RECENT_TURNS * 2
    assert [message.role for message in kept] == ["user", "assistant"] * RECENT_TURNS
    assert kept[0].content.startswith(f"最近{OLD_TURNS + 1}：")
    assert kept[-2].content.startswith(f"最近{TOTAL_TURNS}：")

    # ③ 结果侧：trace 快照记的是"实际装配了多少轮"，不是窗口上限
    assert result.working_memory["history_turns"] == RECENT_TURNS
    assert session.working_memory()["history_turns"] == RECENT_TURNS


def test_assembly_keeps_eight_blocks_static_first_and_the_clock_last(
    settings, session, registry, turn
):
    provider = FakeProvider(text_reply("好。"))
    turn(session, registry, provider, "在吗")
    request = provider.requests[0]

    assert len(request.system) == 8  # S1~S8：空段也保留占位，trace 才对得上
    assert request.system[6] == "当前时间：2026-09-19 10:00（周六，UTC+08:00）"
    # 静态段里不许出现**格式化后的**时间戳（§4.5）：一旦时间进了前缀，缓存永不命中。
    # 注意 S1 的守则正文里会出现"当前时间"这个词，所以断言打在时间字符串上。
    static_text = "\n\n".join(request.system[index] or "" for index in (0, 1, 2, 3, 4, 5, 7))
    assert "2026-09-19 10:00" not in static_text
    assert request.system_text().endswith(request.system[6])


def test_a_small_budget_never_drops_the_last_turn(settings, conn, clock, registry, turn):
    """预算小到放不下时，底线是**至少留一轮**：宁可超预算，也不能让模型看不见刚刚说的话。"""
    session = SessionManager(settings, store=conn, session_id="cli:test", clock=clock)
    session.add_exchange("第一轮" + "旧" * 500, "回复" + "旧" * 500, [])
    session.add_exchange("第二轮" + "新" * 500, "回复" + "新" * 500, [])
    session.budget_chars = 10

    provider = FakeProvider(text_reply("好。"))
    turn(session, registry, provider, USER_TEXT)
    request = provider.requests[0]

    content = [message.content for message in request.messages]
    assert any(text.startswith("第二轮") for text in content)
    assert not any(text.startswith("第一轮") for text in content)
    assert content[-1] == USER_TEXT
