"""计划工具：create_plan / add_task / list_today / list_range / complete_task /
reschedule_task（TECH §9.2）。

``list_today`` 是 D-02 的主角：**严格只返回今日 items，不编造**。
它的 description 里也写着这句，因为工具返回什么、模型说什么，靠的是同一份契约。

``list_range`` 是"这周 / 下周有什么"的一次性出口：按天分组、只报库里有的。
两个查询工具都只认 ``plans`` / ``plan_items`` 这一段事实——用户在别处听到的任务
（写在回复里的、历史里的）一律不算数。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from yixiang.tools.memo import parse_day, parse_range, parse_when

VALID_STATUS = ("done", "skipped")

WEEKDAY_ZH = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def _sync_plan_doc(conn: sqlite3.Connection, data_dir: Path | str | None) -> None:
    """计划一变就把 ``your_plan.md`` 跟着重渲染（§7.3 的"agent 也能同步该文档"）。

    文件写失败不能把工具调用整成失败——DB 才是权威，文件下次同步会自愈。
    """
    if not data_dir:
        return
    try:
        from yixiang import plan_doc

        plan_doc.write_plan_doc(conn, Path(data_dir))
    except Exception:  # noqa: BLE001 - 视图文件坏掉不该让排期工具报错
        return


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
    *,
    data_dir: Path | str | None = None,
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
    _sync_plan_doc(conn, data_dir)
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
    *,
    data_dir: Path | str | None = None,
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
    _sync_plan_doc(conn, data_dir)
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
    *,
    data_dir: Path | str | None = None,
) -> str:
    """把任务标记为 done / skipped（不提供物理删除）。"""
    if status not in VALID_STATUS:
        return f"Error: status 只接受 {' / '.join(VALID_STATUS)}，收到 {status!r}"
    row = conn.execute("SELECT id FROM plan_items WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        return f"Error: 没有 item_id={item_id} 的任务（先调 list_today 看当前 id）"
    with conn:
        conn.execute("UPDATE plan_items SET status = ? WHERE id = ?", (status, item_id))
    _sync_plan_doc(conn, data_dir)
    return json.dumps({"ok": True, "item_id": item_id, "status": status}, ensure_ascii=False)


def reschedule_task(
    conn: sqlite3.Connection,
    now: Callable[[], datetime],
    item_id: int,
    date: str,
    *,
    data_dir: Path | str | None = None,
) -> str:
    """把一条任务挪到另一天（只动 ``date``，内容 / 状态 / 所属计划都不动）。

    日期用的是严格解析（``parse_day``）：用户说"挪到本周"这种**没落到某一天**的
    说法时返回可行动的错误，让模型回去换算成具体日期再调一次，而不是挑一天写进库
    ——"改期改错了"比"改期没改成"更难发现（下次看 ``list_today`` 时它已经不在
    该在的位置上了）。
    """
    current = now()
    row = conn.execute("SELECT id, date FROM plan_items WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        return f"Error: 没有 item_id={item_id} 的任务（先调 list_today 看当前 id）"
    parsed = parse_day(date, current)
    if parsed is None:
        return (
            f"Error: date={date!r} 说不到具体哪一天（今天是 {current.date().isoformat()}）；"
            "请传 YYYY-MM-DD，或'明天 / 周五'这种写法"
        )
    day = parsed.date().isoformat()
    with conn:
        conn.execute("UPDATE plan_items SET date = ? WHERE id = ?", (day, item_id))
    _sync_plan_doc(conn, data_dir)
    return json.dumps(
        {"ok": True, "item_id": item_id, "date": day, "from": row["date"]}, ensure_ascii=False
    )


def list_range(
    conn: sqlite3.Connection,
    now: Callable[[], datetime],
    start_date: str,
    end_date: str | None = None,
) -> str:
    """一段日期内的任务 + 到期备忘，按天分组；**只报数据库里有的**。

    用户问"这周 / 下周有什么"时用这一个工具答完——``start_date`` 吃
    ``本周 / 这周 / 下周 / 2026-09-01 / 明天``，``end_date`` 不给就默认往后一周。
    区间解析不出来时返回可行动的错误（而不是兜底成"今天"）：答错区间比答"没有"
    更糟——用户会以为那几天真的空着。
    """
    current = now()
    window = parse_range(start_date, end_date, current)
    if window is None:
        asked = f"start_date={start_date!r}"
        if str(end_date or "").strip():
            asked += f", end_date={end_date!r}"
        return (
            f"Error: 起止日期解析不了（{asked}）；"
            "start_date 传 YYYY-MM-DD 或'本周 / 这周 / 下周 / 明天'，"
            "end_date 可选（不给就默认往后一周）"
        )
    start, end = window
    if end < start:
        return f"Error: end_date={end.isoformat()} 早于 start_date={start.isoformat()}（格式 YYYY-MM-DD）"

    items = conn.execute(
        """
        SELECT item.id AS id, item.date AS date, item.content AS content,
               item.est_minutes AS est_minutes, item.status AS status,
               plan.title AS plan_title
          FROM plan_items AS item
          LEFT JOIN plans AS plan ON plan.id = item.plan_id
         WHERE item.date BETWEEN ? AND ? AND item.status <> 'skipped'
         ORDER BY item.date, item.status <> 'todo', item.id
        """,
        (start.isoformat(), end.isoformat()),
    ).fetchall()
    memos = conn.execute(
        "SELECT id, content, due_at FROM memos "
        "WHERE done = 0 AND due_at IS NOT NULL AND date(due_at) BETWEEN ? AND ? "
        "ORDER BY due_at",
        (start.isoformat(), end.isoformat()),
    ).fetchall()
    if not items and not memos:
        return f"（{start.isoformat()} ～ {end.isoformat()} 没有安排：没有任务，也没有到期备忘）"

    grouped: dict[str, list[sqlite3.Row]] = {}
    for row in items:
        grouped.setdefault(str(row["date"]), []).append(row)
    memos_by_day: dict[str, list[sqlite3.Row]] = {}
    for row in memos:
        memos_by_day.setdefault(str(row["due_at"])[:10], []).append(row)

    lines = [f"{start.isoformat()} ～ {end.isoformat()} 的安排："]
    for day in sorted(set(grouped) | set(memos_by_day)):
        lines.append("")
        lines.append(f"## {day}（{WEEKDAY_ZH[datetime.fromisoformat(day).weekday()]}）")
        for row in grouped.get(day, []):
            minutes = f"{row['est_minutes']} 分钟" if row["est_minutes"] else "未估时"
            lines.append(
                f"- [item_id={row['id']}] {row['content']}（{minutes}，{row['status']}）"
                + (f" · 来自《{row['plan_title']}》" if row["plan_title"] else "")
            )
        for row in memos_by_day.get(day, []):
            lines.append(f"- [memo_id={row['id']}] {row['content']}（到期 {row['due_at']}）")
    return "\n".join(lines)
