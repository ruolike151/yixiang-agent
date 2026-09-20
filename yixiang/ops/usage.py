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

# §6.1 的 system 段名（``ops cost --explain`` 用它把"钱花在哪一段"说清楚）
SEGMENT_KEYS = ("s1", "s2", "s3", "s4", "s5", "s6", "s7", "s8")
SEGMENT_LABELS = {
    "s1": "S1 soul 身份与守则",
    "s2": "S2 Learned rules",
    "s3": "S3 user.md 画像",
    "s4": "S4 memory.md 核心",
    "s5": "S5 skills 索引",
    "s6": "S6 本轮检索记忆",
    "s7": "S7 环境时间",
    "s8": "S8 本轮契约",
}

# 中文按 1 字 ≈ 0.7 token 的保守口径（§15.1）
CHARS_PER_TOKEN = 0.7


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


def period_lines(path: Path | str, *, period: str = "day", ref: date | None = None) -> list[UsageLine]:
    """某天/某月的原始用量行（``--explain`` 的分段统计从这里出发）。"""
    ref = ref or datetime.now().astimezone().date()
    kept: list[UsageLine] = []
    for line in iter_lines(path):
        day = _local_day(line.ts)
        if day is None:
            continue
        if period == "day" and day != ref:
            continue
        if period == "month" and (day.year, day.month) != (ref.year, ref.month):
            continue
        kept.append(line)
    return kept


def segment_report(traces: Iterable[dict[str, Any]]) -> list[tuple[str, float, float]]:
    """把 trace 里的 ``working_memory`` 分段长度平均成"各段占比"。

    返回 ``[(段名, 平均字符数, 占比%)]``，按 S1→S8 顺序；没有可用的 trace 就返回空表。
    ``s6`` 在 trace 里是 ``{"facts": …, "episodes": …, "chars": …}``，这里取 ``chars``。
    """
    totals = dict.fromkeys(SEGMENT_KEYS, 0)
    counted = 0
    for record in traces:
        memory = record.get("working_memory") if isinstance(record, dict) else None
        if not isinstance(memory, dict):
            continue
        values: dict[str, int] = {}
        for key in SEGMENT_KEYS:
            raw = memory.get(key)
            if isinstance(raw, dict):
                values[key] = int(raw.get("chars") or 0)
            elif isinstance(raw, int):
                values[key] = raw
        if not values:
            continue
        counted += 1
        for key, value in values.items():
            totals[key] += value
    if not counted:
        return []
    average = {key: totals[key] / counted for key in SEGMENT_KEYS}
    subtotal = sum(average.values()) or 1.0
    return [(SEGMENT_LABELS[key], average[key], average[key] / subtotal * 100) for key in SEGMENT_KEYS]


def explain_text(
    summary: dict[str, Any],
    *,
    lines: Iterable[UsageLine] = (),
    traces: Iterable[dict[str, Any]] = (),
    budget_cny_per_day: float | None = None,
) -> str:
    """``ops cost --explain`` 的正文：钱花在哪个角色、哪一段、哪几轮（§15.2 护栏）。

    三段视角各回答一个问题：**谁花的**（角色占比）、**哪一段涨了**（system 分段
    占比）、**哪一轮贵**（按 turn 排序）。数字都能回到 ``usage.jsonl`` 与 trace。
    """
    total = summary["total"]
    cost = float(total["cost_cny"])
    usage_lines = list(lines)
    out = [
        f"{summary['ref']}（{summary['period']}）：{total['turns']} 轮 · {total['calls']} 次调用 · "
        f"{total['input']} in / {total['output']} out · ¥{cost:.4f}"
    ]
    if budget_cny_per_day:
        used = cost / budget_cny_per_day * 100
        out.append(f"预算 ¥{budget_cny_per_day:.2f}/天 → 已用 {used:.1f}%")

    out.append("按角色（谁花的）：")
    by_role = summary["by_role"] or {}
    for role, slot in sorted(by_role.items(), key=lambda item: -item[1]["cost_cny"]):
        share = slot["cost_cny"] / cost * 100 if cost else 0.0
        out.append(
            f"  · {role:<10} {slot['calls']:>3} 次 · {slot['input']:>7} in / "
            f"{slot['output']:>5} out · ¥{slot['cost_cny']:.4f}（{share:.1f}%）"
        )
    main = by_role.get("main")
    if main and main["input"]:
        main_lines = [line for line in usage_lines if line.role == "main"]
        cached = sum(line.cached_input for line in main_lines)
        if main_lines:
            out.append(
                f"  · main 的前缀缓存命中：{cached / main['input'] * 100:.1f}%"
                f"（{cached}/{main['input']} in，缓存不是优化项，是预算成立的前提）"
            )

    segments = segment_report(traces)
    if segments:
        out.append("按 system 分段（哪一段涨了；字符口径，1 字 ≈ 0.7 token）：")
        for label, average, share in segments:
            out.append(f"  · {label:<20} {average:>7.0f} 字 · {share:>5.1f}%")
    else:
        out.append("按 system 分段：今天还没有 trace，跑一轮 chat 再看（字符口径）。")

    turns: dict[str, float] = {}
    for line in usage_lines:
        if line.turn_id:
            turns[line.turn_id] = turns.get(line.turn_id, 0.0) + line.cost_cny
    if turns:
        out.append("最贵的几轮：")
        for turn_id, turn_cost in sorted(turns.items(), key=lambda item: -item[1])[:3]:
            out.append(f"  · {turn_id} ¥{turn_cost:.4f}")
    return "\n".join(out)


def write_daily_report(
    usage_path: Path | str,
    reports_dir: Path | str,
    *,
    ref: date | None = None,
    budget_cny_per_day: float | None = None,
    clock: Clock | None = None,
) -> Path:
    """把某天的汇总写成 ``reports/usage-YYYY-MM-DD.json``（每日汇总 job 的落点）。"""
    ref = ref or (clock or SystemClock()).now().astimezone().date()
    summary = summarize(usage_path, period="day", ref=ref)
    path = Path(reports_dir) / f"usage-{ref.isoformat()}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        **summary,
        "budget_cny_per_day": budget_cny_per_day,
        "written_at": to_local_iso((clock or SystemClock()).now()),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path
