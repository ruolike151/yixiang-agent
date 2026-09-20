"""trace：每轮一行追加写 ``data/traces/YYYY-MM-DD.jsonl``（TECH §11.1）。

字段设计原则（照抄 §11.1 的表格）：
  * 记 ``latency_ms`` 分项 —— 用户说"慢"时能定位是模型还是工具；
  * 记 ``working_memory`` 分段长度 —— 排查"为什么答错"的第一现场（§6.3）；
  * ``reply_preview`` 只存前 100 字，全文在 ``chat_log``；
  * 不记密钥、不记完整 tool result。
"""

from __future__ import annotations

import json
import secrets
from datetime import date, datetime
from pathlib import Path
from typing import Any

from yixiang.runtime.models import Clock, SystemClock, TurnResult, to_local_iso

REPLY_PREVIEW_LIMIT = 100


def new_turn_id(clock: Clock | None = None) -> str:
    """``t_20260919_081233_ab12``（§11.1）。"""
    now = (clock or SystemClock()).now()
    return f"t_{now:%Y%m%d_%H%M%S}_{secrets.token_hex(2)}"


def trace_file(traces_dir: Path | str, day: date | None = None) -> Path:
    day = day or datetime.now().astimezone().date()
    return Path(traces_dir) / f"{day.isoformat()}.jsonl"


def append_trace(traces_dir: Path | str, record: dict[str, Any]) -> Path:
    path = trace_file(traces_dir, _record_day(record))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return path


def _record_day(record: dict[str, Any]) -> date:
    try:
        return datetime.fromisoformat(str(record.get("ts", ""))).date()
    except ValueError:
        return datetime.now().astimezone().date()


def read_traces(traces_dir: Path | str, *, day: date | None = None,
                limit: int | None = None) -> list[dict[str, Any]]:
    """读某天的 trace；``day=None`` 时从最新一天往回找（``/trace`` 用）。"""
    traces_dir = Path(traces_dir)
    if not traces_dir.is_dir():
        return []
    # 文件名是 ISO 日期，字典序 = 时间序（旧 → 新）
    files = (
        [trace_file(traces_dir, day)]
        if day is not None
        else sorted(traces_dir.glob("*.jsonl"))
    )
    records: list[dict[str, Any]] = []
    for path in files:
        if not path.is_file():
            continue
        for raw in path.read_text(encoding="utf-8").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                records.append(json.loads(raw))
            except ValueError:
                continue
    if limit is not None:
        records = records[-limit:]
    return records


def find_trace(traces_dir: Path | str, turn_id: str) -> dict[str, Any] | None:
    for record in reversed(read_traces(traces_dir)):
        if record.get("turn_id") == turn_id:
            return record
    return None


def build_turn_record(
    *,
    result: TurnResult,
    session_id: str,
    source: str,
    user_text: str,
    clock: Clock | None = None,
    gate: dict[str, Any] | None = None,
    intent: dict[str, Any] | None = None,
    rag: dict[str, Any] | None = None,
    latency_ms: dict[str, int] | None = None,
) -> dict[str, Any]:
    now = (clock or SystemClock()).now()
    return {
        "turn_id": result.turn_id,
        "ts": to_local_iso(now),
        "session": session_id,
        "source": source,
        "user_text": user_text,
        "intent": intent or {"remember": False},
        "gate": gate,
        # 检索降级状态（D-24）：``embed != "ok"`` 时这一轮的召回是纯 FTS5 的
        "rag": rag,
        "working_memory": result.working_memory,
        "iterations": result.iterations,
        "tool_calls": [event.as_trace() for event in result.tool_calls],
        "tokens": result.usage.as_dict(),
        "cost_cny": _cost(result),
        "latency_ms": latency_ms or result.latency_ms,
        "model": result.model,
        "reply_preview": (result.reply or "")[:REPLY_PREVIEW_LIMIT],
        "finish_reason": result.finish_reason,
        "error": result.error,
        "error_detail": result.error_detail,
        "memory_write_failed": result.memory_write_failed,
    }


def _cost(result: TurnResult) -> float:
    from yixiang.ops.pricing import cost_cny

    return cost_cny(result.model, result.usage)
