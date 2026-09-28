"""``your_plan.md`` ⟷ ``plans`` / ``plan_items`` 双向同步（TECH §7.3 的同一范式）。

**它和 ``memory.md`` 分工不同，别混**：

  * ``memory.md`` 记的是**事实**——"我是谁、我喜欢什么、长期在做什么"。它由
    ``manage_memory`` / ``save_memory`` 管，是模型要长期记住的东西；
  * ``your_plan.md`` 记的是**排期**——"哪一天要做什么"。它由计划工具
    （``create_plan`` / ``add_task`` / ``complete_task`` / ``reschedule_task``）管，
    是"这两周怎么走"的日程视图。一次性的截止提醒走 ``memos``（自己一条线）。

``plans`` / ``plan_items`` 是权威源，这份文件是给人看、也给人改的渲染视图。
与 ``memory.md`` 共享同四条纪律：

  1. **文件为准**：人删整行 = 那条任务软删（``status='skipped'``，留痕可恢复）；
     人改一行 = 改任务内容；人加无 id 的一行 = 按所在日期新增一条任务并回写 id；
     人把一行挪到另一个日期段 = 那条任务改期。``status='done'`` 的条目不渲染
     （完成的任务从这份"待办视图"里消失，但库里还有）。
  2. **条目级 id**：``- [item_id=12] 内容（60 分钟，todo）`` 里的 12 就是
     ``plan_items.id``——这是"人改文件"与"改数据库"能对齐的唯一锚点。
  3. **先 DB 后文件**：DB 变更在一个事务里提交，再原子写文件（``.tmp`` + ``os.replace``）。
  4. **幂等**：写完把 sha256 落到 ``data/.your_plan_md.sha256``；同步先比哈希，
     一致直接返回（零开销）。

少了格式标记的文件**一个字节都不动**：宁可不同步，也不能抹掉人手写的东西。

这里不 import ``yixiang.tools.plan``（那是反向依赖，会成环）：两边只共享
``plans`` / ``plan_items`` 这两张表。
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from yixiang.memory.core_files import atomic_write_text, normalize_text

PLAN_FILE = "your_plan.md"
HASH_FILE = ".your_plan_md.sha256"
PLAN_FORMAT_MARKER = "<!-- yixiang:plan=v1 -->"

# 第一行必须是 ``# `` 开头的标题（``validate_plan_doc`` 会检查）；后面的说明是给用户看的，
# 顺带把"这份文件管什么、memory.md 管什么"讲清楚——两类文档的分工就写在文件头上。
PLAN_HEADER = (
    "# Your Plan — 你的排期（本文件记「哪一天做什么」；长期事实与偏好记在 memory.md）"
)
PLAN_INTRO = (
    "直接编辑本文件就能改排期：改一行文字 = 改任务内容；删掉整行 = 把这条移出排期；"
    "自己在某个日期下面加一行「- 任务内容」= 新增一条任务（下次同步会自动补上 item_id）。",
    "用 agent 管排期走 create_plan / add_task / complete_task / reschedule_task；"
    "记「我是谁、我喜欢什么」这类长期事实请改 memory.md。",
)
PLAN_EMPTY_LINE = "（还没有排期：可以让 agent 排，或者直接在下面某个日期下面手写一行）"

WEEKDAY_ZH = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

SECTION_RE = re.compile(r"^##\s+(\d{4}-\d{2}-\d{2})\s*(?:（[^）]*）)?\s*$")
# 结尾的元信息由渲染器生成：``（60 分钟，todo）`` / ``（未估时，todo）``
_META_PATTERN = r"（(?P<est>\d+\s*分钟|未估时)，(?P<status>todo|done|skipped)）"
META_ENTRY_RE = re.compile(rf"^- \[item_id=(?P<id>\d+)\]\s*(?P<content>.*?)\s*{_META_PATTERN}\s*$")
BARE_ENTRY_RE = re.compile(r"^- \[item_id=(?P<id>\d+)\]\s*(?P<content>.*?)\s*$")
META_PLAIN_RE = re.compile(rf"^-\s+(?P<content>.*?)\s*{_META_PATTERN}\s*$")
BARE_PLAIN_RE = re.compile(r"^-\s+(?P<content>.*?)\s*$")

VALID_STATUS = ("todo", "done", "skipped")


@dataclass(slots=True)
class PlanEntry:
    """文件里的一条任务行。``index`` 是它在整份文件里的行号（0 基）。"""

    index: int
    date: str
    item_id: int | None
    content: str
    est_minutes: int | None = None
    status: str | None = None


@dataclass(slots=True)
class PlanDoc:
    lines: list[str] = field(default_factory=list)
    entries: list[PlanEntry] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass(slots=True)
class PlanReport:
    """一次同步的结果（工具写入路径 / 启动同步共用）。"""

    changed: bool = False
    skipped: bool = False
    inserted: int = 0
    updated: int = 0
    removed: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def touched(self) -> bool:
        return bool(self.inserted or self.updated or self.removed)

    def summary(self) -> str:
        if self.skipped:
            return f"{PLAN_FILE} 未变化（sha256 一致），跳过同步"
        if not self.changed and not self.touched:
            return "无需同步"
        return f"新增 {self.inserted} · 更新 {self.updated} · 移出排期 {self.removed}"


# ------------------------------------------------------------------ 解析
def parse_plan_doc(text: str) -> PlanDoc:
    """逐行解析：只有落在 ``## YYYY-MM-DD`` 段里的任务行才算条目。"""
    lines = normalize_text(text).splitlines()
    doc = PlanDoc(lines=lines)
    current = ""
    for index, line in enumerate(lines):
        section = SECTION_RE.match(line)
        if section:
            raw = section.group(1)
            try:
                date.fromisoformat(raw)
            except ValueError:
                doc.warnings.append(f"第 {index + 1} 行：日期不合法 {raw!r}，这一段先跳过")
                current = ""
                continue
            current = raw
            continue
        if not current:
            continue
        entry = _parse_entry(line, index, current)
        if entry is not None:
            doc.entries.append(entry)
    return doc


