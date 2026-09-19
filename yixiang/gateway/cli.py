"""CLI：REPL、斜杠命令、流式渲染（TECH §10.1）。

Gateway 只做协议转换与文本搬运（ADR-3）：这里没有业务逻辑，
真正干活的是 ``App.handle_message()``。

流式的两条体验规则：
  * 主对话逐字打印（TTFT 就是用户感知到的延迟，§15.3）；
  * 这一轮其实是工具轮时，Provider 会发 ``text_revoke``——把已经打出的
    草稿作废，改成"正在查询…"，用户看到的是"它在动手"而不是半句话。
"""

from __future__ import annotations

import sys
from typing import TextIO

from yixiang.app import App
from yixiang.config import Settings
from yixiang.ops.show_trace import render_trace_list, render_turn_box
from yixiang.ops.usage import summarize, summary_text
from yixiang.runtime.models import LoopEvent, TurnResult

HELP = """斜杠命令：
  /help          显示这份帮助
  /new [名字]    开新会话（生成 cli:20260919-1530 形式的 id）
  /history [n]   列出历史会话；带编号则切换过去
  /tools [关键词] 列出已注册工具
  /trace [n]     不带参数=上一轮详情；带 n=最近 n 轮列表
  /cost [month]  今日（或本月）token 与成本
  /exit          退出（Ctrl+C 同效）
"""


class CliObserver:
    """把 LoopEvent 渲染到终端（QQ 将来复用同一个事件流）。"""

    def __init__(self, out: TextIO) -> None:
        self.out = out
        self.printed = False
        self.revoked = False

    def __call__(self, event: LoopEvent) -> None:
        match event.kind:
            case "text_delta":
                self.out.write(str(event.data.get("text", "")))
                self.out.flush()
                self.printed = True
            case "text_revoke":
                self.revoked = True
                self.out.write("\n（这轮要查资料，撤回上面的草稿）\n")
                self.out.flush()
            case "notice":
                self.out.write(f"  · {event.data.get('text', '')}\n")
                self.out.flush()
            case "tool_start":
                self.out.write(f"  · 调用 {event.data.get('tool')} …\n")
                self.out.flush()
            case "tool_end":
                mark = "✓" if event.data.get("ok") else "✗"
                self.out.write(
                    f"  · {event.data.get('tool')} {mark} {event.data.get('ms', 0)}ms\n"
                )
                self.out.flush()
            case _:
                return


class ChatCLI:
    """交互式 REPL。``handle_line`` 与输入解耦，便于用脚本驱动（demo 用）。"""

    def __init__(
        self,
        settings: Settings,
        *,
        app: App | None = None,
        out: TextIO | None = None,
        stream: bool = True,
        session_id: str = "cli:default",
    ) -> None:
        self.settings = settings
        self.out = out or sys.stdout
        self.stream = stream
        self.app = app or App.from_settings(settings)
        self.app.switch_session(session_id)
        self._sessions: list[dict[str, object]] = []

    # ------------------------------------------------------------------ 主循环
    def run(self) -> int:
        self._write(
            f"yixiang（以湘）已就绪 · {self.settings.describe()}\n"
            f"当前会话 {self.app.session.session_id} · 输入 /help 看命令，/exit 退出\n"
        )
        if not self.settings.api_key:
            self._write("⚠️  还没配 API key（YIXIANG_API_KEY），现在只能试 /help /tools /cost。\n")
        while True:
            try:
                line = input("\nyou > ").strip()
            except (EOFError, KeyboardInterrupt):
                self._write("\n再见。\n")
                return 0
            if not self.handle_line(line):
                return 0

    def handle_line(self, line: str) -> bool:
        """处理一行输入；返回 ``False`` 表示要退出。"""
        text = (line or "").strip()
        if not text:
            return True
        if text.startswith("/"):
            return self._command(text)
        self._turn(text)
        return True

    # ------------------------------------------------------------------ 一轮
    def _turn(self, text: str) -> TurnResult:
        observer = CliObserver(self.out)
        self._write("yixiang > ")
        if not self.stream:
            result = self.app.ask(text, observer=observer, stream=False)
            self._write(f"{result.reply}\n")
        else:
            result = self.app.ask(text, observer=observer, stream=True)
            if observer.printed and not observer.revoked:
                self._write("\n")
            elif not observer.printed:
                self._write(f"{result.reply}\n")
        if self.app.last_record:
            self._write(render_turn_box(self.app.last_record) + "\n")
        return result

    # ------------------------------------------------------------------ 命令
    def _command(self, text: str) -> bool:
        command, _, argument = text.partition(" ")
        argument = argument.strip()
        match command:
            case "/exit" | "/quit":
                self._write("再见。\n")
                return False
            case "/help" | "/?":
                self._write(HELP)
            case "/new":
                session = self.app.session
                session.new_session(argument or None)
                self._write(f"新会话：{session.session_id}\n")
            case "/history":
                self._history(argument)
            case "/tools":
                self._tools(argument)
            case "/trace":
                self._trace(argument)
            case "/cost":
                period = "month" if argument.startswith("month") else "day"
                summary = summarize(self.settings.usage_path, period=period)
                self._write(
                    summary_text(summary, budget_cny_per_day=self.settings.budget_cny_per_day)
                    + "\n"
                )
            case _:
                self._write(f"未知命令 {command}，输入 /help 看可用命令。\n")
        return True

    def _history(self, argument: str) -> None:
        if argument.isdigit():
            index = int(argument) - 1
            if 0 <= index < len(self._sessions):
                target = str(self._sessions[index]["session_id"])
                self.app.switch_session(target)
                self._write(f"已切到 {target}\n")
                return
            self._write("序号超出范围，先 /history 看一下列表。\n")
            return
        self._sessions = self.app.session.list_sessions()
        if not self._sessions:
            self._write("（还没有历史会话：聊一句就有了）\n")
            return
        current = self.app.session.session_id
        lines = ["历史会话（/history <序号> 切换）："]
        for index, item in enumerate(self._sessions, start=1):
            mark = "*" if item["session_id"] == current else " "
            lines.append(
                f"{mark} {index}. {item['session_id']} · {item['turns']} 轮 · "
                f"{item['last_at']} · {item['title']}"
            )
        self._write("\n".join(lines) + "\n")

    def _tools(self, argument: str) -> None:
        registry = self.app.registry
        tools = registry.find(argument) if registry else []
        if not tools:
            self._write("（没有匹配的工具）\n")
            return
        lines = [f"已注册工具（{len(tools)}）："]
        lines.extend("  " + tool.signature() for tool in tools)
        self._write("\n".join(lines) + "\n")

    def _trace(self, argument: str) -> None:
        traces_dir = self.settings.traces_dir
        count = int(argument) if argument.isdigit() else 0
        if count > 0:
            from yixiang.ops.tracing import read_traces

            self._write(render_trace_list(read_traces(traces_dir, limit=count)) + "\n")
            return
        if self.app.last_record:
            self._write(render_turn_box(self.app.last_record) + "\n")
            return
        from yixiang.ops.tracing import read_traces

        self._write(render_trace_list(read_traces(traces_dir, limit=1)) + "\n")

    # ------------------------------------------------------------------ 输出
    def _write(self, text: str) -> None:
        self.out.write(text)
        self.out.flush()
