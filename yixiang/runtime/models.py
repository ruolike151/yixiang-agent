"""内部数据结构：Message / ModelReply / ProviderRequest / LoopEvent / TurnResult。

这些是 PART 2/3/4 都要消费的冻结契约（PART-1 §4），改动即破坏性变更：
  * 工具返回值永远是 ``str``；
  * 时间一律 ISO8601 带时区；展示用本地时间，比较用 UTC；
  * 系统段（system blocks）保持"静态在前、动态在后"，否则前缀缓存永远不命中（§4.5）。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, runtime_checkable

# 工具结果长度上限（§5.5 冻结约定）
TOOL_RESULT_LIMIT = 2000


# --------------------------------------------------------------------- 时钟
@runtime_checkable
class Clock(Protocol):
    """时间必须可注入：测试里一律用假时钟，禁止 sleep（§13.6）。"""

    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now().astimezone()


@dataclass
class FixedClock:
    """测试用假时钟：``advance()`` 手动推进，行为完全确定。"""

    current: datetime

    def now(self) -> datetime:
        return self.current

    def advance(self, **delta: float) -> datetime:
        self.current = self.current + timedelta(**delta)
        return self.current


def to_local_iso(dt: datetime) -> str:
    """ISO8601 带时区（本地）。naive 时间按本地时区补齐。"""
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.isoformat(timespec="seconds")


def iso_minute(dt: datetime) -> str:
    """精确到分钟的时间串——给 prompt 用，避免秒级抖动击穿前缀缓存（§4.5）。"""
    return dt.strftime("%Y-%m-%d %H:%M %Z").strip()


def utc_now() -> datetime:
    return datetime.now(UTC)


# ----------------------------------------------------------------- 消息模型
@dataclass(slots=True)
class ToolCall:
    """模型请求的一次工具调用。``arguments`` 已是 dict（JSON 解析失败时为 {}）。"""

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)

    def key(self) -> tuple[str, str]:
        """用于重复调用检测的稳定键：工具名 + 排序后的参数 JSON。"""
        import json

        return self.name, json.dumps(self.arguments, ensure_ascii=False, sort_keys=True, default=str)


@dataclass(slots=True)
class Message:
    role: str
    content: str = ""
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None

    def to_openai(self) -> dict[str, Any]:
        """出站格式（OpenAI Chat Completions）。Provider 是唯一的适配点。"""
        payload: dict[str, Any] = {"role": self.role}
        if self.role == "assistant" and self.tool_calls:
            payload["content"] = self.content or None
            payload["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": _json_dumps(call.arguments),
                    },
                }
                for call in self.tool_calls
            ]
            return payload
        if self.role == "tool":
            payload["tool_call_id"] = self.tool_call_id
        payload["content"] = self.content
        return payload


def _json_dumps(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, default=str)


def system_message(text: str) -> Message:
    return Message(role="system", content=text)


def user_message(text: str) -> Message:
    return Message(role="user", content=text)


def assistant_message(text: str = "", tool_calls: list[ToolCall] | None = None) -> Message:
    return Message(role="assistant", content=text, tool_calls=tool_calls or None)


def tool_message(tool_call_id: str, content: str) -> Message:
    return Message(role="tool", content=content, tool_call_id=tool_call_id)


# ------------------------------------------------------------------- 请求/回复
@dataclass(slots=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0

    def merge(self, other: Usage) -> Usage:
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cached_input_tokens=self.cached_input_tokens + other.cached_input_tokens,
        )

    def as_dict(self) -> dict[str, int]:
        return {
            "in": self.input_tokens,
            "cached_in": self.cached_input_tokens,
            "out": self.output_tokens,
        }


@dataclass(slots=True)
class ProviderRequest:
    """一次模型调用请求。``system`` 是**分段列表**而不是一整块字符串，
    为的是前缀缓存与差异化裁剪（§4.1、§6.1）。"""

    role: str
    system: list[str] = field(default_factory=list)
    messages: list[Message] = field(default_factory=list)
    tools: list[dict[str, Any]] | None = None
    temperature: float = 0.0
    max_tokens: int = 2048
    timeout: float = 60.0
    stream: bool = False
    # 记账与排障用：usage.jsonl 的 turn_id 关联（§4.4）
    turn_id: str = ""
    session_id: str = ""

    def system_text(self) -> str:
        return "\n\n".join(block for block in self.system if block)

    def input_chars(self) -> int:
        """粗略字符数（测试断言"模型看到了什么"用，不是 token 口径）。"""
        return len(self.system_text()) + sum(len(m.content or "") for m in self.messages)


@dataclass(slots=True)
class ModelReply:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    finish_reason: str = "stop"
    model: str = ""
    latency_ms: int = 0
    raw: dict[str, Any] = field(default_factory=dict)
    # 出错时填错误码（如 E_LLM_TIMEOUT）；正常回复为 None
    error: str | None = None

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


# ------------------------------------------------------------------- 事件/结果
@dataclass(slots=True)
class LoopEvent:
    """流式事件。CLI / QQ（P2）共用同一个 observer 接口（§5.4）。

    kind 取值：
      ``text_delta``  增量文本（已确认是回复轮的，可直接打印）
      ``text_revoke`` 撤回已经吐出的预览文本（这一轮其实是工具轮）
      ``tool_start``  工具开始执行
      ``tool_end``    工具执行结束（含耗时与成功标记）
      ``iteration``   进入第 N 轮迭代
      ``notice``      给用户的提示（如"正在查询…"、截断告知）
      ``done``        本轮结束
    """

    kind: str
    data: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class Observer(Protocol):
    def __call__(self, event: LoopEvent) -> None: ...


class NullObserver:
    """默认 observer：丢弃所有事件（评测 / 后台任务用）。"""

    def __call__(self, event: LoopEvent) -> None:  # noqa: ARG002 - 协议签名
        return None


def collecting_observer(sink: list[LoopEvent]) -> Observer:
    """把事件收集进列表，供测试断言"用户看到了什么"。"""

    def _observe(event: LoopEvent) -> None:
        sink.append(event)

    return _observe


@dataclass(slots=True)
class ToolEvent:
    """一次工具调用的 trace 记录（§11.1 的 tool_calls[] 元素）。

    ``error`` 是失败原因的错误码（§11.3），成功时为 ``None``——
    D-22（路径逃逸）要求"错误码写入 trace"，就是靠这个字段。
    """

    iter: int
    tool: str
    args: dict[str, Any]
    output: str
    ok: bool
    ms: int
    error: str | None = None

    def as_trace(self) -> dict[str, Any]:
        record = {
            "iter": self.iter,
            "tool": self.tool,
            "args": self.args,
            "ok": self.ok,
            "ms": self.ms,
        }
        if self.error:
            record["error"] = self.error
        return record


@dataclass(slots=True)
class TurnResult:
    turn_id: str = ""
    reply: str = ""
    tool_calls: list[ToolEvent] = field(default_factory=list)
    iterations: int = 1
    usage: Usage = field(default_factory=Usage)
    finish_reason: str = "stop"
    model: str = ""
    error: str | None = None
    error_detail: str | None = None
    latency_ms: dict[str, int] = field(default_factory=dict)
    gate: dict[str, Any] | None = None
    working_memory: dict[str, Any] | None = None
    memory_write_failed: bool = False
    intent: dict[str, Any] | None = None

    def tools_used_summary(self) -> str:
        """折叠成一行 ``[tools used: create_plan(3 项), add_task(×3)]``（§5.3）。"""
        if not self.tool_calls:
            return ""
        counts: dict[str, int] = {}
        for event in self.tool_calls:
            counts[event.tool] = counts.get(event.tool, 0) + 1
        parts = [f"{name}(×{n})" if n > 1 else name for name, n in counts.items()]
        summary = "[tools used: " + ", ".join(parts) + "]"
        if len(summary) > 200:
            summary = "[tools used: " + ", ".join(counts) + "]"
        return summary

    def fold_into_history(self) -> str:
        """写回历史时的 assistant 文本（正文 + 工具痕迹）。"""
        summary = self.tools_used_summary()
        if not summary:
            return self.reply
        return f"{self.reply}\n\n{summary}".strip()


def iterations_of(events: Iterable[LoopEvent]) -> int:
    """从事件流里取最大迭代号（测试断言用）。"""
    return max((int(e.data.get("iteration", 0)) for e in events), default=0)
