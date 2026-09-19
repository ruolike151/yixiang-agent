"""D-22 路径逃逸：参数含 ``..\\..\\`` 或绝对路径 → 拒绝执行，错误码写入 trace。

§9.4 的分层防御里，这是**第 1 层**：任何声明了 ``path_args`` 的工具，参数先过
``safe_path()``——解析到 ``data/`` 之外就拒绝，工具函数根本不会被调用。

用例刻意分三处断言，因为这三处会各自被改坏：
  ① 工具层：``safe_path`` 拒绝（``ok=False`` / ``E_TOOL_FAILED`` / ``ms=0``）；
  ② loop 层：拒绝变成可行动的文本回喂模型，用户看到的是"操作失败了"；
  ③ trace 层：错误码进 ``tool_calls[].error``，但工具原文不落盘（§11.1）。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from fake_provider import FakeProvider, text_reply, tool_round

from yixiang.errors import E_TOOL_FAILED
from yixiang.ops.tracing import build_turn_record
from yixiang.runtime.session import SessionManager
from yixiang.tools.registry import Tool, ToolRegistry

USER_TEXT = "帮我读一下那份笔记"


def reader_registry(data_dir, executed: list[str]) -> ToolRegistry:
    """一个读文件的工具：``path_args`` 是逃逸校验的开关，data/ 是它的沙箱根。"""

    def read_note(path: str) -> str:
        executed.append(path)
        return f"读了 {path}"

    return ToolRegistry(
        [
            Tool(
                name="read_note",
                description="读 data/ 下的一个文件；path 必须是相对 data/ 的相对路径",
                input_schema={
                    "type": "object",
                    "properties": {"path": {"type": "string", "description": "相对路径"}},
                    "required": ["path"],
                },
                fn=read_note,
                side_effect=False,
                path_args=("path",),
            )
        ],
        data_dir=data_dir,
    )


def assert_refused(outcome) -> None:
    assert outcome.ok is False
    assert outcome.output.startswith("Error")
    assert "路径越界" in outcome.output
    assert outcome.error_code == E_TOOL_FAILED
    assert outcome.ms == 0  # 失败路径不测量耗时（注册表的契约）


def test_d22_parent_traversal_is_refused(settings):
    executed: list[str] = []
    registry = reader_registry(settings.data_dir, executed)

    assert_refused(registry.run("read_note", {"path": "../../secret.txt"}))
    assert_refused(registry.run("read_note", {"path": "notes/../../secret.txt"}))
    assert executed == []  # 工具函数一次都没被调用


@pytest.mark.skipif(os.name != "nt", reason="反斜杠是 Windows 的路径分隔符")
def test_d22_backslash_traversal_is_refused_on_windows(settings):
    executed: list[str] = []
    registry = reader_registry(settings.data_dir, executed)

    assert_refused(registry.run("read_note", {"path": "..\\..\\secret.txt"}))
    assert executed == []


def test_d22_absolute_path_is_refused_even_when_it_exists(settings, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    executed: list[str] = []
    registry = reader_registry(settings.data_dir, executed)

    assert_refused(registry.run("read_note", {"path": str(outside)}))
    assert executed == []


def test_a_relative_path_inside_data_is_allowed(settings):
    executed: list[str] = []
    registry = reader_registry(settings.data_dir, executed)
    outcome = registry.run("read_note", {"path": "notes/a.txt"})

    assert outcome.ok is True
    assert executed and Path(executed[0]).is_relative_to(settings.data_dir.resolve())


def test_d22_error_code_reaches_the_trace(settings, conn, clock, turn):
    executed: list[str] = []
    registry = reader_registry(settings.data_dir, executed)
    session = SessionManager(settings, store=conn, session_id="cli:test", clock=clock)
    provider = FakeProvider(
        tool_round(("read_note", {"path": "../../secret.txt"})),
        text_reply("我没法读 data/ 之外的文件。"),
    )
    result = turn(session, registry, provider, USER_TEXT)

    # ② loop 层：拒绝可行动，用户看到的是人话
    assert executed == []
    assert result.error == E_TOOL_FAILED
    assert "这个操作失败了" in result.reply
    assert "路径越界" in result.reply

    # ③ trace 层：错误码进 tool_calls[].error，工具原文不落盘
    record = build_turn_record(
        result=result,
        session_id=session.session_id,
        source="cli",
        user_text=USER_TEXT,
        clock=clock,
    )
    call = record["tool_calls"][0]
    assert (call["tool"], call["ok"], call["error"]) == ("read_note", False, E_TOOL_FAILED)
    assert "output" not in call
    assert call["args"] == {"path": "../../secret.txt"}
