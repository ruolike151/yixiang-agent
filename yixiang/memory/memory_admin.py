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
from pathlib import Path
from typing import Any

from yixiang.memory import core_files, episodic, procedural, semantic, sync
from yixiang.memory.core_files import (
    CONFIRM_SECTION,
    SOUL_LEARNED_MARKER,
    SOUL_MAX,
    USER_MAX,
    LimitExceeded,
)

SEARCH_LIMIT = 8


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
) -> str:
    """工具实现：``search`` / ``update`` / ``delete`` / ``restore`` 四个动作。"""
    match (action or "").strip().lower():
        case "search" | "list":
            return _search(kind=kind, query=query)
        case "update":
            return _update(id, content=content, subject=subject)
        case "delete":
            return _delete(id)
        case "restore":
            return _restore(id)
        case "":
            return _error("action 必填：search / update / delete / restore")
        case other:
            return _error(f"不支持的 action={other!r}，只接受 search / update / delete / restore")


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
