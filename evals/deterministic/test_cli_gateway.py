"""CLI 网关：斜杠命令、流式渲染、trace 与成本账（PART-1 §1「对话可用 / 可观测」、TECH §10.1）。

网关只做协议转换（ADR-3），所以这一组用例既不碰网络也不碰真模型：假 Provider 出
剧本，断言落在三处——终端上用户看到了什么、trace 里记了什么、``usage.jsonl`` 里
记了几行。这样 `/trace` / `/cost` 这两条演示路径不会"只有手工点过才知道对不对"。
"""

from __future__ import annotations

import io
import json
from pathlib import Path

from fake_provider import FakeProvider, text_reply, tool_round

from yixiang.app import App
from yixiang.gateway.cli import ChatCLI
from yixiang.ops.usage import JsonlUsageSink

USER_TEXT = "帮我排一个两周的 RAG 复习计划"


def make_cli(settings, clock, *script, stream: bool = True, session_id: str = "cli:test"):
    sink = JsonlUsageSink(settings.usage_path, clock=clock)
    provider = FakeProvider(*script, usage_sink=sink, clock=clock, stream_pieces=4)
    app = App.from_settings(settings, provider=provider, usage_sink=sink, clock=clock)
    out = io.StringIO()
    return ChatCLI(settings, app=app, out=out, stream=stream, session_id=session_id), out


def test_slash_commands_never_touch_the_model(settings, clock):
    cli, out = make_cli(settings, clock)  # 剧本是空的：一旦调模型就会炸

    assert cli.handle_line("/help") is True
    assert "/trace" in out.getvalue()

    assert cli.handle_line("/tools") is True
    assert "已注册工具（7）" in out.getvalue()
    assert "create_plan" in out.getvalue()

    assert cli.handle_line("/cost") is True
    assert "成本：¥0.0000" in out.getvalue()  # 还没花过钱

    assert cli.handle_line("/nope") is True
    assert "未知命令 /nope" in out.getvalue()

    assert cli.handle_line("/exit") is False
    assert "再见" in out.getvalue()


def test_new_and_history_switch_sessions(settings, clock):
    cli, out = make_cli(settings, clock)

    cli.handle_line("/new 复习")
    assert "-复习" in out.getvalue()
    assert cli.app.session.session_id.startswith("cli:")

    cli.handle_line("/history")
    assert "还没有历史会话" in out.getvalue()  # 空库：如实说"没有"，不编造


def test_a_streamed_tool_turn_renders_events_and_writes_trace_and_usage(settings, clock):
    script = (
        tool_round(
            ("create_plan", {"title": "两周 RAG 复习", "start_date": "2026-09-19"}),
            ("add_task", {"plan_id": 1, "date": "2026-09-19", "content": "读 RAG 论文"}),
        ),
        text_reply("排好了：今天先读 RAG 论文，明天做检索评测。"),
    )
    cli, out = make_cli(settings, clock, *script)

    cli.handle_line(USER_TEXT)
    rendered = out.getvalue()

    # ① 终端：工具轮显示"正在查询 / 调用 X / 结果标记"，正文照常打印
    assert "正在查询…" in rendered
    assert "调用 create_plan" in rendered
    assert "create_plan ✓" in rendered
    assert "排好了：今天先读 RAG 论文" in rendered
    assert "本轮" in rendered  # 每轮末尾的 trace 摘要框

    # ② trace：一行一条，工具调用与 turn_id 都记上了
    trace_path = settings.traces_dir / "2026-09-19.jsonl"
    records = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
    assert len(records) == 1
    record = records[0]
    assert record["turn_id"] == cli.app.last_record["turn_id"]
    assert [call["tool"] for call in record["tool_calls"]] == ["create_plan", "add_task"]
    assert record["finish_reason"] == "stop"
    assert record["working_memory"]["history_turns"] == 0  # 首轮：历史里还没有东西

    # ③ 成本账：两次模型调用两行，角色是 main
    lines = [
        json.loads(line)
        for line in settings.usage_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(lines) == 2
    assert {line["role"] for line in lines} == {"main"}
    assert {line["turn_id"] for line in lines} == {record["turn_id"]}

    # ④ /trace 与 /cost 读的是刚落盘的这两份文件
    cli.handle_line("/trace")
    cli.handle_line("/cost")
    rendered = out.getvalue()
    assert rendered.count("本轮") == 2  # 轮末一次 + /trace 一次
    assert "iter: 2" in rendered
    assert "2 次调用" in rendered


def test_history_is_folded_into_the_next_turn(settings, clock):
    """折叠契约（§5.3）：工具痕迹进 assistant 历史，完整工具结果不进。"""
    cli, _ = make_cli(
        settings,
        clock,
        tool_round(("create_plan", {"title": "两周 RAG 复习"})),
        text_reply("计划建好了。"),
        text_reply("今天只有一项：读 RAG 论文。"),
    )

    cli.handle_line(USER_TEXT)
    cli.handle_line("今天要做什么")

    provider = cli.app.provider
    # 第一轮用了两次模型调用（工具轮 + 收尾轮），第二轮从 index=2 开始
    second_request = provider.requests[2]
    history = [message.content or "" for message in second_request.messages]
    assert any("[tools used: create_plan]" in text for text in history)
    assert not any("plan_id" in text for text in history)  # 工具原文不留在历史里
    assert history[-1] == "今天要做什么"


def test_non_stream_mode_still_prints_the_reply(settings, clock):
    cli, out = make_cli(settings, clock, text_reply("好。"), stream=False)

    cli.handle_line("在吗")

    assert "好。" in out.getvalue()
    assert Path(settings.db_path).is_file()  # 交互过程用的是文件库，不是内存库
