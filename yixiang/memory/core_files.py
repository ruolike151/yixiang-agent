"""核心三文件：``soul.md`` / ``user.md`` / ``memory.md`` 的读、写、解析、校验。

两条纪律写死在代码里（TECH §7.3、§7.4）：

  1. **格式即接口**：``memory.md`` 的条目是 ``- [12] 内容``，``[12]`` 是 ``facts.id``，
     这是"人改文件"和"改数据库"能对齐的唯一依据；无法解析的行**原样保留**；
  2. **原子写**：临时文件 + ``os.replace()``，UTF-8 无 BOM、``\\n`` 行尾——
     否则 Windows 上 CRLF 混用会制造一大堆假 diff。

``## 待确认`` / ``## 手写笔记`` / ``## 归档`` 里的条目不注入核心区（S4）：
前两者是缓冲区与自由文本，后者是容量淘汰后的停尸房（仍可被检索）。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

# ── 上限（TECH §6.1、§7.3；N-2 待决策项只改这一个常量）──
SOUL_MAX = 8000
USER_MAX = 4000
MEMORY_MAX_LINES = 150
MEMORY_ARCHIVE_MAX_LINES = 300
SKILL_BODY_MAX = 1500

CORE_FILES = ("soul.md", "user.md", "memory.md")
CHAR_LIMITS = {"soul.md": SOUL_MAX, "user.md": USER_MAX}

# soul.md 里"学到的东西"这一段由 update_soul 追加（§7.6）
SOUL_LEARNED_MARKER = "## Learned rules"

MEMORY_HEADER = "# Memory — yixiang 记得的事（直接编辑本文件即可修改记忆，删除整行即删除该记忆）"
MEMORY_FORMAT_MARKER = "<!-- yixiang:format=v1 -->"
MEMORY_DEFAULT_SECTIONS = ("用户", "偏好", "待确认", "手写笔记")
CONFIRM_SECTION = "待确认"
MANUAL_SECTION = "手写笔记"
ARCHIVE_SECTION = "归档"
# 这三段不参与 S4 核心区注入
NON_CORE_SECTIONS = (CONFIRM_SECTION, MANUAL_SECTION, ARCHIVE_SECTION)
# 这两段不参与 facts 同步（归档里的条目仍是 fact）
NON_FACT_SECTIONS = (MANUAL_SECTION,)

SECTION_RE = re.compile(r"^##\s+(\S.*?)\s*$")
ENTRY_RE = re.compile(r"^- \[(\d+)\]\s*(\*)?\s*(.*)$")
PLAIN_ENTRY_RE = re.compile(r"^- (.*)$")


class LimitExceeded(Exception):
    """超过文件上限。工具层把它翻译成可行动的 ``Error`` 文本（D-15）。"""

    def __init__(self, name: str, limit: int, actual: int, unit: str) -> None:
        self.name = name
        self.limit = limit
        self.actual = actual
        self.unit = unit
        super().__init__(f"{name} 超过上限：{actual} {unit} > {limit} {unit}")


# --------------------------------------------------------------------- 基础
def normalize_text(text: str) -> str:
    """统一的落地格式：无 BOM、``\\n`` 行尾、结尾恰好一个换行。"""
    text = (text or "").lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    return text.rstrip("\n") + "\n"


def atomic_write_text(path: Path | str, text: str) -> None:
    """临时文件 + ``os.replace()``：崩在写一半也不会留下半截记忆文件（§7.4.4）。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(normalize_text(text))
    os.replace(tmp, path)


def read_core_files(data_dir: Path | str) -> CoreFiles:
    """一次读三文件；缺文件返回空串（不炸，doctor 负责补齐）。"""
    root = Path(data_dir)
    values = {}
    for name in CORE_FILES:
        path = root / name
        values[name] = path.read_text(encoding="utf-8") if path.is_file() else ""
    return CoreFiles(**values)


def write_core_file(data_dir: Path | str, name: str, text: str) -> None:
    """原子写 + 上限校验（D-15）。超限抛 ``LimitExceeded``，文件一个字节不动。"""
    if name not in CORE_FILES:
        raise ValueError(f"未知核心文件 {name!r}，允许：{CORE_FILES}")
    normalized = normalize_text(text)
    if name in CHAR_LIMITS:
        limit = CHAR_LIMITS[name]
        actual = len(normalized.strip())
        if actual > limit:
            raise LimitExceeded(name, limit, actual, "字符")
    else:
        limit = MEMORY_MAX_LINES
        actual = active_line_count(normalized)
        if actual > limit:
            raise LimitExceeded(name, limit, actual, "行")
    atomic_write_text(Path(data_dir) / name, normalized)


