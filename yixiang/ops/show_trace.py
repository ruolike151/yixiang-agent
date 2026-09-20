"""终端渲染：单轮链路（``/trace``、``yixiang ops show-trace <turn_id>``）。

输出格式照 §10.1 的「本轮」框——面试演示时一屏要能讲完：门控做了什么、
调了哪些工具、花了多少 token 与钱。
"""

from __future__ import annotations

from typing import Any

WIDTH = 62


def _box(lines: list[str]) -> str:
    head = "┌ 本轮 " + "─" * max(WIDTH - 6, 4)
    body = [f"  │ {line}" for line in lines]
    return "\n".join([head, *body, "  └" + "─" * (WIDTH - 2)])


def render_turn_box(record: dict[str, Any]) -> str:
    """把一条 trace 渲染成「本轮」摘要框。"""
    lines: list[str] = []
    gate = record.get("gate")
    if gate:
        skipped = gate.get("skipped_by")
        reason = gate.get("reason") or "—"
        mark = "rule" if skipped else f"{gate.get('ms', 0)}ms"
        lines.append(f"gate: retrieve={str(bool(gate.get('retrieve'))).lower()}（{mark}，{reason}）")
    tools = record.get("tool_calls") or []
    if tools:
        parts = []
        for call in tools:
            mark = "✓" if call.get("ok") else "✗"
            parts.append(f"{call.get('tool')} {mark} {call.get('ms', 0)}ms")
        lines.append("tools: " + " · ".join(parts))
    else:
        lines.append("tools: —")
    tokens = record.get("tokens") or {}
    lines.append(
        f"iter: {record.get('iterations', 1)} · tokens: {tokens.get('in', 0)} in "
        f"({tokens.get('cached_in', 0)} cached) / {tokens.get('out', 0)} out"
    )
    latency = record.get("latency_ms") or {}
    total_ms = latency.get("total", 0)
    lines.append(
        f"cost: ¥{float(record.get('cost_cny') or 0):.4f} · latency: {total_ms / 1000:.1f}s"
    )
    rag = record.get("rag") or {}
    if rag.get("embed") == "unavailable":
        # 降级是产品要求（D-24），但必须可观测：一行带过，不当错误报
        lines.append("rag: 已降级（纯 FTS5）")
    if record.get("error"):
        detail = record.get("error_detail") or ""
        lines.append(f"error: {record['error']}{(' · ' + str(detail)[:60]) if detail else ''}")
    return _box(lines)


def render_trace_detail(record: dict[str, Any]) -> str:
    """``ops show-trace`` 的完整视图（比 /trace 多分段长度与工具参数）。"""
    lines = [
        f"turn_id     : {record.get('turn_id', '')}",
        f"时间         : {record.get('ts', '')}",
        f"会话/来源     : {record.get('session', '')} / {record.get('source', '')}",
        f"用户         : {record.get('user_text', '')}",
        f"模型         : {record.get('model', '')}",
        f"迭代         : {record.get('iterations', 1)}",
    ]
    working = record.get("working_memory") or {}
    if working:
        s6 = working.get("s6") or {}
        lines.append(
            "工作记忆      : "
            f"s1={working.get('s1', 0)} s2={working.get('s2', 0)} s3={working.get('s3', 0)} "
            f"s4={working.get('s4', 0)} s5={working.get('s5', 0)} "
            f"s6={s6.get('chars', 0)}(facts={s6.get('facts', 0)}) "
            f"s7={working.get('s7', 0)} s8={working.get('s8', 0)} "
            f"history={working.get('history_turns', 0)} 轮"
        )
    tools = record.get("tool_calls") or []
    if tools:
        lines.append("工具调用      :")
        for call in tools:
            mark = "✓" if call.get("ok") else "✗"
            lines.append(
                f"  [{call.get('iter')}] {call.get('tool')} {mark} {call.get('ms', 0)}ms "
                f"{_short(call.get('args'))}"
            )
    tokens = record.get("tokens") or {}
    lines.append(
        f"tokens       : {tokens.get('in', 0)} in ({tokens.get('cached_in', 0)} cached) / "
        f"{tokens.get('out', 0)} out · ¥{float(record.get('cost_cny') or 0):.4f}"
    )
    latency = record.get("latency_ms") or {}
    lines.append(
        f"耗时         : 模型 {latency.get('llm', 0)}ms · 工具 {latency.get('tools', 0)}ms · "
        f"总计 {latency.get('total', 0)}ms"
    )
    lines.append(f"回复预览      : {record.get('reply_preview', '')}")
    lines.append(f"结束原因      : {record.get('finish_reason', '')}")
    if record.get("error"):
        lines.append(f"错误         : {record.get('error')} {record.get('error_detail') or ''}")
    return "\n".join(lines)


def render_trace_list(records: list[dict[str, Any]]) -> str:
    """``/trace [n]`` 的紧凑列表。"""
    if not records:
        return "（还没有 trace：先聊一句试试）"
    lines = []
    for record in records:
        tools = record.get("tool_calls") or []
        tool_text = ",".join(str(call.get("tool")) for call in tools) or "—"
        tokens = record.get("tokens") or {}
        lines.append(
            f"{record.get('turn_id', '')}  {record.get('ts', '')[11:19]}  "
            f"iter={record.get('iterations', 1)}  tok={tokens.get('in', 0)}/"
            f"{tokens.get('out', 0)}  ¥{float(record.get('cost_cny') or 0):.4f}  {tool_text}"
        )
    return "\n".join(lines)


def _short(value: Any, limit: int = 80) -> str:
    text = str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"
