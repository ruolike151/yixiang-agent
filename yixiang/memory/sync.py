"""``memory.md`` ⟷ ``facts`` 双向同步（TECH §7.4）——本项目最容易出 bug 的一处。

四条纪律（§7.4.4、PART-2 §5）：

  1. **文件为准**：人删一行 = 软删 fact（不注入、不检索、可恢复）；人改一行 =
     更新 fact；人加无 id 行 = 导入新 fact 并回写 id；换了 section = 更新 subject。
  2. **条目级 id**：``- [12] 内容`` 里的 12 就是 ``facts.id``，这是"人改文件"与
     "改数据库"能对齐的唯一锚点。没有它，两侧各自增删后必然丢数据。
  3. **先 DB 后文件**：DB 变更在单个事务里提交，再原子写文件（``.tmp`` + ``os.replace``）。
     DB 失败整体回滚；文件写失败下次启动因 sha256 不匹配重跑，最终收敛。
  4. **幂等**：每次写完文件把 sha256 落到 ``data/.memory_md.sha256``；同步先比哈希，
     一致直接返回（零开销）。

``## 手写笔记`` 里的行**不入库**（它是自由文本，不是事实）；无法解析的行原样保留。
"""

from __future__ import annotations

import hashlib
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from yixiang.memory import core_files, semantic
from yixiang.memory.core_files import (
    ARCHIVE_SECTION,
    CONFIRM_SECTION,
    ENTRY_RE,
    MANUAL_SECTION,
    MEMORY_FORMAT_MARKER,
    MEMORY_HEADER,
    MEMORY_MAX_LINES,
    NON_FACT_SECTIONS,
    PLAIN_ENTRY_RE,
    SECTION_RE,
    atomic_write_text,
    format_entry,
    parse_memory_md,
)
from yixiang.runtime.models import Clock, SystemClock, to_local_iso

HASH_FILE = ".memory_md.sha256"
MEMORY_FILE = "memory.md"

# 待确认区条目多少天没人管就归档（§7.11 第 3 条）
PENDING_ARCHIVE_DAYS = 30
PENDING_ARCHIVE_SUFFIX = "(未确认，已归档)"

# 文件里的 section 顺序 = 渲染顺序（归档永远最后，它是停尸房）
SECTION_ORDER = ("用户", "偏好", CONFIRM_SECTION, MANUAL_SECTION, ARCHIVE_SECTION)
# 可容纳 facts 的 section（手写笔记不参与同步）
FACT_SECTIONS = ("用户", "偏好", CONFIRM_SECTION, ARCHIVE_SECTION)
# subject → 落到哪个 section（多对一：项目/其他都进"用户"）
SUBJECT_SECTION = {
    "用户": "用户",
    "偏好": "偏好",
    "项目": "用户",
    "其他": "用户",
    CONFIRM_SECTION: CONFIRM_SECTION,
    ARCHIVE_SECTION: ARCHIVE_SECTION,
}

# 一把锁保护"完整同步 + 所有文件写"（§7.4.4 的并发要求）。
# 这里用 RLock 而不是 asyncio.Lock：同步入口是同步函数，线程锁才是正确原语。
_LOCK = threading.RLock()


@dataclass(slots=True)
class SyncContext:
    data_dir: Path
    clock: Clock


@dataclass(slots=True)
class SyncReport:
    """一次同步的结果（``memory sync`` / 启动同步共用）。"""

    changed: bool = False
    skipped: bool = False
    inserted: int = 0
    updated: int = 0
    deleted: int = 0
    restored: int = 0
    archived: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def touched(self) -> bool:
        return bool(
            self.inserted or self.updated or self.deleted or self.restored or self.archived
        )

    def summary(self) -> str:
        if self.skipped:
            return "memory.md 未变化（sha256 一致），跳过同步"
        if not self.changed and not self.touched:
            return "无需同步"
        return (
            f"新增 {self.inserted} · 更新 {self.updated} · 软删 {self.deleted} · "
            f"复活 {self.restored} · 归档 {self.archived}"
        )


_context: SyncContext | None = None


