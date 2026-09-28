"""组装根：Settings → DB → Provider → Tools → Loop → Trace（TECH §1.1、§10）。

只有一个地方把零件接起来，所以"谁依赖谁"是能一眼看全的：CLI / QQ / 评测
都只调用 ``App.handle_message()``（ADR-3：入口只搬文本，不含业务逻辑）。
"""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from yixiang import db, plan_doc
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
from yixiang.runtime.eventloop import CancelToken
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
    PLAN_QUERY_RETRY_NOTICE,
    PLAN_RETRY_NOTICE,
    RESCHEDULE_RETRY_NOTICE,
    SessionManager,
)
from yixiang.tools.registry import Deps, ToolRegistry, build_registry

# 写入类记忆工具：后验校验看的就是这些（§7.9 阶段 3）
MEMORY_WRITE_TOOLS = ("save_memory", "manage_memory")

# 改期工具：说得再像"挪好了"，没有这两个之一成功返回就等于没挪
RESCHEDULE_TOOLS = ("reschedule_task", "reschedule_memo")

# 排期写工具：要"排计划"就必须有其中之一真的落库（§7.9 阶段 3 的同一护栏）
PLAN_WRITE_TOOLS = ("create_plan", "add_task")
# 排期查工具：问"这周有什么"必须先过其中之一，否则就是凭印象背清单
PLAN_QUERY_TOOLS = ("list_range", "list_today", "daily_brief")