def _parse_entry(line: str, index: int, current: str) -> PlanEntry | None:
    for pattern in (META_ENTRY_RE, BARE_ENTRY_RE):
        match = pattern.match(line)
        if not match:
            continue
        content = (match.group("content") or "").strip()
        if not content:
            return None
        return PlanEntry(
            index=index,
            date=current,
            item_id=int(match.group("id")),
            content=content,
            est_minutes=_parse_est(match.groupdict().get("est")),
            status=match.groupdict().get("status"),
        )
    for pattern in (META_PLAIN_RE, BARE_PLAIN_RE):
        match = pattern.match(line)
        if not match:
            continue
        content = (match.group("content") or "").strip()
        if not content:
            return None
        return PlanEntry(
            index=index,
            date=current,
            item_id=None,
            content=content,
            est_minutes=_parse_est(match.groupdict().get("est")),
            status=match.groupdict().get("status"),
        )
    return None


def _parse_est(raw: str | None) -> int | None:
    text = (raw or "").strip()
    if text.endswith("分钟"):
        digits = text[: -len("分钟")].strip()
        if digits.isdigit():
            return int(digits)
    return None


def validate_plan_doc(text: str) -> list[str]:
    """格式校验：返回问题清单（空 = 没问题）。人写坏了要能说清是哪一行。"""
    doc = parse_plan_doc(text)
    problems: list[str] = list(doc.warnings)
    lines = doc.lines
    if not any(line.strip() for line in lines):
        problems.append("error: 文件是空的")
        return problems
    if not lines[0].startswith("# "):
        problems.append("error: 第一行应该是 `# ` 开头的标题")
    if not any(line.strip() == PLAN_FORMAT_MARKER for line in lines):
        problems.append(
            f"warning: 缺少格式标记 {PLAN_FORMAT_MARKER}（同步会跳过这份文件，不改它）"
        )
    return problems


# ------------------------------------------------------------------ 路径 / 哈希
def plan_path(data_dir: Path | str) -> Path:
    return Path(data_dir) / PLAN_FILE


def _file_hash(path: Path) -> str:
    if not path.is_file():
        return ""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _read_stored_hash(data_dir: Path) -> str:
    path = data_dir / HASH_FILE
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8").strip()


def _write_stored_hash(data_dir: Path, path: Path) -> None:
    digest = _file_hash(path)
    if digest:
        atomic_write_text(data_dir / HASH_FILE, digest + "\n")


def _stamp(now: datetime | None) -> str:
    moment = now or datetime.now().astimezone()
    if moment.tzinfo is None:
        moment = moment.astimezone()
    return moment.isoformat(timespec="seconds")