def configure(*, data_dir: Path | str, clock: Clock | None = None) -> SyncContext:
    global _context

    _context = SyncContext(data_dir=Path(data_dir), clock=clock or SystemClock())
    return _context


def reset() -> None:
    global _context
    _context = None


def context() -> SyncContext:
    if _context is None:
        raise RuntimeError("同步模块未装配：先调用 yixiang.memory.configure(...)")
    return _context


def _now() -> str:
    return to_local_iso(context().clock.now())


# ------------------------------------------------------------------ 哈希快速路径
def memory_path() -> Path:
    return Path(context().data_dir) / MEMORY_FILE


def file_hash(path: Path | None = None) -> str:
    target = path or memory_path()
    if not target.is_file():
        return ""
    return hashlib.sha256(target.read_bytes()).hexdigest()


def _read_stored_hash() -> str:
    path = Path(context().data_dir) / HASH_FILE
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8").strip()


def _write_stored_hash() -> None:
    digest = file_hash()
    path = Path(context().data_dir) / HASH_FILE
    if digest:
        atomic_write_text(path, digest + "\n")


def should_sync(conn: sqlite3.Connection) -> bool:  # noqa: ARG001 - 签名由文档冻结
    """文件 sha256 与上次落盘不一致才需要完整比对（§7.4.2）。"""
    if not memory_path().is_file():
        return False
    stored = _read_stored_hash()
    if not stored:
        return True
    return stored != file_hash()


# ------------------------------------------------------------------ 文件 → DB
def sync_memory_md(conn: sqlite3.Connection) -> SyncReport:
    """完整比对一次：文件是权威，把人的增删改落到 DB，再规范化写回文件。"""
    report = SyncReport()
    path = memory_path()
    if not path.is_file():
        report.warnings.append(f"{MEMORY_FILE} 不存在，跳过同步")
        report.skipped = True
        return report
    if not should_sync(conn):
        report.skipped = True
        return report

    with _LOCK:
        text = path.read_text(encoding="utf-8")
        doc = parse_memory_md(text)
        report.warnings.extend(doc.warnings)
        changed_ids = _apply_file_to_db(conn, doc, report)
        _maintain(conn, report)
        lines = _render(conn, doc.lines)
        atomic_write_text(path, "\n".join(lines))
        _write_stored_hash()
        report.changed = True
    if changed_ids:
        semantic.rebuild_facts_fts(conn)
        _reindex_vectors(conn, changed_ids)
    return report


def _apply_file_to_db(
    conn: sqlite3.Connection, doc: core_files.MemoryDoc, report: SyncReport
) -> list[int]:
    """逐条执行 §7.4.3 的规则；所有写操作在**一个事务**里（失败整体回滚）。"""
    seen: set[int] = set()
    changed: list[int] = []
    now = _now()
    with conn:
        for entry in doc.entries:
            if entry.section in NON_FACT_SECTIONS:
                continue  # 手写笔记：自由文本，不入库
            content = (entry.content or "").strip()
            section = entry.section if entry.section in FACT_SECTIONS else "用户"
            if entry.fact_id is None:
                subject = SUBJECT_SECTION.get(section, "用户")
                cursor = conn.execute(
                    """
                    INSERT INTO facts(subject, content, source, pinned, created_at,
                                      updated_at, last_used_at)
                    VALUES (?, ?, 'file', ?, ?, ?, ?)
                    """,
                    (subject, content, int(entry.pinned), now, now, now),
                )
                new_id = int(cursor.lastrowid)
                seen.add(new_id)  # 别让下面的"文件里没有就软删"清扫把刚导入的条目删掉
                report.inserted += 1
                changed.append(new_id)
                report.warnings.append(
                    f"第 {entry.index + 1} 行：无 id 的手写条目已导入为 fact #{new_id}"
                )
                continue

            fact_id = int(entry.fact_id)
            seen.add(fact_id)
            row = conn.execute("SELECT * FROM facts WHERE id = ?", (fact_id,)).fetchone()
            if row is None:
                # 回收站被物理清理后手写回来：显式 id 重插
                conn.execute(
                    """
                    INSERT INTO facts(id, subject, content, source, pinned, created_at,
                                      updated_at, last_used_at)
                    VALUES (?, ?, ?, 'file', ?, ?, ?, ?)
                    """,
                    (
                        fact_id,
                        SUBJECT_SECTION.get(section, "用户"),
                        content,
                        int(entry.pinned),
                        now,
                        now,
                        now,
                    ),
                )
                report.inserted += 1
                changed.append(fact_id)
                continue

            subject = _subject_for(row["subject"], section)
            if row["deleted"]:
                conn.execute(
                    "UPDATE facts SET deleted = 0, deleted_at = NULL, subject = ?, content = ?,"
                    " pinned = ?, updated_at = ? WHERE id = ?",
                    (subject, content, int(entry.pinned), now, fact_id),
                )
                report.restored += 1
                changed.append(fact_id)
                continue

            if (
                str(row["content"]).strip() != content
                or str(row["subject"]) != subject
                or int(row["pinned"]) != int(entry.pinned)
            ):
                conn.execute(
                    "UPDATE facts SET subject = ?, content = ?, pinned = ?, updated_at = ?"
                    " WHERE id = ?",
                    (subject, content, int(entry.pinned), now, fact_id),
                )
                report.updated += 1
                changed.append(fact_id)

        # 库中存活但文件里已不存在 → 软删（可恢复，§7.4.3）
        for row in conn.execute("SELECT id FROM facts WHERE deleted = 0 ORDER BY id").fetchall():
            fact_id = int(row["id"])
            if fact_id in seen:
                continue
            conn.execute(
                "UPDATE facts SET deleted = 1, deleted_at = ? WHERE id = ?", (now, fact_id)
            )
            report.deleted += 1
            changed.append(fact_id)
    return changed


