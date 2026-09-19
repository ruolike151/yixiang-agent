"""Agent Loop：reason → act → observe（TECH §5，核心链路 <100 行）。

刻意不引框架（ADR-1）：整个 Agent 就是下面这个 for 循环 + 三条防护。
面试被问"为什么不用 LangGraph"时，答案在这一屏里——循环本体短到可以现场白板重写，
真正花心思的是**边界**：错误码、截断、重复调用、上下文增长（§5.5）。

流式的三条边界由 Provider 负责（§5.4），loop 只做一件事：
把 observer 一路透传下去，让 CLI / QQ / 测试看到同一串事件。
"""

from __future__ import annotations

import json
import re
import time

from yixiang.errors import E_LLM_TRUNCATED, E_TOOL_FAILED, ProviderError, user_message
from yixiang.loop.guard import Guard
from yixiang.providers import ChatModel
from yixiang.runtime.models import (
    LoopEvent,
    ModelReply,
    NullObserver,
    Observer,
    ProviderRequest,
    ToolEvent,
    TurnResult,
    Usage,
    assistant_message,
    tool_message,
)
from yixiang.runtime.session import SessionManager
from yixiang.tools.registry import ToolRegistry

ITER_LIMIT_NOTICE = (
    "这轮我到迭代上限了，还没做完（loop_max_iter）。"
    "建议把这件事拆小一点，我按小步接着做。"
)
TOOL_ROUND_NOTICE = "正在查询…"


async def run_loop(
    session: SessionManager,
    registry: ToolRegistry,
    provider: ChatModel,
    observer: Observer | None = None,
    *,
    stream: bool = True,
    tools: bool = True,
    max_iter: int | None = None,
) -> TurnResult:
    """跑完一轮对话。返回的 ``TurnResult`` 就是 trace / chat_log 的事实来源。"""
    observe = observer or NullObserver()
    settings = session.settings
    limit = max_iter or settings.loop_max_iter
    guard = Guard(max_iter=limit, tool_retry_max=settings.tool_retry_max)
    messages = session.assemble()
    schemas = registry.schemas() if tools else None

    events: list[ToolEvent] = []
    usage = Usage()
    started = time.perf_counter()
    llm_ms = tools_ms = 0
    reply, model, finish_reason = "", "", "stop"
    error: str | None = None
    detail: str | None = None

    for iteration in range(1, limit + 1):
        observe(LoopEvent("iteration", {"iteration": iteration}))
        request = ProviderRequest(
            role="main",
            system=session.system_blocks(),
            messages=messages,
            tools=schemas,
            temperature=0.0,
            timeout=settings.llm_timeout,
            stream=stream,
            turn_id=session.turn_id,
            session_id=session.session_id,
        )
        try:
            answer = await _ask(provider, request, observe, stream=stream)
        except ProviderError as exc:
            error, detail, finish_reason = exc.code, str(exc), "error"
            reply = user_message(exc.code)
            break

        llm_ms += answer.latency_ms
        usage = usage.merge(answer.usage)
        model = answer.model or model

        if not answer.wants_tools:
            reply, finish_reason = answer.text, answer.finish_reason or "stop"
            if answer.error:
                error, detail = answer.error, "流式回复中断，已把已输出内容作为最终回复"
            elif finish_reason == "length":
                error, finish_reason = E_LLM_TRUNCATED, "length"
                reply = user_message(E_LLM_TRUNCATED)
            break

        messages.append(assistant_message(answer.text, answer.tool_calls))
        observe(LoopEvent("notice", {"text": TOOL_ROUND_NOTICE}))
        for call in answer.tool_calls:
            hit = guard.observe_call(call)
            if hit and hit.stop:
                reply, finish_reason = hit.message, "guard_stop"
                break
            if hit:
                # 重复调用：不执行、不计失败，只把纠错文本喂回模型
                output, ok, code, ms = hit.message, False, E_TOOL_FAILED, 0
            else:
                observe(LoopEvent("tool_start", {"tool": call.name, "args": call.arguments}))
                outcome = registry.run(call.name, call.arguments)
                output, ok, code, ms = (
                    outcome.output,
                    outcome.ok,
                    outcome.error_code,
                    outcome.ms,
                )
                if ok:
                    guard.observe_success(call.name)
                else:
                    output = guard.observe_failure(call.name) or output
            tools_ms += ms
            event = ToolEvent(
                iter=iteration,
                tool=call.name,
                args=call.arguments,
                output=output,
                ok=ok,
                ms=ms,
                error=code,
            )
            events.append(event)
            observe(LoopEvent("tool_end", {"tool": call.name, "ok": ok, "ms": ms}))
            messages.append(tool_message(call.id, output))
        if finish_reason == "guard_stop":
            break
    else:
        reply, finish_reason = ITER_LIMIT_NOTICE, "iter_limit"

    if any(not event.ok for event in events):
        error = error or E_TOOL_FAILED
        reason = _first_failure(events)
        detail = detail or reason
        notice = user_message(E_TOOL_FAILED, detail=reason)
        reply = f"{reply}\n\n{notice}" if reply else notice
    if not reply:
        reply, finish_reason = ITER_LIMIT_NOTICE, finish_reason or "iter_limit"

    result = TurnResult(
        turn_id=session.turn_id,
        reply=reply,
        tool_calls=events,
        iterations=iteration,
        usage=usage,
        finish_reason=finish_reason,
        model=model,
        error=error,
        error_detail=detail,
        latency_ms={
            "llm": llm_ms,
            "tools": tools_ms,
            "total": int((time.perf_counter() - started) * 1000),
        },
        working_memory=session.working_memory(),
    )
    observe(LoopEvent("done", {"turn_id": result.turn_id, "iterations": iteration}))
    return result


async def _ask(
    provider: ChatModel, request: ProviderRequest, observe: Observer, *, stream: bool
) -> ModelReply:
    """主对话走流式，评测 / 调试可走非流式（§5.4）。"""
    if stream:
        return await provider.complete_stream(request, observe)
    return await provider.complete(request)


def _first_failure(events: list[ToolEvent]) -> str:
    """给用户看的失败原因：从结构化错误里取 hint，截到 80 字。"""
    for event in events:
        if event.ok:
            continue
        return f"{event.tool}：{_failure_reason(event.output)}"
    return "未知原因"


def _failure_reason(output: str) -> str:
    """给用户看的失败原因（D-19）。

    两条来源都要能读：工具抛异常时是 ``Error running <tool>: <msg>``（去掉前缀只留
    msg），schema 校验失败时是 ``Error: {"error":...,"hint":...}``（取 hint）。
    两条路径都截到 80 字——不把整段响应体糊进用户回复（§14.3 输出管控）。
    """
    text = (output or "").strip()
    running = re.match(r"Error running [^:]*:\s*(.*)", text, re.DOTALL)
    if running:
        text = running.group(1).strip()
    elif text.startswith("Error:"):
        payload = text[len("Error:") :].strip()
        try:
            data = json.loads(payload)
        except (TypeError, ValueError):
            text = payload
        else:
            field_name = data.get("field")
            hint = str(data.get("hint") or data.get("error") or "")
            text = f"{field_name}：{hint}" if field_name else hint
    return text.replace("\n", " ").strip()[:80]
