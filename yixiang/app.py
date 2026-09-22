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
from yixiang.memory import configure as configure_memory
from yixiang.memory import consolidate, core_files, gate, sync
from yixiang.ops.tracing import append_trace, build_turn_record, new_turn_id
from yixiang.ops.usage import JsonlUsageSink, UsageSink
from yixiang.providers import ChatModel, OpenAICompatibleProvider
from yixiang.rag import clear_warnings as clear_rag_warnings
from yixiang.rag import configure as configure_rag
from yixiang.rag import reset as reset_rag
from yixiang.rag import trace_info as rag_trace_info
from yixiang.runtime import eventloop
from yixiang.runtime.models import (
    Clock,
    Observer,
    SystemClock,
    TurnResult,
    assistant_message,
    user_message,
)
from yixiang.runtime.session import (
    CONTEXT_BUDGET_CHARS,
    MEMORY_RETRY_NOTICE,
    SessionManager,
)
from yixiang.tools.registry import Deps, ToolRegistry, build_registry

# 写入类记忆工具：后验校验看的就是这些（§7.9 阶段 3）
MEMORY_WRITE_TOOLS = ("save_memory", "manage_memory")


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
    # 门控/巩固用的便宜模型；不给就走主模型（N-1 决策项）
    gate_provider: ChatModel | None = None
    # 嵌入后端（默认按 settings.embed_backend 选；测试注入 HashEmbedder 走离线路径）
    embedder: Any = None
    # 语料检索的嵌入后端与记忆分开：两套协议不同（记忆 name/embed，语料 model/dim/encode）
    rag_embedder: Any = None
    memory_ready: bool = False
    rag_ready: bool = False
    startup_report: Any = None

    def __post_init__(self) -> None:
        # 记下 provider 是不是外部注入的：注入的（测试假模型）不给它发额外出站请求
        self._provider_injected = self.provider is not None
        self.conn = self.conn or db.connect(self.settings.db_path)
        db.migrate(self.conn)
        self.usage_sink = self.usage_sink or JsonlUsageSink(
            self.settings.usage_path, clock=self.clock
        )
        self.provider = self.provider or OpenAICompatibleProvider(
            self.settings, usage_sink=self.usage_sink, clock=self.clock
        )
        self._configure_memory()
        self._configure_rag()
        self.registry = self.registry or build_registry(self.settings, self.deps())
        self.session = self.session or self.new_session_manager()

    def _configure_memory(self) -> None:
        """装配记忆子系统 + 启动同步（§7.4）。任何一步失败都不该让 App 起不来。"""
        try:
            configure_memory(
                self.conn,
                data_dir=self.settings.data_dir,
                clock=self.clock,
                settings=self.settings,
                embedder=self.embedder,
            )
            core_files.ensure_memory_file(
                self.settings.data_dir, template_dir=self.settings.templates_dir
            )
            self.startup_report = sync.sync_memory_md(self.conn)
            self.memory_ready = True
        except Exception as exc:  # 记忆坏了也要能聊天（降级：三文件照读，检索缺席）
            self.memory_ready = False
            self.startup_report = f"记忆子系统装配失败：{exc}"

    def _configure_rag(self) -> None:
        """装配语料检索（PART 3 §4）。嵌入拿不到也不报错——降级纯 FTS5（D-24）。

        与 ``_configure_memory`` 相同的一条纪律：子系统的任何一步失败都不该让
        App 起不来。"宁可检索质量降级，不能让功能不可用"。
        """
        embedder = self.rag_embedder
        if embedder is None:
            try:
                from yixiang.rag.embed import build_embedder

                embedder = build_embedder(self.settings)
            except Exception:  # 后端没实现 / 依赖缺失：后面 configure 会退成无嵌入
                embedder = None
        try:
            configure_rag(
                self.conn,
                data_dir=self.settings.data_dir,
                clock=self.clock,
                settings=self.settings,
                embedder=embedder,
            )
            self.rag_ready = True
        except Exception:  # 装配本身失败：不留下半装配的全局上下文
            self.rag_ready = False
            reset_rag()

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
        registry: ToolRegistry | None = None,
    ) -> TurnResult:
        """一轮对话的完整链路：loop → 落 chat_log → 写 trace。

        ``registry`` 显式传入时用它（QQ 这类外部来源会带一份**收窄过**的注册表），
        否则用 App 自己那份。除此之外没有第二条差别——来源不该改动主链路。
        """
        active = session or self.session
        active_registry = registry or self.registry
        if active is None or active_registry is None or self.provider is None:
            raise RuntimeError("App 尚未装配完成（缺 session / registry / provider）")
        turn_id = new_turn_id(self.clock)
        active.begin_turn(text, turn_id=turn_id)
        # 入口截断后的用户消息才是"这一轮真正说的话"：门控、历史、trace 共用它（T-8）
        user_text = active.pending_user
        started = time.perf_counter()

        # 降级标记只反映"这一轮"：上一轮的 E_EMBED_UNAVAILABLE 不能一直挂着（D-24）
        clear_rag_warnings()
        decision = await self._gate(user_text, active)
        result = await run_loop(
            active, active_registry, self.provider, observer, stream=stream, tools=tools
        )
        result = await self._verify_memory_write(
            active, result, observer, stream, tools, registry=active_registry
        )
        result.turn_id = turn_id
        result.model = result.model or self.settings.model_for("main")
        result.working_memory = active.working_memory()
        result.gate = decision.as_trace()
        result.intent = {"remember": active.turn_intent == "REMEMBER", "intent": active.turn_intent}
        result.latency_ms["total"] = int((time.perf_counter() - started) * 1000)

        # 工具痕迹折叠进 assistant 历史（§5.3），再整轮落盘（§5.4 的落盘一致性）
        active.add_exchange(user_text, result.fold_into_history(), result.tool_calls)
        record = build_turn_record(
            result=result,
            session_id=active.session_id,
            source=active.source,
            user_text=user_text,
            clock=self.clock,
            gate=result.gate,
            intent=result.intent,
            rag=rag_trace_info(),
        )
        append_trace(self.settings.traces_dir, record)
        self.last_record = record
        await self._consolidate()
        return result

    # ------------------------------------------------------------------ 记忆链路
    async def _gate(self, text: str, active: SessionManager) -> gate.GateDecision:
        """§7.5 检索门控：规则优先，命中才检索；本轮只算一次。"""
        if not self.memory_ready:
            decision = gate.GateDecision(False, "", "memory_off", "rule")
        else:
            decision = await gate.should_retrieve(text, self._gate_model())
        active.prime_retrieval(decision.query or text, allowed=decision.retrieve)
        return decision

    def _gate_model(self) -> ChatModel | None:
        """门控用的模型：注入的 provider 不参与（测试里一个剧本只能演一次）。"""
        if self._provider_injected:
            return None
        if self.gate_provider is not None:
            return self.gate_provider
        if self.settings.api_key:
            self.gate_provider = OpenAICompatibleProvider(
                self.settings, usage_sink=self.usage_sink, clock=self.clock
            )
        return self.gate_provider

    async def _verify_memory_write(
        self,
        active: SessionManager,
        result: TurnResult,
        observer: Observer | None,
        stream: bool,
        tools: bool,
        *,
        registry: ToolRegistry | None = None,
    ) -> TurnResult:
        """§7.9 阶段 3：REMEMBER 轮没写记忆就纠错重试一次，仍失败则如实标记。"""
        active_registry = registry or self.registry
        if _wrote_memory(result):
            return result
        if active.turn_intent != "REMEMBER" or active_registry is None or self.provider is None:
            result.memory_write_failed = _memory_tool_failed(result)
            return result
        # 让重试看得见上一轮的回复：临时塞两条进历史，跑完再撤回
        active.history.append(assistant_message(result.reply))
        active.history.append(user_message(MEMORY_RETRY_NOTICE))
        active.extra_contract = MEMORY_RETRY_NOTICE
        try:
            retry = await run_loop(
                active, active_registry, self.provider, observer, stream=stream, tools=tools
            )
        finally:
            del active.history[-2:]
            active.extra_contract = ""
        if not _wrote_memory(retry):
            retry.memory_write_failed = True
        return _merge(result, retry)

    async def _consolidate(self) -> None:
        """§7.7 巩固：后台按阈值跑；注入的假模型不参与（避免污染剧本）。"""
        if not self.memory_ready or self._provider_injected or self.conn is None:
            return
        with contextlib.suppress(Exception):
            await consolidate.consolidate(
                self.conn,
                self.provider,
                settings=self.settings,
                clock=self.clock,
            )

    def ask(self, text: str, **kwargs: Any) -> TurnResult:
        """同步入口（终端会话 / 脚本 / 演示）：跑在**本线程常驻的 loop** 上。

        不要退回 ``asyncio.run``：它每轮新建并关掉一条 loop，provider 缓存的连接池
        会在第二轮报 ``Event loop is closed``（见 ``runtime/eventloop.py``）。
        """
        return eventloop.run(self.handle_message(text, **kwargs))

    # ------------------------------------------------------------------ 收尾
    def close(self) -> None:
        closer = getattr(self.provider, "aclose", None)
        if closer is not None:
            with contextlib.suppress(RuntimeError):  # 已在事件循环里：交给调用方自己关
                # 与对话同一条 loop：换个 loop 关不掉 provider 的连接池
                eventloop.run(closer())
        if self.conn is not None:
            self.conn.close()
            self.conn = None


def _wrote_memory(result: TurnResult) -> bool:
    """本轮是否真的写进了记忆（§7.9 阶段 3 的判据）。"""
    for event in result.tool_calls:
        if event.tool not in MEMORY_WRITE_TOOLS or not event.ok:
            continue
        if event.tool == "save_memory":
            return True
        if str(event.args.get("action", "")).strip().lower() == "update":
            return True
    return False


def _memory_tool_failed(result: TurnResult) -> bool:
    """记忆工具报错了（用户的"记住"被静默吞掉时 trace 必须看得出来）。"""
    return any(
        event.tool in MEMORY_WRITE_TOOLS and not event.ok for event in result.tool_calls
    )


def _merge(first: TurnResult, second: TurnResult) -> TurnResult:
    """把纠错重试的一轮并回原结果：回复取后者，工具痕迹与用量累加。"""
    second.turn_id = first.turn_id
    second.tool_calls = first.tool_calls + second.tool_calls
    second.iterations = first.iterations + second.iterations
    second.usage = first.usage.merge(second.usage)
    for key, value in first.latency_ms.items():
        second.latency_ms[key] = second.latency_ms.get(key, 0) + value
    return second
