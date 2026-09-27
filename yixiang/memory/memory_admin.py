"""对话式记忆治理：``save_memory`` / ``manage_memory`` / ``update_soul`` / ``update_user``。

两条纪律：

  * **yixiang 侧一切修改都要双写**（PART-2 §5 第 3 条）：DB 是权威状态，写完必须
    再渲染回 ``memory.md``；只写一侧就会立刻漂移（``memory verify`` 会抓到）；
  * **失败必须返回可行动的 ``Error:`` 文本**（§9.1），不抛异常给 loop——
    工具抛异常会变成"这一轮工具失败"，而模型需要的是"怎么改参数"。

``update_soul`` 只追加到 ``## Learned rules``：**只加不删**是刻意的设计，
人格条款的删除永远由人手工改文件决定（A-14）。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from yixiang.memory import core_files, episodic, procedural, semantic, sync
from yixiang.memory.core_files import (
    CONFIRM_SECTION,
    MANUAL_SECTION,
    MEMORY_MAX_LINES,
    SOUL_LEARNED_MARKER,
    SOUL_MAX,
    USER_MAX,
    LimitExceeded,
)

SEARCH_LIMIT = 8

# edit 只开放三个精确动作；整份 rewrite 刻意不做（§7.4 人机共治）
EDIT_OPS = ("add", "replace", "remove")
ID_MATCH_RE = re.compile(r"^\[\s*(\d+)\s*\]$")


# ------------------------------------------------------------------ 路径/文本
def _data_dir() -> Path:
    return sync.context().data_dir


def _templates_dir() -> Path | None:
    settings = getattr(semantic.context(), "settings", None)
    path = getattr(settings, "templates_dir", None)
    return Path(path) if path is not None else None


def _read_core(name: str) -> str:
    """读核心文件：``data/`` 优先，缺文件时回落到 ``templates/``（只读兜底）。"""
    path = _data_dir() / name
    if path.is_file():
        return path.read_text(encoding="utf-8")
    templates = _templates_dir()
    if templates is not None and (templates / name).is_file():
        return (templates / name).read_text(encoding="utf-8")
    return ""


def _ok(payload: dict[str, Any]) -> str:
    return json.dumps({"ok": True, **payload}, ensure_ascii=False)


def _error(message: str) -> str:
    return f"Error: {message}"


def _write_back(conn=None) -> None:
    sync.write_memory_doc(conn or semantic.context().conn)


# ------------------------------------------------------------------ save_memory
def save_memory(subject: str, content: str) -> str:
    """工具实现：写入一条持久事实（自动去重）并双写 ``memory.md``。"""
    content = (content or "").strip()
    if not content:
        return _error("content 不能为空：描述一条跨会话仍然成立的事实")
    try:
        fact_id, action = semantic.save_fact(subject, content)
    except Exception as exc:  # 写库失败要如实返回，不能假装记住（§7.9）
        return _error(f"写入失败：{exc}")
    _write_back()
    return json.dumps({"id": fact_id, "action": action}, ensure_ascii=False)


# ------------------------------------------------------------------ manage_memory
def manage_memory(
    action: str = "",
    *,
    kind: str = "",
    id: int | None = None,
    query: str = "",
    content: str = "",
    subject: str = "",
    op: str = "",
    match: str = "",
    section: str = "",
) -> str:
    """工具实现：``search`` / ``update`` / ``delete`` / ``restore`` / ``edit``。"""
    match (action or "").strip().lower():
        case "search" | "list":
            return _search(kind=kind, query=query)
        case "update":
            return _update(id, content=content, subject=subject)
        case "delete":
            return _delete(id)
        case "restore":
            return _restore(id)
        case "edit":
            return _edit(op, section=section, content=content, match=match)
        case "":
            return _error("action 必填：search / update / delete / restore / edit")
        case other:
            return _error(
                f"不支持的 action={other!r}，只接受 search / update / delete / restore / edit"
            )


def _search(*, kind: str = "", query: str = "", limit: int = SEARCH_LIMIT) -> str:
    """带 id 的编号列表——模型要靠这个 id 调 update / delete。"""
    query = (query or "").strip()
    lines: list[str] = []
    if kind not in {"episode"}:
        rows = _search_facts(query, limit)
        if not rows:
            lines.append("（没有匹配的事实记忆）")
        for index, hit in enumerate(rows, start=1):
            lines.append(f"{index}. [{hit.id}] {hit.content}（{hit.subject or '用户'}）")
    if kind not in {"fact"}:
        episodes = _search_episodes(query, min(3, limit))
        if episodes:
            lines.append("情景记忆：")
            for item in episodes:
                lines.append(f"- [{item['happened_at'][:10]}] {item['summary']}")
    if not lines:
        return "（没有匹配的记忆）"
    return "\n".join(lines)


def _search_facts(query: str, limit: int) -> list[semantic.Hit]:
    store = semantic.FactStore(semantic.context())
    if not query:
        return [
            semantic.Hit(
                id=int(row["id"]),
                kind="fact",
                content=str(row["content"]),
                subject=str(row["subject"]),
            )
            for row in store.alive(limit)
        ]
    hits = store.search_fts(query, limit=limit)
    if not hits:
        hits = store.search_like(query, limit=limit)
    return hits


def _search_episodes(query: str, limit: int) -> list[dict[str, Any]]:
    if not query:
        return episodic.list_episodes(limit)
    try:
        hits = episodic.retrieve_episodes(query, k=limit)
    except Exception:  # 嵌入不可用 / 未装配：搜索降级，不影响事实检索
        return []
    return [
        {"happened_at": hit.happened_at, "summary": hit.content, "id": hit.id} for hit in hits
    ]


def _update(fact_id: int | None, *, content: str, subject: str) -> str:
    if fact_id is None:
        return _error("update 需要 id：先调 manage_memory(action='search') 拿到 id")
    content = (content or "").strip()
    if not content:
        return _error("update 需要 content：给出更新后的完整表述")
    store = semantic.FactStore(semantic.context())
    row = store.get(int(fact_id))
    if row is None or int(row["deleted"]):
        return _error(f"id={fact_id} 不存在（或已被删除）；用 action='search' 重新找")
    store.update(
        int(fact_id),
        content=content,
        subject=(subject or str(row["subject"])).strip() or "用户",
        vector=semantic._embed_one(content),
    )
    _write_back()
    return _ok({"id": int(fact_id), "action": "update"})


def _delete(fact_id: int | None) -> str:
    if fact_id is None:
        return _error("delete 需要 id：先调 manage_memory(action='search') 拿到 id")
    store = semantic.FactStore(semantic.context())
    row = store.get(int(fact_id))
    if row is None:
        return _error(f"id={fact_id} 不存在；用 action='search' 重新找")
    if not int(row["deleted"]):
        semantic.soft_delete_fact(int(fact_id))  # 软删 + 双写（可 restore 捞回来）
    return _ok({"id": int(fact_id), "action": "delete"})


def _restore(fact_id: int | None) -> str:
    if fact_id is None:
        return _error("restore 需要 id")
    store = semantic.FactStore(semantic.context())
    row = store.get(int(fact_id))
    if row is None:
        return _error(f"id={fact_id} 不存在：回收站只保留软删过的条目")
    store.restore(int(fact_id), vector=semantic._embed_one(str(row["content"])))
    _write_back()
    return _ok({"id": int(fact_id), "action": "restore"})


# ---------------------------------------------- edit：用户点名的 memory.md 正文
def _edit(op: str = "", *, section: str = "", content: str = "", match: str = "") -> str:
    """用户明确要求时才动 ``memory.md`` 正文：只做 add / replace / remove。

    刻意不做整份 rewrite——整份重排、合并、改写交给人在编辑器里做（§7.4 人机共治），
    工具只提供三个能被定位、能被校验、能被回滚的精确动作。走的是既有的
    ``parse_memory_md`` / ``insert_lines`` / ``remove_lines`` + 原子写，
    写完立刻 ``sync_memory_md`` 让 facts 跟着文件走（带 id 的行被删 = 软删，
    ``manage_memory(action='restore')`` 还能捞回来）。
    """
    op = (op or "").strip().lower()
    if op not in EDIT_OPS:
        return _error(
            f"不支持的 op={op!r}：edit 只接受 add / replace / remove；"
            "整份 rewrite 请人工编辑 memory.md（工具不做）"
        )
    path = core_files.ensure_memory_file(_data_dir(), template_dir=_templates_dir())
    doc = core_files.parse_memory_md(path.read_text(encoding="utf-8"))
    if op == "add":
        return _edit_add(doc, section=section, content=content)
    hits, error = _match_entries(doc, match)
    if error:
        return error
    if len(hits) > 1:
        listing = "\n".join(f"  {hit.index + 1}: {hit.render()}" for hit in hits)
        return _error(
            f"match={match.strip()!r} 命中 {len(hits)} 行，不能猜着改一行；"
            f"请给更长的原文片段：\n{listing}"
        )
    entry = hits[0]
    if op == "remove":
        return _edit_remove(doc, entry)
    return _edit_replace(doc, entry, content=content)


def _edit_add(doc: core_files.MemoryDoc, *, section: str = "", content: str = "") -> str:
    """往指定段末尾加一行；命中的若是事实段，紧接着同步导入成新 fact 并把 id 写回文件。"""
    content = (content or "").strip()
    if not content:
        return _error("content 不能为空：给出要往记忆文件里加的那一行内容")
    target = (section or "").strip() or MANUAL_SECTION
    line = content if content.startswith("- ") else f"- {content}"
    lines = core_files.insert_lines(list(doc.lines), target, [line])
    error = _commit_memory_lines(lines)
    if error:
        return error
    sync.sync_memory_md(semantic.context().conn)
    return _ok({"op": "add", "section": target, "id": _fact_id_for(target, content)})


def _edit_replace(doc: core_files.MemoryDoc, entry: core_files.MemoryEntry, *, content: str) -> str:
    """按定位结果换掉这一行的正文：带 id 的条目 id 不变，库里那条跟着改。"""
    content = (content or "").strip()
    if content.startswith("- "):
        content = content[2:].strip()
    if not content:
        return _error("content 不能为空：给出替换后的正文（只换这一行）")
    lines = list(doc.lines)
    lines[entry.index] = core_files.format_entry(entry.fact_id, content, entry.pinned)
    error = _commit_memory_lines(lines)
    if error:
        return error
    sync.sync_memory_md(semantic.context().conn)
    return _ok({"op": "replace", "id": entry.fact_id, "line": entry.index + 1})


def _edit_remove(doc: core_files.MemoryDoc, entry: core_files.MemoryEntry) -> str:
    """删掉这一行：带 id 的条目在库里变成软删（可 restore），手写行只是文件级删除。"""
    lines = core_files.remove_lines(list(doc.lines), {entry.index})
    error = _commit_memory_lines(lines)
    if error:
        return error
    sync.sync_memory_md(semantic.context().conn)
    return _ok({"op": "remove", "id": entry.fact_id, "line": entry.index + 1})


def _match_entries(
    doc: core_files.MemoryDoc, match: str
) -> tuple[list[core_files.MemoryEntry], str]:
    """定位要改的那一行：``[12]`` 按 fact_id 找，其余按正文子串找。

    两个拒绝口径：没命中 → 让模型去 search 核对原文；命中多行 → 报出全部候选，
    绝不猜一行改掉（猜错的代价是丢掉一条人写的记忆）。
    """
    raw = (match or "").strip()
    if not raw:
        return [], _error("match 必填：给一段原文片段，或 [id] 形式的条目号")
    id_match = ID_MATCH_RE.match(raw)
    if id_match:
        fact_id = int(id_match.group(1))
        hits = [entry for entry in doc.entries if entry.fact_id == fact_id]
        if not hits:
            return [], _error(
                f"记忆文件里没有 [{fact_id}] 这一条；"
                "先用 manage_memory(action=\"search\") 核对 id"
            )
        return hits, ""
    hits = [entry for entry in doc.entries if raw in entry.content]
    if not hits:
        return [], _error(
            f"记忆文件里没有包含 {raw!r} 的行；"
            "先用 manage_memory(action=\"search\") 看准原文，或改给 [id]"
        )
    return hits, ""


def _commit_memory_lines(lines: list[str]) -> str:
    """原子写 + 上限校验：超限整份拒绝、文件一个字节不动；成功返回空串。"""
    try:
        core_files.write_core_file(_data_dir(), "memory.md", "\n".join(lines))
    except LimitExceeded as exc:
        return _error(f"{exc}；请人工精简 memory.md（上限 {MEMORY_MAX_LINES} 行）")
    return ""


def _fact_id_for(section: str, content: str) -> int | None:
    """同步后回读文件，把刚导入那条的 id 找回来（手写笔记段永远是 None）。"""
    if section in core_files.NON_FACT_SECTIONS:
        return None
    text = (_data_dir() / "memory.md").read_text(encoding="utf-8")
    for entry in core_files.parse_memory_md(text).entries:
        if entry.fact_id is not None and entry.section == section and entry.content == content:
            return entry.fact_id
    return None


# ------------------------------------------------------------------ update_soul
def update_soul(rule: str) -> str:
    """只追加一条学到的新规则到 ``## Learned rules``；删不了任何既有条款。"""
    rule = " ".join((rule or "").split())
    if not rule:
        return _error("rule 不能为空；update_soul 只追加，永远不修改或删除已有条款")
    text = _read_core("soul.md")
    line = rule if rule.startswith("- ") else f"- {rule}"
    if SOUL_LEARNED_MARKER in text:
        head, _, tail = text.partition(SOUL_LEARNED_MARKER)
        body = tail
        rendered = f"{head}{SOUL_LEARNED_MARKER}{body.rstrip(chr(10))}\n{line}\n"
    else:
        rendered = f"{text.rstrip()}\n\n{SOUL_LEARNED_MARKER}\n{line}\n"
    try:
        core_files.write_core_file(_data_dir(), "soul.md", rendered)
    except LimitExceeded as exc:
        return _error(f"{exc}；请人工精简 soul.md（上限 {SOUL_MAX} 字符）")
    return _ok({"file": "soul.md", "chars": len(rendered.strip())})