@dataclass(slots=True)
class CoreFiles:
    """三文件的原始文本（``soul`` 含 Learned rules 段）。"""

    soul: str = ""
    user: str = ""
    memory: str = ""

    def get(self, name: str) -> str:
        return getattr(self, name.removesuffix(".md"))


# --------------------------------------------------------------- memory.md 解析
@dataclass(slots=True)
class MemoryEntry:
    """一条记忆行的解析结果。``index`` 是它在整份文件里的行号（0 基）。"""

    index: int
    section: str
    fact_id: int | None
    content: str
    pinned: bool = False

    def render(self) -> str:
        return format_entry(self.fact_id, self.content, self.pinned)


@dataclass(slots=True)
class MemoryDoc:
    """按行保留的 memory.md：raw 行原样留着，entries 是带语义的那部分。"""

    lines: list[str] = field(default_factory=list)
    entries: list[MemoryEntry] = field(default_factory=list)
    sections: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def entry_by_id(self, fact_id: int) -> MemoryEntry | None:
        for entry in self.entries:
            if entry.fact_id == fact_id:
                return entry
        return None

    def entry_at(self, index: int) -> MemoryEntry | None:
        for entry in self.entries:
            if entry.index == index:
                return entry
        return None

    def render(self) -> str:
        return "\n".join(self.lines).rstrip("\n") + "\n"


def format_entry(fact_id: int | None, content: str, pinned: bool = False) -> str:
    content = (content or "").strip()
    if fact_id is None:
        return f"- {content}"
    star = "*" if pinned else ""  # 星号紧跟 `]`，一眼能看出这条被置顶保护
    return f"- [{fact_id}]{star} {content}"


def parse_memory_md(text: str) -> MemoryDoc:
    """逐行解析。解析不了的行 → 原样保留（不报错、不丢内容）。"""
    lines = normalize_text(text).splitlines()
    doc = MemoryDoc(lines=lines)
    section: str | None = None
    seen: set[int] = set()
    for index, line in enumerate(lines):
        match = SECTION_RE.match(line)
        if match:
            section = match.group(1).strip()
            doc.sections.append(section)
            continue
        if section is None:
            continue  # 头部区域：标题 / 注释 / 自由文本，全部原样保留
        match = ENTRY_RE.match(line)
        if match:
            fact_id = int(match.group(1))
            if fact_id in seen:
                doc.warnings.append(f"第 {index + 1} 行：id {fact_id} 重复，按手写笔记原样保留")
                continue
            seen.add(fact_id)
            doc.entries.append(
                MemoryEntry(
                    index=index,
                    section=section,
                    fact_id=fact_id,
                    content=match.group(3).strip(),
                    pinned=bool(match.group(2)),
                )
            )
            continue
        match = PLAIN_ENTRY_RE.match(line)
        if match and match.group(1).strip():
            doc.entries.append(
                MemoryEntry(
                    index=index,
                    section=section,
                    fact_id=None,
                    content=match.group(1).strip(),
                )
            )
    return doc


# ----------------------------------------------------------------- 行集合操作
def section_bounds(lines: list[str], name: str) -> tuple[int, int] | None:
    """返回 ``(标题行, 结束行)``（左闭右开）；没有这段返回 None。"""
    start: int | None = None
    for index, line in enumerate(lines):
        match = SECTION_RE.match(line)
        if not match:
            continue
        if start is not None:
            return start, index
        if match.group(1).strip() == name:
            start = index
    if start is None:
        return None
    return start, len(lines)


def section_names(lines: list[str]) -> list[str]:
    names = []
    for line in lines:
        match = SECTION_RE.match(line)
        if match:
            names.append(match.group(1).strip())
    return names


def insert_lines(lines: list[str], section: str, new_lines: list[str]) -> list[str]:
    """把 ``new_lines`` 插入到指定 section 的末尾（没有这段就新建）。"""
    result = list(lines)
    if not new_lines:
        return result
    bounds = section_bounds(result, section)
    if bounds is None:
        if result and result[-1].strip():
            result.append("")
        result.append(f"## {section}")
        result.extend(new_lines)
        return result
    start, end = bounds
    position = end
    while position > start + 1 and not result[position - 1].strip():
        position -= 1
    return result[:position] + list(new_lines) + result[position:]


def remove_lines(lines: list[str], indices: set[int]) -> list[str]:
    return [line for index, line in enumerate(lines) if index not in indices]


