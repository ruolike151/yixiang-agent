"""假 Provider + 假 HTTP 传输（PART-1 §5 第 7 条、TECH §13.2）。

它不是"以后补"的测试替身，而是第一周的**交付物**：没有它，Agent 行为就不能
可复现地测试，后面三部分的评测（judge / 回归）全都建不起来。

四件套：

  * ``FakeProvider``       按剧本回放 ``ModelReply``（能注入超时 / 异常）；
  * ``scripted_transport`` 按顺序回放状态码 / JSON / 异常——测重试矩阵用；
  * ``SseBody``            手写的假 SSE 分片——测"假流"组装用；
  * ``SleepRecorder``      替换 ``asyncio.sleep``，退避时长只记录不等待。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from math import ceil
from typing import Any

import httpx

from yixiang.ops.usage import NullUsageSink, UsageSink
from yixiang.runtime.models import (
    Clock,
    LoopEvent,
    ModelReply,
    NullObserver,
    Observer,
    ProviderRequest,
    SystemClock,
    ToolCall,
    Usage,
)


# --------------------------------------------------------------------- 剧本构件
def text_reply(text: str, **overrides: Any) -> ModelReply:
    """一轮就结束的回复（无工具调用）。"""
    values: dict[str, Any] = {"text": text, "finish_reason": "stop"}
    values.update(overrides)
    return ModelReply(**values)


def tool_call(name: str, arguments: dict[str, Any] | None = None, *, call_id: str = "") -> ToolCall:
    return ToolCall(id=call_id or f"call_{name}", name=name, arguments=dict(arguments or {}))


def tool_round(*calls: Any, text: str = "", usage: Usage | None = None) -> ModelReply:
    """一轮工具调用，例如 ``tool_round(("add_memo", {"content": "x"}), ("list_today", {}))``。"""
    parsed: list[ToolCall] = []
    for index, item in enumerate(calls):
        if isinstance(item, ToolCall):
            parsed.append(item)
            continue
        name, arguments = item
        parsed.append(tool_call(name, arguments, call_id=f"call_{index + 1}"))
    return ModelReply(
        text=text,
        tool_calls=parsed,
        usage=usage or Usage(),
        finish_reason="tool_calls",
    )


def usage(input_tokens: int, output_tokens: int, *, cached: int = 0) -> Usage:
    return Usage(input_tokens=input_tokens, output_tokens=output_tokens, cached_input_tokens=cached)


# ------------------------------------------------------------------ FakeProvider
class FakeProvider:
    """按剧本回放的假 Provider：``complete`` 与 ``complete_stream`` 共用一份剧本。

    剧本元素可以是 ``ModelReply``，也可以是**异常实例**——后者用来注入"模型
    调用直接失败"（超时 / 鉴权失败），验证错误码这条链路。
    """

    def __init__(
        self,
        *script: ModelReply | Exception,
        stream_pieces: int = 0,
        latency_ms: int = 12,
        usage_sink: UsageSink | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.script: list[ModelReply | Exception] = list(script)
        self.requests: list[ProviderRequest] = []
        self.events: list[LoopEvent] = []
        self.stream_pieces = stream_pieces  # >0 时把回复切成分片吐出来（假流）
        self.latency_ms = latency_ms
        self.usage_sink: UsageSink = usage_sink or NullUsageSink()
        self.clock = clock or SystemClock()

    def __len__(self) -> int:
        return len(self.script)

    def remaining(self) -> list[ModelReply | Exception]:
        return list(self.script)

    async def complete(self, req: ProviderRequest) -> ModelReply:
        reply = self._next(req)
        self._record(req, reply)
        return reply

    async def complete_stream(
        self, req: ProviderRequest, observer: Observer | None = None
    ) -> ModelReply:
        observe = observer or NullObserver()
        reply = self._next(req)
        for event in self._stream_events(reply):
            self.events.append(event)
            observe(event)
        self._record(req, reply)
        return reply

    async def aclose(self) -> None:
        return None

    # ------------------------------------------------------------------ 内部
    def _next(self, req: ProviderRequest) -> ModelReply:
        self.requests.append(req)
        if not self.script:
            raise AssertionError(f"FakeProvider 剧本用完了：这是第 {len(self.requests)} 次调用")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        item.latency_ms = self.latency_ms
        if not item.model:
            item.model = "deepseek-flash"
        return item

    def _stream_events(self, reply: ModelReply) -> list[LoopEvent]:
        events: list[LoopEvent] = []
        if reply.text and self.stream_pieces > 0:
            size = max(1, ceil(len(reply.text) / self.stream_pieces))
            events.extend(
                LoopEvent("text_delta", {"text": reply.text[index : index + size]})
                for index in range(0, len(reply.text), size)
            )
        if reply.tool_calls and reply.text:
            # 边界 1：这一轮其实是工具轮 → 撤回已经打出的预览（§5.4）
            events.append(LoopEvent("text_revoke", {"chars": len(reply.text)}))
        return events

    def _record(self, req: ProviderRequest, reply: ModelReply) -> None:
        try:
            self.usage_sink.record(
                role=req.role,
                model=reply.model,
                usage=reply.usage,
                latency_ms=reply.latency_ms,
                turn_id=req.turn_id,
            )
        except Exception:  # 记账失败不能影响对话本身（与真 Provider 同一口径）
            return


# ------------------------------------------------------------------ 假流（SSE）
@dataclass
class SseBody:
    """一段假 SSE 响应体：``payloads`` 是逐个 ``data:`` 行的内容（JSON 字符串）。"""

    payloads: list[str]
    status: int = 200
    fail_after: int | None = None  # 成功吐出 N 个分片后抛异常（0 = 一个字都没吐就断）
    error: type[httpx.HTTPError] = httpx.ReadTimeout


class _SseStream(httpx.AsyncByteStream):
    def __init__(self, body: SseBody) -> None:
        self.body = body

    async def __aiter__(self):
        served = 0
        for payload in self.body.payloads:
            if self.body.fail_after is not None and served >= self.body.fail_after:
                raise self.body.error("假流：连接断了")
            served += 1
            yield f"data: {payload}\n\n".encode()
        if self.body.fail_after is not None and served >= self.body.fail_after:
            raise self.body.error("假流：连接断了")
        yield b"data: [DONE]\n\n"

    async def aclose(self) -> None:
        return None


def stream_body(*payloads: dict[str, Any], fail_after: int | None = None) -> SseBody:
    """把若干 delta 拼成假 SSE 响应体（``[DONE]`` 自动补）。"""
    return SseBody(
        payloads=[json.dumps(payload, ensure_ascii=False) for payload in payloads],
        fail_after=fail_after,
    )


def delta_text(text: str, *, model: str = "") -> dict[str, Any]:
    payload: dict[str, Any] = {"choices": [{"delta": {"content": text}}]}
    if model:
        payload["model"] = model
    return payload


def delta_tool(index: int, *, call_id: str = "", name: str = "", arguments: str = "") -> dict[str, Any]:
    function: dict[str, Any] = {}
    if name:
        function["name"] = name
    if arguments:
        function["arguments"] = arguments
    call: dict[str, Any] = {"index": index}
    if call_id:
        call["id"] = call_id
    if function:
        call["function"] = function
    return {"choices": [{"delta": {"tool_calls": [call]}}]}


def delta_finish(reason: str = "stop", *, usage_raw: dict[str, Any] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"choices": [{"delta": {}, "finish_reason": reason}]}
    if usage_raw:
        payload["usage"] = usage_raw
    return payload


# ------------------------------------------------------------------ 传输回放
def scripted_transport(*steps: Any) -> tuple[httpx.MockTransport, list[dict[str, Any]]]:
    """按顺序回放请求。``steps`` 元素可以是：

      * ``dict``      → 200 + 该 JSON 响应体
      * ``int``       → 该 HTTP 状态码（响应体一行字）
      * ``Exception`` → 直接抛出（测网络错误 / 超时）
      * ``SseBody``   → text/event-stream 假流

    返回 ``(transport, bodies)``；``bodies`` 是每次请求的 JSON 载荷，
    用来断言**请求侧**（"模型看到了什么"）。
    """
    queue = list(steps)
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        raw = request.content.decode("utf-8") if request.content else ""
        bodies.append(json.loads(raw) if raw else {})
        if not queue:
            raise AssertionError(f"HTTP 剧本用完了：这是第 {len(bodies)} 次请求")
        step = queue.pop(0)
        if isinstance(step, Exception):
            raise step
        if isinstance(step, SseBody):
            headers = {"content-type": "text/event-stream"}
            return httpx.Response(step.status, headers=headers, stream=_SseStream(step))
        if isinstance(step, int):
            return httpx.Response(step, text=f"status {step}")
        return httpx.Response(200, json=step)

    return httpx.MockTransport(handler), bodies


def completion_body(
    text: str = "好的",
    *,
    model: str = "deepseek-flash",
    usage_raw: dict[str, Any] | None = None,
    finish_reason: str = "stop",
    tool_calls: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """非流式响应的最小合法体（OpenAI Chat Completions 兼容）。"""
    message: dict[str, Any] = {"role": "assistant", "content": text}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "id": "chatcmpl-test",
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        "usage": usage_raw or {"prompt_tokens": 10, "completion_tokens": 5},
    }


class SleepRecorder:
    """替换 ``asyncio.sleep``：退避时长只记录，绝不让用例真的等（§13.6）。"""

    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(round(seconds, 6))


# ------------------------------------------------------------------ 同步跑法
async def _close(provider: Any) -> None:
    closer = getattr(provider, "aclose", None)
    if closer is not None:
        await closer()


def run_complete(provider: Any, req: ProviderRequest) -> ModelReply:
    """一次非流式调用的同步包装（顺手把 HTTP 客户端关掉，免得留资源告警）。"""

    async def _run() -> ModelReply:
        try:
            return await provider.complete(req)
        finally:
            await _close(provider)

    return asyncio.run(_run())


def run_stream(
    provider: Any, req: ProviderRequest, observer: Observer | None = None
) -> ModelReply:
    """一次流式调用的同步包装。"""

    async def _run() -> ModelReply:
        try:
            return await provider.complete_stream(req, observer)
        finally:
            await _close(provider)

    return asyncio.run(_run())