# 「停止生成」写进 chat_log 的回复占位：停止是用户主动行为，不是错误，但这一轮
# 必须留下痕迹——否则用户按了停止就像这句话从没说过（§5.4 的落盘一致性）
CANCELLED_REPLY = "（已停止生成）"


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
        self._configure_plan_doc()
        self._configure_rag()
        self.registry = self.registry or build_registry(self.settings, self.deps())
        self.session = self.session or self.new_session_manager()

    def _configure_plan_doc(self) -> None:
        """启动同步 ``your_plan.md``（§7.3）：先收用户手改，再按库重渲染。

        与记忆子系统同一条纪律——文档同步坏了不该让 App 起不来，所以整体吞异常。
        失败只影响这一份「用户可见视图」，``plans``/``plan_items`` 仍是权威源。
        """
        with contextlib.suppress(Exception):
            plan_doc.sync_plan_doc(
                self.conn, self.settings.data_dir, now=self.clock.now()
            )

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
        images: list[str] | None = None,
    ) -> TurnResult:
        """一轮对话的完整链路：loop → 落 chat_log → 写 trace。

        ``registry`` 显式传入时用它（QQ 这类外部来源会带一份**收窄过**的注册表），
        否则用 App 自己那份。除此之外没有第二条差别——来源不该改动主链路。

        ``images`` 是本轮附图（``data/`` 下的相对路径，Web 控制台上传的那张图）。
        它随这一轮的 user 消息发出去，不进历史。
        """
        active = session or self.session
        active_registry = registry or self.registry
        if active is None or active_registry is None or self.provider is None:
            raise RuntimeError("App 尚未装配完成（缺 session / registry / provider）")
        turn_id = new_turn_id(self.clock)
        active.begin_turn(text, turn_id=turn_id, images=images)
        # 入口截断后的用户消息才是"这一轮真正说的话"：门控、历史、trace 共用它（T-8）
        user_text = active.pending_user
        started = time.perf_counter()

        # 降级标记只反映"这一轮"：上一轮的 E_EMBED_UNAVAILABLE 不能一直挂着（D-24）
        clear_rag_warnings()
        decision: gate.GateDecision | None = None
        cancelled = False
        try:
            decision = await self._gate(user_text, active)
            result = await run_loop(
                active, active_registry, self.provider, observer, stream=stream, tools=tools
            )
            result = await self._verify_memory_write(
                active, result, observer, stream, tools, registry=active_registry
            )
            result = await self._verify_reschedule(
                active, result, observer, stream, tools, registry=active_registry
            )
            result = await self._verify_plan(
                active, result, observer, stream, tools, registry=active_registry
            )
            result = await self._verify_plan_query(
                active, result, observer, stream, tools, registry=active_registry
            )
        except asyncio.CancelledError:
            # 「停止生成」：不再往下跑，但这一轮**必须留痕**（chat_log 与 trace 都要有）
            # 在这里吞掉 CancelledError 是安全的：上面就是 run_until_complete 的边界
            # （runtime/eventloop.run），没有别人 await 这个 task；反过来让它冒出去，
            # 会从 _SerialRunner._loop 带走整条工作线程，此后所有请求都堵死。
            cancelled = True
            result = TurnResult(
                reply=CANCELLED_REPLY,
                finish_reason="cancelled",
                error="cancelled",
                error_detail="用户停止了这一轮",
            )
        result.turn_id = turn_id
        result.model = result.model or self.settings.model_for("main")
        result.working_memory = active.working_memory()
        result.gate = decision.as_trace() if decision else None
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
        if not cancelled:  # 用户刚说"停"，就别接着花时间跑巩固了
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
        retry = await self._retry_intent_turn(
            active,
            result,
            observer,
            stream,
            tools,
            intent="REMEMBER",
            landed=_wrote_memory,
            notice=MEMORY_RETRY_NOTICE,
            registry=registry,
        )
        if retry is None:
            result.memory_write_failed = _memory_tool_failed(result)
            return result
        if not _wrote_memory(retry):
            retry.memory_write_failed = True
        return _merge(result, retry)

    async def _verify_reschedule(
        self,
        active: SessionManager,
        result: TurnResult,
        observer: Observer | None,
        stream: bool,
        tools: bool,
        *,
        registry: ToolRegistry | None = None,
    ) -> TurnResult:
        """改期轮的后验校验：说了挪却没真调工具，就纠错重试一次。

        与"记住"轮同一条护栏（``_verify_memory_write``）。两者的意图互斥
        （``detect_intent`` 只返回一个），所以一轮里最多只会重试一次。
        """
        retry = await self._retry_intent_turn(
            active,
            result,
            observer,
            stream,
            tools,
            intent="RESCHEDULE",
            landed=_did_reschedule,
            notice=RESCHEDULE_RETRY_NOTICE,
            registry=registry,
        )
        return result if retry is None else _merge(result, retry)

    async def _verify_plan(
        self,
        active: SessionManager,
        result: TurnResult,
        observer: Observer | None,
        stream: bool,
        tools: bool,
        *,
        registry: ToolRegistry | None = None,
    ) -> TurnResult:
        """排期轮的后验校验：说了"排好了"却没落库，就纠错重试一次。

        判据只看有没有 ``create_plan`` / ``add_task`` 成功返回——有的轮只建计划
        骨架（比如"排一个两周的复习计划"），不能强求它同时写满任务。
        """
        retry = await self._retry_intent_turn(
            active,
            result,
            observer,
            stream,
            tools,
            intent="PLAN",
            landed=_did_plan,
            notice=PLAN_RETRY_NOTICE,
            registry=registry,
        )
        return result if retry is None else _merge(result, retry)

    async def _verify_plan_query(
        self,
        active: SessionManager,
        result: TurnResult,
        observer: Observer | None,
        stream: bool,
        tools: bool,
        *,
        registry: ToolRegistry | None = None,
    ) -> TurnResult:
        """排期查询轮的后验校验：问"这周有什么"必须先过库，不能凭印象背。

        与排期写入轮同一条护栏。意图互斥（``detect_intent`` 只返回一个），所以
        一轮里最多只会重试一次。
        """
        retry = await self._retry_intent_turn(
            active,
            result,
            observer,
            stream,
            tools,
            intent="PLAN_QUERY",
            landed=_did_plan_query,
            notice=PLAN_QUERY_RETRY_NOTICE,
            registry=registry,
        )
        return result if retry is None else _merge(result, retry)

    async def _retry_intent_turn(
        self,
        active: SessionManager,
        result: TurnResult,
        observer: Observer | None,
        stream: bool,
        tools: bool,
        *,
        intent: str,
        landed: Callable[[TurnResult], bool],
        notice: str,
        registry: ToolRegistry | None = None,
    ) -> TurnResult | None:
        """``intent`` 轮没落库就带着提醒重跑一遍 loop；返回 ``None`` 表示不需重试。"""
        active_registry = registry or self.registry
        if landed(result) or active.turn_intent != intent:
            return None
        if active_registry is None or self.provider is None:
            return None
        # 让重试看得见上一轮的回复：临时塞两条进历史，跑完再撤回
        active.history.append(assistant_message(result.reply))
        active.history.append(user_message(notice))
        active.extra_contract = notice
        try:
            return await run_loop(
                active, active_registry, self.provider, observer, stream=stream, tools=tools
            )
        finally:
            del active.history[-2:]
            active.extra_contract = ""

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

    def ask(
        self, text: str, *, token: CancelToken | None = None, **kwargs: Any
    ) -> TurnResult:
        """同步入口（终端会话 / 脚本 / 演示）：跑在**本线程常驻的 loop** 上。

        ``token`` 给 Web 控制台的「停止生成」用：取消由请求线程发起，落在本线程这条
        loop 上那个 task 上。不要退回 ``asyncio.run``（见 ``runtime/eventloop.py``）。
        """
        return eventloop.run(self.handle_message(text, **kwargs), token=token)

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


def _did_reschedule(result: TurnResult) -> bool:
    """本轮是否真的把某条改期落到了库里（改期轮后验校验的判据）。"""
    return any(event.tool in RESCHEDULE_TOOLS and event.ok for event in result.tool_calls)


def _did_plan(result: TurnResult) -> bool:
    """本轮是否真的把排期落到了库里（排期轮后验校验的判据）。

    只说话不落库、或者只把清单写进聊天回复，都算没排——``create_plan`` 建骨架
    也算数，因为有的任务本来就只排到"计划"这一层。
    """
    return any(event.tool in PLAN_WRITE_TOOLS and event.ok for event in result.tool_calls)


def _did_plan_query(result: TurnResult) -> bool:
    """本轮问排期时是否真查过库（排期查询轮后验校验的判据）。"""
    return any(event.tool in PLAN_QUERY_TOOLS and event.ok for event in result.tool_calls)


def _merge(first: TurnResult, second: TurnResult) -> TurnResult:
    """把纠错重试的一轮并回原结果：回复取后者，工具痕迹与用量累加。"""
    second.turn_id = first.turn_id
    second.tool_calls = first.tool_calls + second.tool_calls
    second.iterations = first.iterations + second.iterations
    second.usage = first.usage.merge(second.usage)
    for key, value in first.latency_ms.items():
        second.latency_ms[key] = second.latency_ms.get(key, 0) + value
    return second