def active_line_count(text: str) -> int:
    """memory.md 的"行数"口径：**不含 ``## 归档``**（归档是停尸房，不该撑爆活跃区）。"""
    lines = normalize_text(text).splitlines()
    bounds = section_bounds(lines, ARCHIVE_SECTION)
    if bounds is None:
        return len(lines)
    start, end = bounds
    return len(lines) - (end - start)


def core_memory_text(text: str) -> str:
    """S4 注入用的文本：剪掉 ``待确认`` / ``手写笔记`` / ``归档`` 三段。"""
    lines = normalize_text(text).splitlines()
    kept: list[str] = []
    skipping = False
    for line in lines:
        match = SECTION_RE.match(line)
        if match:
            skipping = match.group(1).strip() in NON_CORE_SECTIONS
            if skipping:
                continue
        if not skipping:
            kept.append(line)
    return "\n".join(kept).strip()


def validate_memory_md(text: str) -> list[str]:
    """格式校验：返回问题清单（空 = 没问题）。人写的文件坏了要能说清哪一行。"""
    doc = parse_memory_md(text)
    problems: list[str] = list(doc.warnings)
    lines = doc.lines
    if not lines:
        problems.append("error: 文件是空的")
        return problems
    if not lines[0].startswith("# "):
        problems.append("error: 第一行应该是 `# ` 开头的标题")
    if not any(line.strip() == MEMORY_FORMAT_MARKER for line in lines):
        problems.append(f"warning: 缺少格式标记 {MEMORY_FORMAT_MARKER}")
    if ARCHIVE_SECTION not in doc.sections and len(lines) > MEMORY_ARCHIVE_MAX_LINES:
        problems.append(f"warning: 文件 {len(lines)} 行，建议分段（上限 {MEMORY_MAX_LINES} 行）")
    count = active_line_count("\n".join(lines))
    if count > MEMORY_MAX_LINES:
        problems.append(f"error: 活跃区 {count} 行，超过上限 {MEMORY_MAX_LINES} 行")
    bounds = section_bounds(lines, ARCHIVE_SECTION)
    if bounds is not None and bounds[1] - bounds[0] > MEMORY_ARCHIVE_MAX_LINES:
        problems.append(
            f"warning: 归档段 {bounds[1] - bounds[0]} 行，建议人工清理（不会自动删）"
        )
    for entry in doc.entries:
        if entry.section in NON_FACT_SECTIONS:
            continue
        if not entry.content:
            problems.append(f"warning: 第 {entry.index + 1} 行是空条目")
        if len(entry.content) > 200:
            problems.append(f"warning: 第 {entry.index + 1} 行超过 200 字，建议拆开")
    return problems


def new_memory_doc() -> MemoryDoc:
    """一份空白但合法的 memory.md（构建失败时的兜底骨架）。"""
    text = "\n".join(
        [
            MEMORY_HEADER,
            "",
            MEMORY_FORMAT_MARKER,
            "",
            "## 用户",
            "",
            "## 偏好",
            "",
            "## 待确认",
            "",
            "## 手写笔记",
            "",
        ]
    )
    return parse_memory_md(text)


def ensure_memory_file(data_dir: Path | str, *, template_dir: Path | str | None = None) -> Path:
    """保证 ``data/memory.md`` 存在：优先用 ``templates/memory.md``，否则空白骨架。"""
    path = Path(data_dir) / "memory.md"
    if path.is_file():
        return path
    if template_dir is not None:
        source = Path(template_dir) / "memory.md"
        if source.is_file():
            atomic_write_text(path, source.read_text(encoding="utf-8"))
            return path
    atomic_write_text(path, new_memory_doc().render())
    return path


__all__ = [
    "ARCHIVE_SECTION",
    "CHAR_LIMITS",
    "CONFIRM_SECTION",
    "CORE_FILES",
    "CoreFiles",
    "LimitExceeded",
    "MANUAL_SECTION",
    "MEMORY_ARCHIVE_MAX_LINES",
    "MEMORY_DEFAULT_SECTIONS",
    "MEMORY_FORMAT_MARKER",
    "MEMORY_MAX_LINES",
    "MemoryDoc",
    "MemoryEntry",
    "NON_CORE_SECTIONS",
    "NON_FACT_SECTIONS",
    "SKILL_BODY_MAX",
    "SOUL_LEARNED_MARKER",
    "SOUL_MAX",
    "USER_MAX",
    "active_line_count",
    "atomic_write_text",
    "core_memory_text",
    "ensure_memory_file",
    "format_entry",
    "insert_lines",
    "new_memory_doc",
    "normalize_text",
    "parse_memory_md",
    "read_core_files",
    "remove_lines",
    "section_bounds",
    "section_names",
    "validate_memory_md",
    "write_core_file",
]
