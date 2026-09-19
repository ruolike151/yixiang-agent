"""计划工具：create_plan / add_task / list_today / complete_task（TECH §9.2）。

``list_today`` 是 D-02 的主角：**严格只返回今日 items，不编造**。
它的 description 里也写着这句，因为工具返回什么、模型说什么，靠的是同一份契约。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import datetime

from yixiang.tools.memo import parse_when

VALID_STATUS = ("done", "skipped")


def _date_str(value: str | None, now: datetime) -> str | None:
    """把 ``2026-09-20`` / ``明天`` 统一成 ``YYYY-MM-DD``；空值原样返回。"""
    if not value:
        return None
    parsed = parse_when(str(value), now)
    if parsed is not None:
        return parsed.date().isoformat()
    text = str(value).strip()
    return text[:10] if len(text) >= 10 else text


def create_plan(
    conn: sqlite3.Connection,
    now: Callable[[], datetime],
    title: str,
    goal: str = "",
    start_date: str | None = None,
    end_date: str | None = None,
) -> str:
    """建计划容器，返回 ``{"plan_id": ...}``；随后用 add_task 加每日任务。"""
    name = (title or "").strip()
    if not name:
        return "Error: 计划标题不能为空"
    start = _date_str(start_date, now())
    end = _date_str(end_date, now())
    with conn:
        cursor = conn.execute(
            "INSERT INTO plans(title, goal, start_date, end_date, created_at) "
            "VALUES(?, ?, ?, ?, ?)",
            (name, (goal or "").strip(), start, end, now().isoformat(timespec="seconds")),
        )
    return json.dumps(
        {"plan_id": cursor.lastrowid, "title": name, "start_date": start, "end_date": end},
        ensure_ascii=False,
    )


def add_task(
    conn: sqlite3.Connection,
    now: Callable[[], datetime],
    plan_id: int,
    date: str,
    content: str,
    est_minutes: int | None = None,
) -> str:
    """往计划里加一条某天的任务，返回 ``{"item_id": ...}``。"""
    plan = conn.execute("SELECT id FROM plans WHERE id = ?", (plan_id,)).fetchone()
    if plan is None:
        return f"Error: plan_id={plan_id} 不存在（先调 create_plan 拿 plan_id）"
    text = (content or "").strip()
    if not text:
        return "Error: 任务内容不能为空"
    day = _date_str(date, now())
    if not day:
        return "Error: date 不能为空（格式 YYYY-MM-DD）"
    minutes = int(est_minutes) if est_minutes not in (None, "") else None
    with conn:
        cursor = conn.execute(
            "INSERT INTO plan_items(plan_id, date, content, est_minutes, created_at) "
            "VALUES(?, ?, ?, ?, ?)",
            (plan_id, day, text, minutes, now().isoformat(timespec="seconds")),
        )
    return json.dumps(
        {"item_id": cursor.lastrowid, "plan_id": plan_id, "date": day}, ensure_ascii=False
    )


def list_today(
    conn: sqlite3.Connection, now: Callable[[], datetime], date: str | None = None
) -> str:
    """今天（或指定日期）的任务 + 当天到期的备忘；**只报数据库里有的**。"""
    day = _date_str(date, now()) or now().date().isoformat()
    items = conn.execute(
        """
        SELECT item.id AS id, item.content AS content, item.est_minutes AS est_minutes,
               item.status AS status, plan.title AS plan_title
          FROM plan_items AS item
          LEFT JOIN plans AS plan ON plan.id = item.plan_id
         WHERE item.date = ?
         ORDER BY item.status <> 'todo', item.id
        """,
        (day,),
    ).fetchall()
    memos = conn.execute(
        "SELECT id, content, due_at FROM memos "
        "WHERE done = 0 AND due_at IS NOT NULL AND date(due_at) = date(?) ORDER BY due_at",
        (day,),
    ).fetchall()
    if not items and not memos:
        return f"（{day} 没有安排：没有任务，也没有到期备忘）"
    lines = [f"{day} 的安排："]
    for row in items:
        minutes = f"{row['est_minutes']} 分钟" if row["est_minutes"] else "未估时"
        lines.append(
            f"- [item_id={row['id']}] {row['content']}（{minutes}，{row['status']}）"
            + (f" · 来自《{row['plan_title']}》" if row["plan_title"] else "")
        )
    if memos:
        lines.append("到期备忘：")
        for row in memos:
            lines.append(f"- [memo_id={row['id']}] {row['content']}（{row['due_at']}）")
    return "\n".join(lines)


def complete_task(
    conn: sqlite3.Connection,
    now: Callable[[], datetime],
    item_id: int,
    status: str = "done",
) -> str:
    """把任务标记为 done / skipped（不提供物理删除）。"""
    if status not in VALID_STATUS:
        return f"Error: status 只接受 {' / '.join(VALID_STATUS)}，收到 {status!r}"
    row = conn.execute("SELECT id FROM plan_items WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        return f"Error: 没有 item_id={item_id} 的任务（先调 list_today 看当前 id）"
    with conn:
        conn.execute("UPDATE plan_items SET status = ? WHERE id = ?", (status, item_id))
    return json.dumps({"ok": True, "item_id": item_id, "status": status}, ensure_ascii=False)
