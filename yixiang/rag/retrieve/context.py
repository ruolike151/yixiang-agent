"""全局装配与这一轮的状态（``configure`` / ``reset`` / ``trace_info``）。

``_context`` 是模块级单例，所以**只有这里有它**：别的模块读它一律走 ``current()``，
否则 ``from ... import _context`` 会拿到装配前的 ``None``。``mark_unavailable`` 是
旧 ``_mark_unavailable`` 去掉下划线搬来这里——它写的正是全局 ``warnings``，必须跟着
``_context`` 走。"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from yixiang.errors import E_EMBED_UNAVAILABLE
from yixiang.rag.embed import build_embedder
from yixiang.runtime.models import Clock, SystemClock


# ------------------------------------------------------------------ 检索上下文
@dataclass(slots=True)
class RetrievalContext:
    """组装根注入的检索上下文（一次 configure，全局只读）。"""

    conn: sqlite3.Connection | None = None
    data_dir: Path = field(default_factory=lambda: Path("data"))
    clock: Clock = field(default_factory=SystemClock)
    settings: Any = None
    embedder: Any = None
    # ok（这一轮真跑过嵌入）/ unavailable（跑失败，已降级）/ skipped（还没跑）
    embed: str = "skipped"
    vec_ready: bool = False
    warnings: list[str] = field(default_factory=list)
    reindex_required: str = ""


_context: RetrievalContext | None = None


def configure(
    conn: sqlite3.Connection,
    *,
    data_dir: Path | str,
    clock: Clock | None = None,
    settings: Any = None,
    embedder: Any = None,
) -> RetrievalContext:
    """装配检索：注入连接、data_dir、时钟与嵌入后端（缺了就纯 FTS5 跑）。"""
    global _context
    resolved = embedder
    if resolved is None and settings is not None:
        try:
            resolved = build_embedder(settings)
        except Exception:  # 后端没实现 / 依赖缺失：降级，不阻塞启动（D-24）
            resolved = None
    _context = RetrievalContext(
        conn=conn,
        data_dir=Path(data_dir),
        clock=clock or SystemClock(),
        settings=settings,
        embedder=resolved,
    )
    return _context


def reset() -> None:
    global _context
    _context = None


def is_configured() -> bool:
    return _context is not None


def current() -> RetrievalContext:
    """取当前上下文；未装配就调用冻结函数是编程错误，直接报错。"""
    if _context is None:
        raise RuntimeError(
            "RAG 子系统尚未装配：先调用 yixiang.rag.configure(conn, data_dir=...)"
        )
    return _context


def ensure_configured() -> RetrievalContext:
    return current()


def clear_warnings() -> None:
    """每轮开头调用：让 trace 里的降级标记只反映**这一轮**。"""
    ctx = _context
    if ctx is None:
        return
    ctx.warnings.clear()
    ctx.embed = "skipped"
    ctx.reindex_required = ""


def trace_info() -> dict[str, Any]:
    """给 trace 的 ``rag`` 字段（D-24 要求 ``E_EMBED_UNAVAILABLE`` 可查）。"""
    ctx = _context
    if ctx is None:
        return {"embed": "skipped", "vec_ready": False}
    info: dict[str, Any] = {"embed": ctx.embed, "vec_ready": bool(ctx.vec_ready)}
    if ctx.warnings:
        info["errors"] = list(dict.fromkeys(ctx.warnings))
    if ctx.reindex_required:
        info["reindex_required"] = ctx.reindex_required
    return info


def mark_unavailable(reason: str) -> None:
    ctx = _context
    if ctx is None:
        return
    ctx.embed = "unavailable"
    if E_EMBED_UNAVAILABLE not in ctx.warnings:
        ctx.warnings.append(E_EMBED_UNAVAILABLE)
    if reason and reason not in ctx.warnings:
        ctx.warnings.append(reason)
