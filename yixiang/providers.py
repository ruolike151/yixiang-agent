"""Provider 层：ChatModel 协议 + OpenAI-compatible 实现（TECH-DESIGN §4）。

职责边界（背下来，面试会问）：
  * **角色路由**：调用方只说 ``role="main"``，用哪个模型由这里决定（§4.2）；
  * **重试矩阵**：5xx/网络退避 0.5s/1.5s、429 退避 2s/6s、超时重试 1 次、
    4xx 与 ``content_filter`` 不重试（§4.3）；
  * **usage 记账**：每次调用写一行 ``usage.jsonl``，成本在这里算（§4.4）；
  * **流式只在主对话**：``complete_stream`` 负责分片组装；门控/巩固/judge
    一律用非流式 ``complete``（§5.4）。

``complete_stream`` 的三条边界（§5.4 的"流式复杂度在边界"）：
  1. 出现 ``tool_calls`` 分片时，先发 ``text_revoke`` 撤回已打印的预览；
  2. 连接异常且**尚未吐出文本** → 自动降级为非流式（自带完整重试矩阵）；
  3. 已经吐出文本再失败 → 立即收尾，把已输出内容作为最终回复并记 ``E_LLM_TIMEOUT``。
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

import httpx

from yixiang.config import Settings
from yixiang.errors import (
    E_LLM_AUTH,
    E_LLM_BAD_REQUEST,
    E_LLM_TIMEOUT,
    ProviderError,
)
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

# 重试矩阵（§4.3）：退避秒数，±20% 抖动（可用 jitter=0 关掉，测试里就是这么做的）
RETRY_SERVER = (0.5, 1.5)
RETRY_RATE_LIMIT = (2.0, 6.0)
RETRY_TIMEOUT = (0.5,)


@runtime_checkable
class ChatModel(Protocol):
    """内部统一契约（PART-1 §4，冻结）。"""

    async def complete(self, req: ProviderRequest) -> ModelReply: ...

    async def complete_stream(
        self, req: ProviderRequest, observer: Observer | None = None
    ) -> ModelReply: ...


@dataclass(slots=True)
class _Failure:
    code: str
    kind: str  # timeout | network | rate_limit | server | fatal
    detail: str


class _HttpFailure(Exception):
    """流式请求在拿到 4xx/5xx 时抛出，交给上层走降级路径。"""

    def __init__(self, failure: _Failure) -> None:
        super().__init__(failure.detail)
        self.failure = failure


class OpenAICompatibleProvider:
    """任何 OpenAI Chat Completions 兼容端点（DeepSeek / GLM / vLLM / Ollama）。"""

    def __init__(
        self,
        settings: Settings,
        *,
        usage_sink: UsageSink | None = None,
        clock: Clock | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        jitter: float = 0.2,
        client: httpx.AsyncClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.usage_sink: UsageSink = usage_sink or NullUsageSink()
        self.clock = clock or SystemClock()
        self._sleep = sleep or asyncio.sleep
        self._jitter = jitter
        self._client = client
        self._transport = transport
        self._owns_client = client is None

    # ------------------------------------------------------------------ 基础设施
    @property
    def endpoint(self) -> str:
        return self.settings.api_base.rstrip("/") + "/chat/completions"

    def headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.settings.api_key:
            headers["Authorization"] = f"Bearer {self.settings.api_key}"
        return headers

    def redact(self, text: str) -> str:
        """任何要进日志/trace 的字符串都过一遍：密钥永不落盘（§14.3）。"""
        key = self.settings.api_key
        if key and len(key) >= 8:
            return text.replace(key, "***")
        return text

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(transport=self._transport)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    def _backoff(self, seconds: float) -> float:
        if self._jitter <= 0:
            return seconds
        spread = seconds * self._jitter
        return max(0.0, seconds + random.uniform(-spread, spread))

    # ------------------------------------------------------------------ 请求构造
    def build_payload(self, req: ProviderRequest, *, stream: bool) -> dict[str, Any]:
        messages: list[dict[str, Any]] = []
        system_text = req.system_text()
        if system_text:
            messages.append({"role": "system", "content": system_text})
        messages.extend(message.to_openai() for message in req.messages)
        payload: dict[str, Any] = {
            "model": self.settings.model_for(req.role),
            "messages": messages,
            "temperature": req.temperature,
            "max_tokens": req.max_tokens,
            "stream": stream,
        }
        if req.tools:
            payload["tools"] = req.tools
        if stream:
            payload["stream_options"] = {"include_usage": True}
        return payload

    # ---------------------------------------------------------------- 非流式调用
    async def complete(self, req: ProviderRequest) -> ModelReply:
        payload = self.build_payload(req, stream=False)
        started = time.perf_counter()
        data = await self._send(payload, req)
        reply = self._reply_from(data, fallback_model=str(payload["model"]))
        reply.latency_ms = _ms(started)
        self._record(req, reply)
        return reply

    async def _send(self, payload: dict[str, Any], req: ProviderRequest) -> dict[str, Any]:
        budgets: dict[str, list[float]] = {
            "timeout": list(RETRY_TIMEOUT),
            "network": list(RETRY_SERVER),
            "server": list(RETRY_SERVER),
            "rate_limit": list(RETRY_RATE_LIMIT),
        }
        while True:
            failure: _Failure
            try:
                response = await self._http().post(
                    self.endpoint, json=payload, headers=self.headers(), timeout=req.timeout
                )
            except httpx.TimeoutException as exc:
                failure = _Failure(E_LLM_TIMEOUT, "timeout", f"请求超时：{exc}")
            except httpx.HTTPError as exc:
                failure = _Failure(E_LLM_TIMEOUT, "network", f"网络错误：{exc}")
            else:
                if response.status_code < 400:
                    try:
                        return response.json()
                    except ValueError as exc:
                        raise ProviderError(
                            f"响应不是合法 JSON：{exc}", code=E_LLM_BAD_REQUEST
                        ) from exc
                failure = self._classify(response)

            plan = budgets.get(failure.kind) or []
            if failure.kind == "fatal" or not plan:
                raise ProviderError(
                    failure.detail,
                    code=failure.code,
                    retryable=failure.kind != "fatal",
                )
            await self._sleep(self._backoff(plan.pop(0)))

    def _classify(self, response: httpx.Response) -> _Failure:
        status = response.status_code
        detail = self.redact(response.text[:300])
        if status in (401, 403):
            return _Failure(E_LLM_AUTH, "fatal", f"鉴权失败（{status}）：{detail}")
        if status == 429:
            return _Failure(E_LLM_TIMEOUT, "rate_limit", f"被限流（429）：{detail}")
        if status >= 500:
            return _Failure(E_LLM_TIMEOUT, "server", f"服务端错误（{status}）：{detail}")
        return _Failure(E_LLM_BAD_REQUEST, "fatal", f"请求被拒绝（{status}）：{detail}")

    def _reply_from(self, data: dict[str, Any], *, fallback_model: str) -> ModelReply:
        choices = data.get("choices") or []
        if not choices:
            raise ProviderError(f"响应里没有 choices：{self.redact(str(data)[:200])}",
                                code=E_LLM_BAD_REQUEST)
        choice = choices[0] or {}
        message = choice.get("message") or {}
        return ModelReply(
            text=message.get("content") or "",
            tool_calls=parse_tool_calls(message.get("tool_calls")),
            usage=usage_from(data.get("usage")),
            finish_reason=choice.get("finish_reason") or "stop",
            model=str(data.get("model") or fallback_model),
            raw={"id": data.get("id"), "finish_reason": choice.get("finish_reason")},
        )

    # -------------------------------------------------------------------- 流式
    async def complete_stream(
        self, req: ProviderRequest, observer: Observer | None = None
    ) -> ModelReply:
        observer = observer or NullObserver()
        payload = self.build_payload(req, stream=True)
        model = str(payload["model"])
        started = time.perf_counter()
        text_parts: list[str] = []
        tool_slots: dict[int, dict[str, str]] = {}
        finish_reason = "stop"
        usage_raw: dict[str, Any] | None = None

        try:
            async with self._http().stream(
                "POST", self.endpoint, json=payload, headers=self.headers(), timeout=req.timeout
            ) as response:
                if response.status_code >= 400:
                    await response.aread()
                    raise _HttpFailure(self._classify(response))
                async for line in response.aiter_lines():
                    line = line.strip()
                    if not line or line.startswith(":") or not line.startswith("data:"):
                        continue
                    chunk_text = line[5:].strip()
                    if not chunk_text or chunk_text == "[DONE]":
                        continue
                    try:
                        chunk = json.loads(chunk_text)
                    except ValueError:
                        continue
                    if chunk.get("usage"):
                        usage_raw = chunk["usage"]
                    if chunk.get("model"):
                        model = str(chunk["model"])
                    for choice in chunk.get("choices") or []:
                        delta = choice.get("delta") or {}
                        piece = delta.get("content")
                        if piece:
                            text_parts.append(piece)
                            observer(LoopEvent("text_delta", {"text": piece}))
                        _absorb_tool_delta(tool_slots, delta.get("tool_calls"))
                        if choice.get("finish_reason"):
                            finish_reason = choice["finish_reason"]
        except (httpx.HTTPError, _HttpFailure, ProviderError):
            if not text_parts:
                # 边界 2：还没吐字 → 降级为非流式（自带重试矩阵）
                return await self.complete(req)
            # 边界 3：已经吐了一半 → 立即收尾，别把半句话丢掉
            reply = ModelReply(
                text="".join(text_parts),
                finish_reason="timeout",
                model=model,
                error=E_LLM_TIMEOUT,
                latency_ms=_ms(started),
            )
            self._record(req, reply)
            return reply

        if tool_slots:
            # 边界 1：这一轮其实是工具轮，撤回已经打印的预览
            if text_parts:
                observer(
                    LoopEvent("text_revoke", {"chars": sum(len(p) for p in text_parts)})
                )
            tool_calls = [
                ToolCall(
                    id=slot["id"] or f"call_{index}",
                    name=slot["name"],
                    arguments=loads_arguments(slot["args"]),
                )
                for index, slot in sorted(tool_slots.items())
            ]
        else:
            tool_calls = []

        reply = ModelReply(
            text="".join(text_parts),
            tool_calls=tool_calls,
            usage=usage_from(usage_raw),
            finish_reason=finish_reason,
            model=model,
            latency_ms=_ms(started),
        )
        self._record(req, reply)
        return reply

    # ------------------------------------------------------------------ 记账
    def _record(self, req: ProviderRequest, reply: ModelReply) -> None:
        try:
            self.usage_sink.record(
                role=req.role,
                model=reply.model,
                usage=reply.usage,
                latency_ms=reply.latency_ms,
                turn_id=req.turn_id,
            )
        except Exception:  # 记账失败不能影响对话本身
            return


# ---------------------------------------------------------------------- 工具函数
def _ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _absorb_tool_delta(
    slots: dict[int, dict[str, str]], deltas: list[dict[str, Any]] | None
) -> None:
    """把流式 ``tool_calls`` 分片按 index 拼装（这是最容易写错的一段）。"""
    for raw in deltas or []:
        slot = slots.setdefault(int(raw.get("index", 0)), {"id": "", "name": "", "args": ""})
        if raw.get("id"):
            slot["id"] = str(raw["id"])
        function = raw.get("function") or {}
        if function.get("name"):
            slot["name"] += str(function["name"])
        if function.get("arguments"):
            slot["args"] += str(function["arguments"])


def loads_arguments(raw: str | None) -> dict[str, Any]:
    """参数是坏 JSON 时返回 ``{}``：让 schema 校验给出可行动的错误。"""
    raw = (raw or "").strip()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def parse_tool_calls(raw_calls: list[dict[str, Any]] | None) -> list[ToolCall]:
    calls: list[ToolCall] = []
    for index, raw in enumerate(raw_calls or []):
        function = raw.get("function") or {}
        calls.append(
            ToolCall(
                id=str(raw.get("id") or f"call_{index}"),
                name=str(function.get("name") or ""),
                arguments=loads_arguments(function.get("arguments")),
            )
        )
    return calls


def usage_from(raw: dict[str, Any] | None) -> Usage:
    """兼容 OpenAI（``prompt_tokens_details.cached_tokens``）与 DeepSeek
    （``prompt_cache_hit_tokens``）两种缓存字段。"""
    raw = raw or {}
    details = raw.get("prompt_tokens_details") or {}
    cached = int(details.get("cached_tokens") or raw.get("prompt_cache_hit_tokens") or 0)
    return Usage(
        input_tokens=int(raw.get("prompt_tokens") or 0),
        output_tokens=int(raw.get("completion_tokens") or 0),
        cached_input_tokens=cached,
    )
