"""备忘工具：add_memo / list_memos / finish_memo + 相对时间解析（TECH §9.2）。

为什么单独写解析器：模型传进来的 ``due_at`` 常常是"周五中午"这种人话。
与其让它自己算出错误日期，不如在工具侧统一解析成 ISO8601——**解析错了能被
用例抓住，模型算错了只能靠运气**（D-01 断言的就是这一段）。

解析规则（用假时钟跑，纯函数，无副作用）：

============  ==================================================
写法           结果
============  ==================================================
ISO8601       ``2026-09-25`` / ``2026-09-25T12:00`` 原样采纳
今天/明天/后天  按当前日期推进
周五/星期三/礼拜五  最近的**未来**该星期（已过则顺延到下周）
下周五         再 +7 天
中午/上午/下午/晚上  12:00 / 09:00 / 14:00 / 20:00
3点/15:30      指定时刻（"下午3点"= 15:00）
不带时刻        默认 09:00
============  ==================================================
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta

from yixiang.runtime.models import to_local_iso

WEEKDAY_CHARS = {
    "一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5,
    "日": 6, "天": 6, "7": 6,
    "1": 0, "2": 1, "3": 2, "4": 3, "5": 4, "6": 5,
}

DEFAULT_HOUR = 9

_WEEKDAY_RE = re.compile(r"(本周|这周|下周|下个?星期|周|星期|礼拜)([一二三四五六日天1-7])")
_DAY_OFFSETS = {"今天": 0, "今日": 0, "明天": 1, "明日": 1, "后天": 2}
_CLOCK_RE = re.compile(r"(\d{1,2})\s*[:：]\s*(\d{2})")
_HOUR_RE = re.compile(r"(\d{1,2})\s*点(半|\d{0,2}分?)?")
_PERIOD_HOURS = (("凌晨", 6), ("早上", 8), ("上午", 9), ("中午", 12), ("正午", 12),
                 ("下午", 14), ("傍晚", 18), ("晚上", 20), ("夜里", 21))


def parse_when(text: str, now: datetime) -> datetime | None:
    """把自然语言时间解析成带时区的 ``datetime``；解析不出来返回 ``None``。"""
    raw = (text or "").strip()
    if not raw:
        return None
    if now.tzinfo is None:
        now = now.astimezone()

    iso = _parse_iso(raw, now.tzinfo)
    if iso is not None:
        return iso

    hour, minute = _parse_clock(raw)
    day = _parse_day(raw, now)
    if day is None:
        candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= now:
            # 没写日期又说了一个已经过去的时刻 → 按明天算
            candidate = candidate + timedelta(days=1)
        return candidate
    return day.replace(hour=hour, minute=minute, second=0, microsecond=0)


def _parse_iso(raw: str, tzinfo) -> datetime | None:
    candidate = raw.replace("年", "-").replace("月", "-").replace("日", "")
    for text in (candidate, candidate.replace(" ", "T")):
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=tzinfo)
        if ":" not in text:  # 只有日期 → 默认上午 9 点
            parsed = parsed.replace(hour=DEFAULT_HOUR, minute=0)
        return parsed.replace(second=0, microsecond=0)
    return None


def _parse_day(raw: str, now: datetime) -> datetime | None:
    for token, offset in _DAY_OFFSETS.items():
        if token in raw:
            base = now + timedelta(days=offset)
            return base.replace(hour=0, minute=0, second=0, microsecond=0)
    match = _WEEKDAY_RE.search(raw)
    if not match:
        return None
    prefix, char = match.group(1), match.group(2)
    target = WEEKDAY_CHARS[char]
    delta = (target - now.weekday()) % 7  # 已过则自然顺延到下周
    if prefix in {"下周", "下星期", "下个星期"}:
        delta += 7
    base = now + timedelta(days=delta)
    return base.replace(hour=0, minute=0, second=0, microsecond=0)


def _parse_clock(raw: str) -> tuple[int, int]:
    """取时刻；没说时刻时给 ``DEFAULT_HOUR``（09:00）。"""
    match = _CLOCK_RE.search(raw)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
        return _shift_hour(raw, hour) % 24, min(minute, 59)
    match = _HOUR_RE.search(raw)
    if match:
        hour = int(match.group(1))
        tail = match.group(2) or ""
        minute = 30 if tail.startswith("半") else int(tail.rstrip("分") or 0)
        return _shift_hour(raw, hour) % 24, min(minute, 59)
    for token, hour in _PERIOD_HOURS:
        if token in raw:
            return hour, 0
    return DEFAULT_HOUR, 0


def _shift_hour(raw: str, hour: int) -> int:
    """``下午3点`` = 15:00；``晚上8点`` = 20:00（12 小时制补齐）。"""
    if hour >= 12:
        return hour
    if any(token in raw for token in ("下午", "晚上", "傍晚", "夜里")):
        return hour + 12
    if hour <= 5 and "凌晨" not in raw:
        return hour + 12
    return hour


def add_memo(
    conn: sqlite3.Connection,
    now: Callable[[], datetime],
    content: str,
    due_at: str | None = None,
    idempotency_key: str | None = None,
) -> str:
    """写一条备忘。返回 JSON；``due_at`` 解析不了时降级保存并给 warning。"""
    text = (content or "").strip()
    if not text:
        return "Error: 备忘正文不能为空"
    if idempotency_key:
        existing = conn.execute(
            "SELECT id, content, due_at FROM memos WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        if existing is not None:
            return json.dumps(
                {
                    "id": existing["id"],
                    "content": existing["content"],
                    "due_at": existing["due_at"],
                    "deduped": True,
                },
                ensure_ascii=False,
            )

    due_iso: str | None = None
    warning: str | None = None
    if due_at:
        parsed = parse_when(str(due_at), now())
        if parsed is None:
            warning = f"due_at={due_at!r} 解析失败，已按无截止时间保存"
        else:
            due_iso = to_local_iso(parsed)

    with conn:
        cursor = conn.execute(
            "INSERT INTO memos(content, due_at, created_at, idempotency_key) "
            "VALUES(?, ?, ?, ?)",
            (text, due_iso, to_local_iso(now()), idempotency_key),
        )
    payload: dict[str, object] = {"id": cursor.lastrowid, "content": text, "due_at": due_iso}
    if warning:
        payload["warning"] = warning
    return json.dumps(payload, ensure_ascii=False)


def list_memos(
    conn: sqlite3.Connection,
    now: Callable[[], datetime],
    status: str = "open",
    due_before: str | None = None,
) -> str:
    """列出备忘（默认未完成），按截止时间排序；无截止时间的排最后。"""
    done = 1 if status == "done" else 0
    sql = (
        "SELECT id, content, due_at, done FROM memos WHERE done = ?"
        + (" AND date(due_at) <= date(?)" if due_before else "")
        + " ORDER BY due_at IS NULL, due_at, id"
    )
    params: tuple[object, ...] = (done, due_before) if due_before else (done,)
    rows = conn.execute(sql, params).fetchall()
    if not rows:
        label = "已完成" if done else "未完成"
        return f"（暂无{label}备忘）"
    lines = []
    for row in rows:
        when = row["due_at"] or "无截止时间"
        mark = "✓" if row["done"] else "○"
        lines.append(f"[{row['id']}] {mark} {when} {row['content']}")
    return "\n".join(lines)


def finish_memo(
    conn: sqlite3.Connection, now: Callable[[], datetime], id: int
) -> str:
    """把一条备忘标记为完成；id 不存在时返回可行动的 Error。"""
    row = conn.execute("SELECT id FROM memos WHERE id = ?", (id,)).fetchone()
    if row is None:
        return f"Error: 没有 id={id} 的备忘（先调 list_memos 看当前 id）"
    with conn:
        conn.execute(
            "UPDATE memos SET done = 1, done_at = ? WHERE id = ?",
            (to_local_iso(now()), id),
        )
    return json.dumps({"ok": True, "id": id}, ensure_ascii=False)
