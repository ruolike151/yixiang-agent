"""循环防护：迭代上限、重复调用检测、工具失败计数（TECH §5.1）。

三条防护各防一类事故，面试要能分别说清：

  1. **迭代上限**——模型陷入"调工具→看不懂结果→再调"的死循环，烧钱不停；
  2. **重复调用检测**——模型在同一轮里把完全相同的调用又发一遍（KeyError 之后
     的典型行为），执行它只会重复写库（"重复订两次日历"）；
  3. **工具失败计数**——某个工具坏了（文件不存在、网络不通），模型反复重试，
     必须划线放弃并**要求它如实报告失败**，不许编造成功。

第 3 次完全相同的调用不是"再拒绝一次"，而是**直接打断本轮**（D-20）：
拒绝两次之后模型还在原地打转，再给机会只是继续烧 token。
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from yixiang.runtime.models import ToolCall

DUPLICATE_LIMIT = 3

DUPLICATE_NOTICE = (
    "Error: 你刚刚已经调用过完全相同的工具与参数，结果不会变。"
    "请换一种方式，或者直接用已有信息回答用户。"
)
DUPLICATE_STOP = (
    "检测到连续 3 次完全相同的工具调用，本轮已停下。"
    "（流程卡在同一个工具上了，建议换个说法或把任务拆小。）"
)
GIVE_UP = (
    "Error: {tool} 连续失败 {n} 次，已放弃调用。"
    "请如实告知用户这个操作失败了，不要编造成功。"
)


@dataclass(slots=True)
class GuardHit:
    """一次拦截：``stop=True`` 表示要立刻结束本轮。"""

    message: str
    stop: bool = False


class Guard:
    """本轮的三本账（重复计数 / 失败计数 / 迭代上限）。"""

    def __init__(
        self,
        *,
        max_iter: int = 8,
        tool_retry_max: int = 2,
        duplicate_limit: int = DUPLICATE_LIMIT,
    ) -> None:
        self.max_iter = max_iter
        self.tool_retry_max = tool_retry_max
        self.duplicate_limit = duplicate_limit
        self._calls: Counter[tuple[str, str]] = Counter()
        self._failures: Counter[str] = Counter()
        self.stopped_by: str | None = None

    # ------------------------------------------------------------------ 调用
    def observe_call(self, call: ToolCall) -> GuardHit | None:
        """第一次放行，第二次起拒绝；第 N 次直接打断（§5.1）。"""
        key = call.key()
        self._calls[key] += 1
        count = self._calls[key]
        if count >= self.duplicate_limit:
            self.stopped_by = "duplicate_call"
            return GuardHit(DUPLICATE_STOP, stop=True)
        if count >= 2:
            return GuardHit(DUPLICATE_NOTICE)
        return None

    # ------------------------------------------------------------------ 失败
    def observe_failure(self, tool: str) -> str | None:
        """同一工具连续失败超限 → 返回"放弃"话术（要求模型如实报告）。"""
        self._failures[tool] += 1
        if self._failures[tool] > self.tool_retry_max:
            return GIVE_UP.format(tool=tool, n=self.tool_retry_max)
        return None

    def observe_success(self, tool: str) -> None:
        """成功一次就把连续失败计数清零（"连续"两个字是这么来的）。"""
        self._failures[tool] = 0

    @property
    def tool_failures(self) -> dict[str, int]:
        return {tool: n for tool, n in self._failures.items() if n}

    def reached_iter_limit(self, iteration: int) -> bool:
        return iteration >= self.max_iter