def _subject_for(current: str, section: str) -> str:
    """section → subject；只有当现有 subject 不落在这个 section 时才改写它。

    这样 ``save_memory(subject="项目")`` 写进"用户"段后在同步里不会被抹成"用户"
    （多对一是刻意的），但人把一条从"偏好"挪到"用户"段时 subject 会跟着变。
    """
    if SUBJECT_SECTION.get(current, "用户") == section:
        return current
    return SUBJECT_SECTION.get(section, "用户")


# ------------------------------------------------------------------ 归档维护
def _maintain(conn: sqlite3.Connection, report: SyncReport) -> None:
    ctx = context()
    now = ctx.clock.now()
    moves: list[tuple[int, str]] = []
    rows = conn.execute(
        "SELECT id, content, updated_at FROM facts WHERE deleted = 0 AND subject = ?",
        (CONFIRM_SECTION,),
    ).fetchall()
    for row in rows:
        when = _parse(str(row["updated_at"] or ""))
        if when is None or (now - when).days < PENDING_ARCHIVE_DAYS:
            continue
        content = str(row["content"])
        if PENDING_ARCHIVE_SUFFIX not in content:
            content = f"{content} {PENDING_ARCHIVE_SUFFIX}"
        moves.append((int(row["id"]), content))
    if moves:
        stamp = to_local_iso(now)
        with conn:
            for fact_id, content in moves:
                conn.execute(
                    "UPDATE facts SET subject = ?, content = ?, updated_at = ? WHERE id = ?",
                    (ARCHIVE_SECTION, content, stamp, fact_id),
                )
        report.archived += len(moves)
        _reindex_vectors(conn, [fact_id for fact_id, _ in moves])
    _archive_overflow(conn, report)


def _archive_overflow(conn: sqlite3.Connection, report: SyncReport) -> int:
    """活跃区超 150 行时，按 §7.11 的排序键把尾部条目移进 ``## 归档``。"""
    moved = 0
    while True:
        lines = _render(conn, _read_lines())
        if core_files.active_line_count("\n".join(lines)) <= MEMORY_MAX_LINES:
            break
        victim = _weakest(conn)
        if victim is None:
            report.warnings.append(
                f"活跃区超过 {MEMORY_MAX_LINES} 行，但没有可归档的无置顶条目"
            )
            break
        with conn:
            conn.execute(
                "UPDATE facts SET subject = ?, updated_at = ? WHERE id = ?",
                (ARCHIVE_SECTION, _now(), victim),
            )
        report.archived += 1
        moved += 1
    return moved


