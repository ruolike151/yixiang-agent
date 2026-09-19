"""组装根：Settings → DB → Provider → Tools → Loop → Trace（TECH §1.1、§10）。

只有一个地方把零件接起来，所以"谁依赖谁"是能一眼看全的：CLI / QQ / 评测
都只调用 ``App.handle_message()``（ADR-3：入口只搬文本，不含业务逻辑）。
"""

from __future__ import annotations

import contextlib
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any

from yixiang import db
from yixiang.config import Settings
from yixiang.loop.agent import run_loop
from yixiang.ops.tracing import append_trace, build_turn_record, new_turn_id
from yixiang.ops.usage import JsonlUsageSink, UsageSink
from yixiang.providers import ChatModel, OpenAICompatibleProvider
from yixiang.runtime.models import Clock, Observer, SystemClock, TurnResult
from yixiang.runtime.session import CONTEXT_BUDGET_CHARS, SessionManager
from yixiang.tools.registry import Deps, ToolRegistry, build_registry


@dataclass
class App:
    """一个进程一个 App：共享连接、Provider、注册表与会话。"""

    settings: Settings
    clock: Clock = field(default_factory=SystemClock)
    conn: sqlite3.Connection | None = None
    provider: ChatModel | None = None
    usage_sink: UsageSink | None = None
    registry: ToolRegistry | None = None
    session: SessionManager | None = None
    last_record: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        self.conn = self.conn or db.connect(self.settings.db_path)
        db.migrate(self.conn)
        self.usage_sink = self.usage_sink or JsonlUsageSink(
            self.settings.usage_path, clock=self.clock
        )
        self.provider = self.provider or OpenAICompatibleProvider(
            self.settings, usage_sink=self.usage_sink, clock=self.clock
        )
        self.registry = self.registry or build_registry(self.settings, self.deps())
        self.session = self.session or self.new_session_manager()

    # ------------------------------------------------------------------ 装配
    @classmethod
    def from_settings(cls, settings: Settings, **overrides: Any) -> App:
        return cls(settings=settings, **overrides)

    def deps(self) -> Deps:
        return Deps(
            conn=self.conn,
            clock=self.clock,
            data_dir=self.settings.data_dir,
            source=self.session.source if self.session else "cli",
        )

    def new_session_manager(
        self,
        session_id: str = "cli:default",
        *,
        source: str = "cli",
        budget_chars: int = CONTEXT_BUDGET_CHARS,
    ) -> SessionManager:
        return SessionManager(
            self.settings,
            store=self.conn,
            session_id=session_id,
            source=source,
            clock=self.clock,
            budget_chars=budget_chars,
        )

    def switch_session(self, session_id: str, *, source: str | None = None) -> SessionManager:
        self.session = self.new_session_manager(session_id, source=source or "cli")
        return self.session

    # ------------------------------------------------------------------ 主链路
    async def handle_message(
        self,
        text: str,
        *,
        session: SessionManager | None = None,
        observer: Observer | None = None,
        stream: bool = True,
        tools: bool = True,
    ) -> TurnResult:
        """一轮对话的完整链路：loop → 落 chat_log → 写 trace。"""
        active = session or self.session
        if active is None or self.registry is None or self.provider is None:
            raise RuntimeError("App 尚未装配完成（缺 session / registry / provider）")
        turn_id = new_turn_id(self.clock)
        active.begin_turn(text, turn_id=turn_id)
        started = time.perf_counter()

        result = await run_loop(
            active, self.registry, self.provider, observer, stream=stream, tools=tools
        )
        result.turn_id = turn_id
        result.model = result.model or self.settings.model_for("main")
        result.working_memory = active.working_memory()
        result.latency_ms["total"] = int((time.perf_counter() - started) * 1000)

        # 工具痕迹折叠进 assistant 历史（§5.3），再整轮落盘（§5.4 的落盘一致性）
        active.add_exchange(text, result.fold_into_history(), result.tool_calls)
        record = build_turn_record(
            result=result,
            session_id=active.session_id,
            source=active.source,
            user_text=text,
            clock=self.clock,
        )
        append_trace(self.settings.traces_dir, record)
        self.last_record = record
        return result

    def ask(self, text: str, **kwargs: Any) -> TurnResult:
        """同步入口（脚本 / 演示用）：内部起一个事件循环跑完一轮。"""
        import asyncio

        return asyncio.run(self.handle_message(text, **kwargs))

    # ------------------------------------------------------------------ 收尾
    def close(self) -> None:
        closer = getattr(self.provider, "aclose", None)
        if closer is not None:
            import asyncio

            with contextlib.suppress(RuntimeError):  # 已在事件循环里：交给调用方自己关
                asyncio.run(closer())
        if self.conn is not None:
            self.conn.close()
            self.conn = None