# ------------------------------------------------------------------ update_user
def update_user(section: str, content: str) -> str:
    """往 ``user.md`` 的某个 section 追加一行；超上限（4000 字符）整份拒绝。"""
    section = (section or "").strip()
    content = (content or "").strip()
    if not section:
        return _error("section 必填，例如 身份 / 作息 / 偏好 / 约束 / 沟通方式")
    if not content:
        return _error("content 不能为空")
    text = _read_core("user.md")
    lines = core_files.normalize_text(text).splitlines()
    line = content if content.startswith("- ") else f"- {content}"
    rendered = "\n".join(core_files.insert_lines(lines, section, [line]))
    try:
        core_files.write_core_file(_data_dir(), "user.md", rendered)
    except LimitExceeded as exc:
        return _error(f"{exc}；请人工精简 user.md（上限 {USER_MAX} 字符）")
    return _ok({"file": "user.md", "section": section})


# ------------------------------------------------------------------ create_skill
def create_skill(
    slug: str,
    name: str,
    description: str,
    triggers: str | list[str],
    body: str,
    confirm: bool = False,
) -> str:
    """写 ``data/skills/<slug>/SKILL.md``；不覆盖已有、必须 ``confirm=true``。"""
    settings = getattr(semantic.context(), "settings", None)
    directory = getattr(settings, "skills_dir", None) or (_data_dir() / "skills")
    result = procedural.install(
        slug,
        name,
        description,
        triggers,
        body,
        confirm=bool(confirm),
        directory=directory,
    )
    if not result.get("ok"):
        return _error(str(result.get("error") or "创建技能失败"))
    return json.dumps(result, ensure_ascii=False)


__all__ = [
    "CONFIRM_SECTION",
    "create_skill",
    "manage_memory",
    "save_memory",
    "update_soul",
    "update_user",
]