def _weakest(conn: sqlite3.Connection) -> int | None:
    """排序键：pinned 降序 → last_used_at 降序 → updated_at 降序；返回尾部那条。"""
    rows = conn.execute(
        "SELECT id, pinned, last_used_at, updated_at FROM facts"
        " WHERE deleted = 0 AND subject IN (?, ?, ?)",
        ("用户", "偏好", CONFIRM_SECTION),
    ).fetchall()
    candidates = [row for row in rows if not int(row["pinned"])]
    if not candidates:
        return None
    weakest = min(
        candidates,
        key=lambda row: (
            str(row["last_used_at"] or ""),
            str(row["updated_at"] or ""),
            int(row["id"]),
        ),
    )
    return int(weakest["id"])


def _parse(value: str) -> datetime | None:
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else moment.astimezone()


# ------------------------------------------------------------------ DB → 文件
def write_memory_doc(conn: sqlite3.Connection) -> SyncReport:
    """把 DB 的权威状态渲染回 ``memory.md``（工具写入路径的第二跳）。"""
    ctx = context()
    ctx.data_dir.mkdir(parents=True, exist_ok=True)
    report = SyncReport()
    with _LOCK:
        _maintain(conn, report)
        lines = _render(conn, _read_lines())
        atomic_write_text(memory_path(), "\n".join(lines))
        _write_stored_hash()
        report.changed = True
    return report


def _read_lines() -> list[str]:
    path = memory_path()
    if not path.is_file():
        return []
    return path.read_text(encoding="utf-8").splitlines()


def _render(conn: sqlite3.Connection, base_lines: list[str] | None) -> list[str]:
    """按 DB 渲染整份文件；头部 / 手写笔记 / 未知段 / 无法解析的行原样保留。"""
    base = list(base_lines or [])
    groups: dict[str, list[sqlite3.Row]] = {name: [] for name in FACT_SECTIONS}
    for row in conn.execute("SELECT * FROM facts WHERE deleted = 0 ORDER BY id").fetchall():
        group = SUBJECT_SECTION.get(str(row["subject"]), "用户")
        groups.setdefault(group, []).append(row)
    for name in FACT_SECTIONS:
        groups[name].sort(key=_strength, reverse=True)

    out = list(_header_lines(base))
    for name in ("用户", "偏好", CONFIRM_SECTION):
        out.append(f"## {name}")
        out.extend(format_entry(int(row["id"]), str(row["content"]), bool(row["pinned"]))
                   for row in groups[name])
        out.extend(_free_lines(base, name))
        out.append("")
    out.append(f"## {MANUAL_SECTION}")
    out.extend(_section_body(base, MANUAL_SECTION))
    out.append("")
    for name, body in _extra_sections(base):
        out.append(f"## {name}")
        out.extend(body)
        out.append("")
    out.append(f"## {ARCHIVE_SECTION}")
    out.extend(format_entry(int(row["id"]), str(row["content"]), bool(row["pinned"]))
               for row in groups[ARCHIVE_SECTION])
    out.extend(_free_lines(base, ARCHIVE_SECTION))
    out.append("")
    return out


def _strength(row: sqlite3.Row) -> tuple[int, str, str, int]:
    return (
        int(row["pinned"]),
        str(row["last_used_at"] or ""),
        str(row["updated_at"] or ""),
        int(row["id"]),
    )


def _header_lines(base: list[str]) -> list[str]:
    head: list[str] = []
    for line in base:
        if SECTION_RE.match(line):
            break
        if line.strip() == MEMORY_FORMAT_MARKER:
            continue
        head.append(line.rstrip())
    while head and not head[-1].strip():
        head.pop()
    if not head or not head[0].strip().startswith("# "):
        head = [MEMORY_HEADER, *[line for line in head if line.strip()]]
    return [*head, "", MEMORY_FORMAT_MARKER, ""]


def _section_body(base: list[str], name: str) -> list[str]:
    bounds = core_files.section_bounds(base, name)
    if bounds is None:
        return []
    body = list(base[bounds[0] + 1 : bounds[1]])
    while body and not body[-1].strip():
        body.pop()
    return body


