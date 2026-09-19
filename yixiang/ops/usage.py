"""usage.jsonl：每次 LLM 调用一行，成本与 token 的唯一事实来源（§4.4）。

行格式（字段名冻结）::

    {"ts":"2026-09-19T08:12:33+08:00","turn_id":"t_20260919_081233_ab12",
     "role":"main","model":"deepseek-chat","input":5821,"cached_input":5012,
     "output":287,"latency_ms":4210,"cost_cny":0.0043}

写入口是 ``UsageSink``（Provider 注入），所以"谁记账"只有一个答案：Provider。
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Protocol

from yixiang.ops.pricing import cost_cny
from yixiang.runtime.models import Clock, SystemClock, Usage, to_local_iso


@dataclass(slots=True)
class UsageLine:
    ts: str
    turn_id: str
    role: str
    model: str
    input: int
    cached_input: int
    output: int
    latency_ms: int
    cost_cny: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "turn_id": self.turn_id,
            "role": self.role,
            "model": self.model,
            "input": self.input,
            "cached_input": self.cached_input,
            "output": self.output,
            "latency_ms": self.latency_ms,
            "cost_cny": self.cost_cny,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> UsageLine:
        return cls(
            ts=str(raw.get("ts", "")),
            turn_id=str(raw.get("turn_id", "")),
            role=str(raw.get("role", "")),
            model=str(raw.get("model", "")),
            input=int(raw.get("input", 0)),
            cached_input=int(raw.get("cached_input", 0)),
            output=int(raw.get("output", 0)),
            latency_ms=int(raw.get("latency_ms", 0)),
            cost_cny=float(raw.get("cost_cny", 0.0)),
        )


class UsageSink(Protocol):
    """Provider 的回调口：记一行用量。实现必须自己不抛异常。"""

    def record(
        self,
        *,
        role: str,
        model: str,
        usage: Usage,
        latency_ms: int,
        turn_id: str,
    ) -> UsageLine | None: ...


class NullUsageSink:
    def record(self, **_: Any) -> None:
        return None


class JsonlUsageSink:
    """默认实现：追加写 ``data/usage.jsonl``。"""

    def __init__(self, path: Path | str, *, clock: Clock | None = None) -> None:
        self.path = Path(path)
        self.clock = clock or SystemClock()

    def record(
        self,
        *,
        role: str,
        model: str,
        usage: Usage,
        latency_ms: int,
        turn_id: str,
    ) -> UsageLine:
        line = UsageLine(
            ts=to_local_iso(self.clock.now()),
            turn_id=turn_id,
            role=role,
            model=model,
            input=usage.input_tokens,
            cached_input=usage.cached_input_tokens,
            output=usage.output_tokens,
            latency_ms=latency_ms,
            cost_cny=cost_cny(model, usage),
        )
        append_line(self.path, line)
        return line


@dataclass
class CollectingUsageSink:
    """测试用：把 UsageLine 收进列表。"""

    lines: list[UsageLine] = field(default_factory=list)
    clock: Clock = field(default_factory=SystemClock)

    def record(
        self,
        *,
        role: str,
        model: str,
        usage: Usage,
        latency_ms: int,
        turn_id: str,
    ) -> UsageLine:
        line = UsageLine(
            ts=to_local_iso(self.clock.now()),
            turn_id=turn_id,
            role=role,
            model=model,
            input=usage.input_tokens,
            cached_input=usage.cached_input_tokens,
            output=usage.output_tokens,
            latency_ms=latency_ms,
            cost_cny=cost_cny(model, usage),
        )
        self.lines.append(line)
        return line


def append_line(path: Path | str, line: UsageLine) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(line.as_dict(), ensure_ascii=False) + "\n")


def iter_lines(path: Path | str) -> Iterable[UsageLine]:
    path = Path(path)
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            yield UsageLine.from_dict(json.loads(raw))
        except (ValueError, TypeError):
            continue  # 坏行跳过：用量文件是追加写的，不因为一行坏掉丢整份账


def _local_day(ts: str) -> date | None:
    try:
        parsed = datetime.fromisoformat(ts)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone().date()


def summarize(
    path: Path | str,
    *,
    period: str = "day",
    ref: date | None = None,
) -> dict[str, Any]:
    """按天/月汇总（``yixiang ops cost`` 与 CLI ``/cost`` 共用）。"""
    ref = ref or datetime.now().astimezone().date()
    total = {
        "calls": 0,
        "turns": 0,
        "input": 0,
        "cached_input": 0,
        "output": 0,
        "cost_cny": 0.0,
        "latency_ms": 0,
    }
    by_role: dict[str, dict[str, float]] = {}
    by_model: dict[str, dict[str, float]] = {}
    turn_ids: set[str] = set()
    for line in iter_lines(path):
        day = _local_day(line.ts)
        if day is None:
            continue
        if period == "day" and day != ref:
            continue
        if period == "month" and (day.year, day.month) != (ref.year, ref.month):
            continue
        total["calls"] += 1
        total["input"] += line.input
        total["cached_input"] += line.cached_input
        total["output"] += line.output
        total["cost_cny"] = round(total["cost_cny"] + line.cost_cny, 6)
        total["latency_ms"] += line.latency_ms
        if line.turn_id:
            turn_ids.add(line.turn_id)
        for bucket, key in ((by_role, line.role), (by_model, line.model)):
            slot = bucket.setdefault(key, {"calls": 0, "input": 0, "output": 0, "cost_cny": 0.0})
            slot["calls"] += 1
            slot["input"] += line.input
            slot["output"] += line.output
            slot["cost_cny"] = round(slot["cost_cny"] + line.cost_cny, 6)
    total["turns"] = len(turn_ids)
    return {"period": period, "ref": ref.isoformat(), "total": total,
            "by_role": by_role, "by_model": by_model}


def summary_text(summary: dict[str, Any], *, budget_cny_per_day: float | None = None) -> str:
    total = summary["total"]
    lines = [
        f"{summary['ref']}（{summary['period']}）：{total['turns']} 轮 · "
        f"{total['calls']} 次调用",
        f"tokens：{total['input']} in（{total['cached_input']} cached）/ {total['output']} out",
        f"成本：¥{total['cost_cny']:.4f}",
    ]
    if budget_cny_per_day is not None:
        used = total["cost_cny"] / budget_cny_per_day * 100 if budget_cny_per_day else 0.0
        flag = "超预算" if used > 100 else "正常"
        lines.append(f"预算：¥{budget_cny_per_day:.2f}/天，已用 {used:.1f}%（{flag}）")
    for role, slot in summary["by_role"].items():
        lines.append(
            f"  · {role}: {slot['calls']} 次 · {slot['input']} in / {slot['output']} out · "
            f"¥{slot['cost_cny']:.4f}"
        )
    return "\n".join(lines)
