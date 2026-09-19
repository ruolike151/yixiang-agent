"""记忆子系统（PART 2 / TECH-DESIGN §7）。

三个层次，各管一件事：

  * **核心区**（``core_files.py``）——``data/*.md``，每轮全量注入，人机共治；
  * **检索区**（``semantic.py`` / ``episodic.py``）——``state.db``，门控命中才注入；
  * **程序性**（``procedural.py``）——``skills/<slug>/SKILL.md``，关键词匹配注入。

模块级函数（``retrieve_memory`` / ``save_fact`` / ``sync_memory_md`` ...）是 PART 3/4
依赖的冻结签名，它们本身不接连接——连接与 data_dir 由组装根通过 ``configure()``
注入，避免"每个函数都要多传两个参数"把签名撑变形。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from yixiang.runtime.models import Clock, SystemClock


@dataclass(slots=True)
class MemoryContext:
    """组装根注入的记忆上下文（一次 configure，全局只读）。"""

    conn: sqlite3.Connection
    data_dir: Path
    clock: Clock
    store: Any = None
    vec_ready: bool = False


_context: MemoryContext | None = None


def configure(
    conn: sqlite3.Connection,
    *,
    data_dir: Path | str,
    clock: Clock | None = None,
    settings: Any = None,
    embedder: Any = None,
    is_same: Any = None,
) -> MemoryContext:
    """装配记忆子系统：注入连接、data_dir、时钟与可选的假嵌入后端。"""
    from yixiang.memory import episodic, semantic, sync

    clock = clock or SystemClock()
    data_dir = Path(data_dir)
    store = semantic.configure(
        conn,
        data_dir=data_dir,
        clock=clock,
        settings=settings,
        embedder=embedder,
        is_same=is_same,
    )
    sync.configure(data_dir=data_dir, clock=clock)
    episodic.configure(conn, data_dir=data_dir, clock=clock)
    global _context
    _context = MemoryContext(
        conn=conn,
        data_dir=data_dir,
        clock=clock,
        store=store,
        vec_ready=bool(semantic.context().vec_ready),
    )
    return _context


def current() -> MemoryContext:
    """取当前上下文；未 configure 就调用冻结函数是编程错误，直接报错。"""
    if _context is None:
        raise RuntimeError(
            "记忆子系统尚未装配：先调用 yixiang.memory.configure(conn, data_dir=...)"
        )
    return _context


def is_configured() -> bool:
    return _context is not None


def reset() -> None:
    """测试用：清掉全局上下文（避免用例之间串库）。"""
    from yixiang.memory import episodic, semantic, sync

    semantic.reset()
    sync.reset()
    episodic.reset()
    global _context
    _context = None


# ------------------------------------------------------------------ 冻结签名
# PART 3/4 按 ``from yixiang.memory import retrieve_memory`` 使用（PART-2 §4）。
# 这里做一层转发：签名保持不变，装配仍由 ``configure()`` 负责。
def retrieve_memory(query: str, top_k: int = 5, ep_k: int = 3) -> Any:
    from yixiang.memory.semantic import retrieve_memory as impl

    return impl(query, top_k=top_k, ep_k=ep_k)


def save_fact(subject: str, content: str) -> tuple[int, str]:
    from yixiang.memory.semantic import save_fact as impl

    return impl(subject, content)


def soft_delete_fact(fact_id: int) -> None:
    from yixiang.memory.semantic import soft_delete_fact as impl

    impl(fact_id)


def preprocess_for_fts(text: str) -> str:
    from yixiang.memory.semantic import preprocess_for_fts as impl

    return impl(text)


def rrf(rankings: list[list[Any]], k: int = 60) -> list[Any]:
    from yixiang.memory.semantic import rrf as impl

    return impl(rankings, k=k)


__all__ = [
    "MemoryContext",
    "configure",
    "current",
    "is_configured",
    "preprocess_for_fts",
    "reset",
    "retrieve_memory",
    "rrf",
    "save_fact",
    "soft_delete_fact",
]
