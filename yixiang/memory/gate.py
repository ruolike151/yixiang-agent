"""检索门控：先判断"要不要翻记忆"，再检索（TECH §7.5、ADR-4）。

门控的两种错误**代价不对称**：

  * 误检（不该检索却检索了）只是多注入几条记忆、多花一点 token；
  * 漏检（该检索却跳过）会让用户直接觉得"它失忆了"。

所以这里的一切容错都朝"宁可多检索"偏：解析失败、超时、限流 → **fail-open**，
返回 ``retrieve=True``，并把 ``E_GATE_FAIL_OPEN`` 记进 trace 供统计。

规则预过滤只做**确定性**判定（§7.5.2）：寒暄白名单直接跳过，明确指向记忆的词
直接检索。模糊判断一律交模型——规则越界比不写规则更糟。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

from yixiang.errors import E_GATE_FAIL_OPEN
from yixiang.providers import ChatModel
from yixiang.runtime.models import ProviderRequest, user_message

# 门控 prompt 里的 JSON 花括号必须转义（走 ``.format()`` 注入用户消息）
GATE_PROMPT = """你是个人助手长期记忆的检索门控。判断回答下面这条用户消息是否需要用户的历史记忆
（关于人物、项目、偏好、过往事件的事实）。

只输出这个 JSON，不要任何其他内容：
{{"retrieve": true/false, "query": "<需要检索时给搜索关键词，否则空串>", "reason": "<5 个字以内>"}}

通用知识、数学、寒暄、自包含的请求 → false
提到用户的生活、人物、计划、历史、偏好 → true

用户消息：{message}"""

# 寒暄白名单：去空白后 ≤6 字且命中就跳过（§7.5.2）
SKIP_WORDS = ("你好", "在吗", "谢谢", "嗯", "好的", "哈哈", "晚安")
SKIP_MAX_CHARS = 6
# 明确指向记忆的词：命中就直接检索，省一次模型调用
RETRIEVE_WORDS = ("记住", "帮我记住", "别忘了", "你还记得", "我说过", "上周", "我的偏好")

# 带思考链的模型会先吐思考块再给 JSON，100 token 会把答案截掉（§7.5.1）
GATE_MAX_TOKENS = 600
DEFAULT_GATE_TIMEOUT = 8.0

Source = Literal["rule", "model", "fail_open"]


@dataclass(slots=True)
class GateDecision:
    """一次门控的判定结果；``source`` 说明这个结论是谁给的。"""

    retrieve: bool
    query: str
    reason: str
    source: Source

    def as_trace(self) -> dict[str, Any]:
        """trace 里的 ``gate`` 字段（§11.1）。fail-open 会带错误码。"""
        record: dict[str, Any] = {
            "retrieve": self.retrieve,
            "query": self.query,
            "reason": self.reason,
            "source": self.source,
        }
        if self.source == "fail_open":
            record["error"] = E_GATE_FAIL_OPEN
        return record


def rule_decision(message: str) -> GateDecision | None:
    """规则预过滤：能确定性判定就返回结论，否则 ``None``（交模型）。"""
    stripped = "".join((message or "").split())
    if not stripped:
        return GateDecision(False, "", "空消息", "rule")
    if len(stripped) <= SKIP_MAX_CHARS and any(word in stripped for word in SKIP_WORDS):
        return GateDecision(False, "", "寒暄", "rule")
    for word in RETRIEVE_WORDS:
        if word in message:
            return GateDecision(True, message, "规则命中", "rule")
    return None


def build_prompt(message: str) -> str:
    return GATE_PROMPT.format(message=message)


def parse_decision(text: str) -> GateDecision | None:
    """解析模型回复：取**第一个 ``{`` 到最后一个 ``}``** 再 ``json.loads``。

    模型偶尔会加前后缀（思考块、``json 围栏、客套话），直接 ``json.loads`` 会失手；
    没有 ``{`` 说明"模型没给可用答案"——那是 fail-open，不是"不需要检索"。
    """
    if not text:
        return None
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict) or "retrieve" not in data:
        return None
    retrieve = bool(data.get("retrieve"))
    query = str(data.get("query") or "").strip()
    reason = str(data.get("reason") or "").strip()[:20]
    if not retrieve:
        query = ""
    if not reason:
        reason = "模型判定"
    return GateDecision(retrieve, query, reason, "model")


def fail_open(message: str) -> GateDecision:
    """门控自身失败：当作"需要检索"，并把原消息当查询词。"""
    return GateDecision(True, message, "门控失败", "fail_open")


async def should_retrieve(message: str, provider: ChatModel | None) -> GateDecision:
    """冻结契约（PART-2 §4）：规则优先，其余交模型，任何异常都 fail-open。"""
    decision = rule_decision(message)
    if decision is not None:
        return decision
    if provider is None:
        return fail_open(message)

    settings = getattr(provider, "settings", None)
    timeout = float(getattr(settings, "gate_timeout", DEFAULT_GATE_TIMEOUT) or DEFAULT_GATE_TIMEOUT)
    request = ProviderRequest(
        role="gate",
        messages=[user_message(build_prompt(message))],
        max_tokens=GATE_MAX_TOKENS,
        temperature=0.0,
        timeout=timeout,
        stream=False,
    )
    try:
        reply = await provider.complete(request)
    except Exception:  # 超时 / 限流 / 鉴权失败：一律 fail-open（§7.5.1）
        return fail_open(message)
    return parse_decision(getattr(reply, "text", "") or "") or fail_open(message)


__all__ = [
    "DEFAULT_GATE_TIMEOUT",
    "GATE_MAX_TOKENS",
    "GATE_PROMPT",
    "RETRIEVE_WORDS",
    "SKIP_MAX_CHARS",
    "SKIP_WORDS",
    "GateDecision",
    "build_prompt",
    "fail_open",
    "parse_decision",
    "rule_decision",
    "should_retrieve",
]