# ------------------------------------------------------------------ 渲染
def _render(conn: sqlite3.Connection) -> str:
    """按 DB 渲染整份文件：``status='todo'`` 的条目按日期分组（done / skipped 不渲染）。"""
    rows = conn.execute(
        "SELECT id, date, content, est_minutes, status FROM plan_items"
        " WHERE status = 'todo' ORDER BY date, id"
    ).fetchall()
    grouped: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        grouped.setdefault(str(row["date"]), []).append(row)

    out = [PLAN_HEADER, "", *PLAN_INTRO, "", PLAN_FORMAT_MARKER, ""]
    if not grouped:
        out.extend([PLAN_EMPTY_LINE, ""])
    for day in sorted(grouped):
        out.append(f"## {day}（{_weekday(day)}）")
        out.extend(_render_entry(row) for row in grouped[day])
        out.append("")
    return "\n".join(out)


def _render_entry(row: sqlite3.Row) -> str:
    minutes = f"{row['est_minutes']} 分钟" if row["est_minutes"] else "未估时"
    return f"- [item_id={row['id']}] {row['content']}（{minutes}，{row['status']}）"


def _weekday(day: str) -> str:
    return WEEKDAY_ZH[date.fromisoformat(day).weekday()]


def _render_and_write(conn: sqlite3.Connection, data_dir: Path) -> None:
    _write_render(data_dir, _render(conn))


def _write_render(data_dir: Path, rendered: str) -> None:
    """把渲染结果原子写盘，并记下它的哈希（下次同步据此判断"人有没有动过"）。"""
    data_dir.mkdir(parents=True, exist_ok=True)
    path = plan_path(data_dir)
    atomic_write_text(path, rendered)
    _write_stored_hash(data_dir, path)


# ------------------------------------------------------------------ 文件 → DB
def _apply_file_to_db(
    conn: sqlite3.Connection, doc: PlanDoc, report: PlanReport, stamp: str
) -> None:
    """逐条执行上面的"文件为准"规则；所有写操作在**一个事务**里（失败整体回滚）。"""
    seen: set[int] = set()
    with conn:
        for entry in doc.entries:
            if entry.item_id is None:
                plan_id = _plan_for_new_items(conn, stamp)
                cursor = conn.execute(
                    "INSERT INTO plan_items(plan_id, date, content, est_minutes, status,"
                    " created_at) VALUES(?, ?, ?, ?, 'todo', ?)",
                    (plan_id, entry.date, entry.content, entry.est_minutes, stamp),
                )
                entry.item_id = int(cursor.lastrowid)
                seen.add(entry.item_id)
                report.inserted += 1
                report.warnings.append(
                    f"第 {entry.index + 1} 行：无 id 的手写任务已导入为 item #{entry.item_id}"
                )
                continue

            row = conn.execute(
                "SELECT * FROM plan_items WHERE id = ?", (entry.item_id,)
            ).fetchone()
            if row is None:
                report.warnings.append(
                    f"第 {entry.index + 1} 行：item_id={entry.item_id} 在库里不存在，已忽略"
                )
                continue
            seen.add(entry.item_id)
            _apply_entry_changes(conn, row, entry, report)

        # 库中还在待办、文件里却没有 → 软删（可恢复，与 memory.md 的软删同一口径）
        for row in conn.execute(
            "SELECT id FROM plan_items WHERE status = 'todo' ORDER BY id"
        ).fetchall():
            item_id = int(row["id"])
            if item_id in seen:
                continue
            conn.execute("UPDATE plan_items SET status = 'skipped' WHERE id = ?", (item_id,))
            report.removed += 1


def _apply_entry_changes(
    conn: sqlite3.Connection, row: sqlite3.Row, entry: PlanEntry, report: PlanReport
) -> None:
    """一条带 id 的行对比库：内容 / 耗时 / 日期（挪段）/ 状态，任一变就更新。"""
    updates: dict[str, object] = {}
    if entry.content and entry.content != str(row["content"]).strip():
        updates["content"] = entry.content
    if entry.est_minutes != row["est_minutes"]:
        updates["est_minutes"] = entry.est_minutes
    if entry.date != str(row["date"]):
        updates["date"] = entry.date
    if entry.status in VALID_STATUS and entry.status != str(row["status"]):
        updates["status"] = entry.status
    if not updates:
        return
    assignments = ", ".join(f"{name} = ?" for name in updates)
    conn.execute(
        f"UPDATE plan_items SET {assignments} WHERE id = ?",  # noqa: S608 - 列名来自本函数的白名单
        (*updates.values(), entry.item_id),
    )
    report.updated += 1