def _free_lines(base: list[str], name: str) -> list[str]:
    """section 内既不是带 id 条目也不是手工条目的行（注释、引用……）原样保留。"""
    return [
        line
        for line in _section_body(base, name)
        if line.strip() and not ENTRY_RE.match(line) and not PLAIN_ENTRY_RE.match(line)
    ]


def _extra_sections(base: list[str]) -> list[tuple[str, list[str]]]:
    """文件中不在固定顺序里的自定义 section，原样保留（排在归档之前）。"""
    extras: list[tuple[str, list[str]]] = []
    names = [match.group(1).strip() for line in base if (match := SECTION_RE.match(line))]
    for name in names:
        if name in SECTION_ORDER or any(name == seen for seen, _ in extras):
            continue
        extras.append((name, _section_body(base, name)))
    return extras


def _reindex_vectors(conn: sqlite3.Connection, fact_ids: list[int]) -> None:
    """同步只重算被改动条目的向量；嵌入不可用则保持旧索引（检索会按 deleted 过滤）。"""
    ids = sorted(set(fact_ids))
    if not ids:
        return
    if not semantic.ensure_vec_tables(conn):
        return
    for fact_id in ids:
        row = conn.execute(
            "SELECT id, content, deleted FROM facts WHERE id = ?", (fact_id,)
        ).fetchone()
        with conn:
            conn.execute(f"DELETE FROM {semantic.VEC_FACTS} WHERE rowid = ?", (fact_id,))
        if row is None or row["deleted"]:
            continue
        vector = semantic._embed_one(str(row["content"]))
        if vector is None:
            continue
        if not semantic.ensure_vec_tables(conn, dim=len(vector)):
            continue
        with conn:
            conn.execute(
                f"INSERT INTO {semantic.VEC_FACTS}(rowid, embedding) VALUES (?, ?)",
                (fact_id, semantic.to_blob(vector)),
            )


# ------------------------------------------------------------------ 三方对账
def verify(conn: sqlite3.Connection) -> list[str]:
    """文件 / DB 存活条目 / 索引三方对账（``yixiang memory verify``）。"""
    problems: list[str] = []
    path = memory_path()
    if not path.is_file():
        return [f"error: {MEMORY_FILE} 不存在"]
    text = path.read_text(encoding="utf-8")
    doc = parse_memory_md(text)
    file_entries = {
        entry.fact_id: entry
        for entry in doc.entries
        if entry.fact_id is not None and entry.section not in NON_FACT_SECTIONS
    }
    alive = {
        int(row["id"]): row
        for row in conn.execute("SELECT * FROM facts WHERE deleted = 0 ORDER BY id").fetchall()
    }
    for fact_id, row in alive.items():
        entry = file_entries.get(fact_id)
        if entry is None:
            problems.append(f"漂移：fact #{fact_id} 在库中存活但 {MEMORY_FILE} 里没有")
        elif entry.content.strip() != str(row["content"]).strip():
            problems.append(
                f"漂移：fact #{fact_id} 内容不一致"
                f"（文件 {entry.content!r} / 库 {str(row['content'])!r}）"
            )
    for fact_id in file_entries:
        if fact_id in alive:
            continue
        row = conn.execute("SELECT deleted FROM facts WHERE id = ?", (fact_id,)).fetchone()
        state = "已软删" if row else "在库中不存在"
        problems.append(f"漂移：{MEMORY_FILE} 里的 #{fact_id} {state}")
    try:
        indexed = int(conn.execute("SELECT COUNT(*) AS n FROM facts_fts").fetchone()["n"])
    except sqlite3.OperationalError:
        indexed = -1
    if indexed >= 0 and indexed != len(alive):
        problems.append(
            f"漂移：FTS 索引 {indexed} 行 vs 存活 fact {len(alive)} 条（跑 memory rebuild）"
        )
    problems.extend(core_files.validate_memory_md(text))
    return problems


__all__ = [
    "HASH_FILE",
    "MEMORY_FILE",
    "SyncContext",
    "SyncReport",
    "configure",
    "context",
    "file_hash",
    "memory_path",
    "reset",
    "should_sync",
    "sync_memory_md",
    "verify",
    "write_memory_doc",
]
