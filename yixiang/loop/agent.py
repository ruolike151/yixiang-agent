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
from typing import Any

from yixiang.errors import E_LLM_TRUNCATED, E_TOOL_FAILED, ProviderError, user_message
from yixiang.loop.guard import Guard
from yixiang.providers import ChatModel
from yixiang.runtime.models import (
    LoopEvent,
    Message,
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
    # 名单里已经关掉思考的模型（``YIXIANG_NO_THINK_MODELS``）：它们撞线就是真的写太长，
    # 再"关一次思考"重问没有意义（参数一个字节都不会变），只会白花一次调用。
    thinking_already_off = settings.thinking_disabled_for(settings.model_for("main"))

    events: list[ToolEvent] = []
    usage = Usage()
    started = time.perf_counter()
    llm_ms = tools_ms = 0
    reply, model, finish_reason = "", "", "stop"
    error: str | None = None
    detail: str | None = None
    retried_without_thinking = False

    for iteration in range(1, limit + 1):
        observe(LoopEvent("iteration", {"iteration": iteration}))
        request = _main_request(session, messages, schemas, stream=stream)
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
                # 撞上输出上限（``max_tokens``）。**先别急着告诉用户"被截断了"**：
                # 第一嫌疑是会思考的模型把 ``reasoning`` 也算进同一个预算——2026-09-27
                # 直连探针实测：同一句 prompt，10000 个 token 全花在思考上、正文一个字
                # 都没吐出来。所以这里自动关掉思考重问一次，第二轮正常收尾就静默换成
                # 完整答案（用户只看到草稿被撤回、答案重说了一遍）。
                #
                # 重问只做一次（``retried_without_thinking``）+ 名单里已关思考的不做
                # （``thinking_already_off``）：撞两次还接着重问，就是拿用户的钱硬顶。
                if not thinking_already_off and not retried_without_thinking:
                    retried_without_thinking = True
                    retry = await _ask_without_thinking(
                        provider,
                        _main_request(session, messages, schemas, stream=stream, no_think=True),
                        observe,
                        stream=stream,
                        printed=len(reply) if stream else 0,
                    )
                else:
                    retry = None
                if retry is not None:
                    llm_ms += retry.latency_ms
                    usage = usage.merge(retry.usage)
                    model = retry.model or model
                    if retry.text.strip() and not retry.error and not retry.wants_tools:
                        # 重说的那一版更长更完整，就发它（第一版已经撤回了）
                        reply, finish_reason = retry.text, retry.finish_reason or "stop"
                        if finish_reason != "length":
                            break
                # 关思考之后仍然撞线：**已经写出来的部分照发**，把"被截断了"接在末尾。
                # 两条理由：① 流式早就把这半篇推给用户了，收尾时凭空收回等于"我看见了
                # 又没了"——2026-09-27 实测到的现象；② 与下面工具失败那条分支同一个口径：
                # 原因拼在答案后面，而不是用一句文案顶掉整条答案。
                error = E_LLM_TRUNCATED
                notice = user_message(E_LLM_TRUNCATED)
                reply = f"{reply.strip()}\n\n{notice}" if reply.strip() else notice
                detail = (
                    f"输出达到上限（max_tokens={request.max_tokens}）"
                    + ("，已关掉思考重问一次" if retried_without_thinking and retry else "")
                )
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


def _main_request(
    session: SessionManager,
    messages: list[Message],
    schemas: list[dict[str, Any]] | None,
    *,
    stream: bool,
    no_think: bool = False,
) -> ProviderRequest:
    """主对话的请求（上限、超时、记账字段都在这里对齐 §3 / §4.1）。"""
    settings = session.settings
    return ProviderRequest(
        role="main",
        system=session.system_blocks(),
        messages=messages,
        tools=schemas,
        temperature=0.0,
        timeout=settings.llm_timeout,
        max_tokens=settings.max_tokens,
        stream=stream,
        thinking_disabled=no_think,
        turn_id=session.turn_id,
        session_id=session.session_id,
    )


async def _ask_without_thinking(
    provider: ChatModel,
    request: ProviderRequest,
    observe: Observer,
    *,
    stream: bool,
    printed: int,
) -> ModelReply | None:
    """撞线后的重问：关掉思考再问一遍同一个问题。

    ``printed`` > 0 说明第一轮那半篇已经流式推给用户了 → 先发 ``text_revoke`` 撤回它，
    不然用户看到的是两版答案首尾相接。重问本身失败（网络 / 鉴权）返回 ``None``：
    第一轮那半篇仍然有价值，不能跟着陪葬。
    """
    if printed:
        observe(LoopEvent("text_revoke", {"chars": printed, "reason": "truncated"}))
    try:
        return await _ask(provider, request, observe, stream=stream)
    except ProviderError:
        return None


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