def _plan_for_new_items(conn: sqlite3.Connection, stamp: str) -> int:
    """手写的新任务挂到哪个计划上：最近建的那个；一个都没有就现建一个。"""
    row = conn.execute("SELECT id FROM plans ORDER BY id DESC LIMIT 1").fetchone()
    if row is not None:
        return int(row["id"])
    cursor = conn.execute(
        "INSERT INTO plans(title, goal, start_date, end_date, status, created_at)"
        " VALUES(?, ?, NULL, NULL, 'active', ?)",
        ("我的排期", "your_plan.md 里手写的任务", stamp),
    )
    return int(cursor.lastrowid)


# ------------------------------------------------------------------ 两个入口
def sync_plan_doc(
    conn: sqlite3.Connection, data_dir: Path | str, *, now: datetime | None = None
) -> PlanReport:
    """完整比对一次：文件是权威，把人的增删改落到 DB，再规范化写回文件。

    文件不存在 → 直接从 DB 渲染一份（第一次启动就把视图给用户）。
    文件没动过**且和库渲染出来的完全一致** → 跳过。少了格式标记 → 一个字节都不动。

    为什么要比"库渲染出来的那份"而不只比文件自身哈希：Web 控制台和 QQ 网关是两个
    进程、共用一份 ``data/state.db``。另一头写的排期不会碰这个进程的哈希文件，只比
    哈希就会把过期的视图一直留在那儿。
    """
    root = Path(data_dir)
    path = plan_path(root)
    report = PlanReport()
    stamp = _stamp(now)
    rendered = _render(conn)
    if not path.is_file():
        _write_render(root, rendered)
        report.changed = True
        return report
    file_hash = _file_hash(path)
    if file_hash == _digest(rendered):
        report.skipped = True
        return report

    text = path.read_text(encoding="utf-8")
    if PLAN_FORMAT_MARKER not in text:
        report.warnings.append(
            f"{PLAN_FILE} 缺少格式标记 {PLAN_FORMAT_MARKER}，"
            "不是 yixiang 生成的排期，已跳过同步（文件没动）"
        )
        return report

    # 人动过文件（现在的内容 ≠ 我们上次写的）→ 文件为准：先收进 DB，再按 DB 重渲染。
    # 人没动过、只是库被别的进程改了 → 直接重渲染，不把过期的文件当成人的意思。
    if file_hash != _read_stored_hash(root):
        doc = parse_plan_doc(text)
        report.warnings.extend(doc.warnings)
        _apply_file_to_db(conn, doc, report, stamp)
        rendered = _render(conn)
    _write_render(root, rendered)
    report.changed = True
    return report


def write_plan_doc(
    conn: sqlite3.Connection, data_dir: Path | str, *, now: datetime | None = None
) -> PlanReport:
    """工具写入路径的第二跳：先把人可能改过的文件收进 DB，再按 DB 重渲染。

    "先收再写"是关键——否则用户在文件里手改的内容会被下一次 ``add_task`` 直接抹掉。
    """
    root = Path(data_dir)
    path = plan_path(root)
    report = PlanReport()
    stamp = _stamp(now)
    if path.is_file() and _file_hash(path) != _read_stored_hash(root):
        text = path.read_text(encoding="utf-8")
        if PLAN_FORMAT_MARKER in text:
            doc = parse_plan_doc(text)
            report.warnings.extend(doc.warnings)
            _apply_file_to_db(conn, doc, report, stamp)
        else:
            report.warnings.append(
                f"{PLAN_FILE} 缺少格式标记 {PLAN_FORMAT_MARKER}，已跳过同步（文件没动）"
            )
            return report
    _render_and_write(conn, root)
    report.changed = True
    return report


__all__ = [
    "HASH_FILE",
    "PLAN_FILE",
    "PLAN_FORMAT_MARKER",
    "PLAN_HEADER",
    "PlanDoc",
    "PlanEntry",
    "PlanReport",
    "parse_plan_doc",
    "plan_path",
    "sync_plan_doc",
    "validate_plan_doc",
    "write_plan_doc",
]
