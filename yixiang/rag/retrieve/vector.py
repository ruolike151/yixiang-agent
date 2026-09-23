"""嵌入与向量索引（``embed_texts`` 带超时 / ``ensure_media_vec`` / ``reindex_media``）。

``embed_texts`` 是本包唯一等外部后端的地方：**整轮对话的无限等待点**就是它，
所以它的超时（``EMBED_TIMEOUT_FALLBACK``，真值取 ``Settings.embed_timeout``）与
降级标记都写在这一层，改动前先读 Task 24 的失败用例。"""

from __future__ import annotations

import sqlite3
from typing import Any

from yixiang import db
from yixiang.rag.embed import (
    EMBED_DIM_META,
    EMBED_MODEL_META,
    ReindexRequired,
    embed_with_cache,
    to_blob,
)
from yixiang.rag.retrieve.context import current, mark_unavailable
from yixiang.rag.retrieve.text import rebuild_media_fts, row_embed_text

# 向量表与分词版本
VEC_MEDIA = "media_vec"


# ------------------------------------------------------------------ 索引维护
def _dim_of(embedder: Any) -> int:
    if embedder is None:
        return 0
    try:
        return int(getattr(embedder, "dim", 0) or 0)
    except (TypeError, ValueError):
        return 0


def ensure_media_vec(conn: sqlite3.Connection, *, dim: int, model: str) -> bool:
    """建 ``media_vec``（sqlite-vec 不可用 → False，全链路降级纯 FTS5）。

    模型或维度与 ``meta`` 对不上就抛 ``ReindexRequired``：静默混用旧向量会得到
    "看起来能跑、排名全是乱的"，比直接失败更糟（§8.2）。
    """
    if not db.load_sqlite_vec(conn):
        return False
    dimensions = int(dim or 0)
    if dimensions <= 0:
        return False
    stored_dim = db.get_meta(conn, EMBED_DIM_META)
    stored_model = db.get_meta(conn, EMBED_MODEL_META)
    if stored_dim and int(stored_dim) != dimensions:
        raise ReindexRequired(
            f"向量维度不一致（库里 {stored_dim}，当前 {dimensions}）："
            "请跑 `yixiang rag reindex` 重建索引"
        )
    if stored_model and stored_model != model:
        raise ReindexRequired(
            f"嵌入模型不一致（库里 {stored_model}，当前 {model}）："
            "请跑 `yixiang rag reindex` 重建索引"
        )
    with conn:
        conn.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS {VEC_MEDIA} USING vec0("
            f"rowid INTEGER PRIMARY KEY, embedding float[{dimensions}] distance_metric=cosine)"
        )
    if not stored_dim:
        db.set_meta(conn, EMBED_DIM_META, str(dimensions))
    if not stored_model:
        db.set_meta(conn, EMBED_MODEL_META, str(model))
    return True


EMBED_TIMEOUT_FALLBACK = 20.0


def embed_texts(texts: list[str], *, batch: int = 32) -> list[list[float]] | None:
    """带缓存的批量嵌入；不可用（含超时）返回 ``None`` 并记降级标记（D-24 + Task 24）。"""
    ctx = current()
    if not texts:
        return []
    if ctx.embedder is None or ctx.conn is None:
        mark_unavailable("没有可用的嵌入后端")
        return None
    timeout = float(getattr(ctx.settings, "embed_timeout", None) or EMBED_TIMEOUT_FALLBACK)
    try:
        vectors = embed_with_cache(
            ctx.conn, ctx.embedder, texts, batch=batch, timeout=timeout
        )
    except Exception as exc:  # 任何嵌入失败都降级，绝不让检索炸掉（§8.2）
        mark_unavailable(str(exc))
        return None
    if len(vectors) != len(texts):
        mark_unavailable(f"嵌入数量不匹配（{len(vectors)} != {len(texts)}）")
        return None
    ctx.embed = "ok"
    return vectors


def index_media_vectors(
    conn: sqlite3.Connection, media_ids: list[int], *, batch: int = 32
) -> int:
    """给若干部作品建/更新向量（``media_vec``）；嵌入不可用返回 0（不阻塞入库）。"""
    ids = [int(value) for value in media_ids]
    if not ids:
        return 0
    embedded = _embed_media_rows(conn, ids, batch=batch)
    if embedded is None:
        return 0
    return _write_embedded_media(conn, *embedded)


def _embed_media_rows(
    conn: sqlite3.Connection, ids: list[int], *, batch: int
) -> tuple[list[sqlite3.Row], list[list[float]]] | None:
    """取行 + 算向量；嵌入不可用返回 ``None``，调用方据此**保持原状**。"""
    marks = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT id, title, title_zh, mtype, genres, synopsis FROM media WHERE id IN ({marks}) "
        "ORDER BY id",
        ids,
    ).fetchall()
    if not rows:
        return None
    vectors = embed_texts([row_embed_text(row) for row in rows], batch=batch)
    if vectors is None:
        return None
    return rows, vectors


def _write_embedded_media(
    conn: sqlite3.Connection, rows: list[sqlite3.Row], vectors: list[list[float]]
) -> int:
    ctx = current()
    written = write_media_vectors(
        conn,
        [(int(row["id"]), vector) for row, vector in zip(rows, vectors, strict=False)],
        model=str(ctx.embedder.model),
        dim=len(vectors[0]),
    )
    ctx.vec_ready = bool(written)
    return written


def write_media_vectors(
    conn: sqlite3.Connection,
    entries: list[tuple[int, list[float]]],
    *,
    model: str,
    dim: int,
) -> int:
    """把 ``(media_id, 向量)`` 写进 ``media_vec``（入库与重建共用同一条路径）。

    入库管线自己拿到向量（它有显式的 embedder，不依赖全局上下文），所以这里
    只负责"建表 + 覆盖写"；sqlite-vec 缺席时返回 0，调用方据此降级。
    """
    if not entries:
        return 0
    if not ensure_media_vec(conn, dim=dim, model=model):
        return 0
    with conn:
        conn.executemany(
            f"DELETE FROM {VEC_MEDIA} WHERE rowid = ?", [(int(key),) for key, _ in entries]
        )
        conn.executemany(
            f"INSERT INTO {VEC_MEDIA}(rowid, embedding) VALUES (?, ?)",
            [(int(key), to_blob(vector)) for key, vector in entries],
        )
    return len(entries)


def rebuild_media_vec(conn: sqlite3.Connection, *, batch: int = 32) -> int:
    """全量重建向量索引（``rag reindex`` 用；嵌入不可用则返回 0）。

    这是**换嵌入模型 / 换维度的唯一路径**：旧向量与新模型不在同一个语义空间，
    而 ``vec0`` 的维度写死在建表语句里——不 DROP 就连建表都建不回来。增量路径上
    的 ``ReindexRequired`` 守卫保持不放宽（它挡的是"静默混用"，不是"重建"）。

    顺序是**先嵌入、再 DROP、最后写**：反过来会让一次"模型暂时不可用"把还能用的
    旧索引删掉——降级是允许的，丢数据不是。
    """
    ids = [
        int(row["id"]) for row in conn.execute("SELECT id FROM media ORDER BY id").fetchall()
    ]
    if not ids:
        return 0
    embedded = _embed_media_rows(conn, ids, batch=batch)
    if embedded is None:
        return 0
    reset_media_vec(conn)
    return _write_embedded_media(conn, *embedded)


def reset_media_vec(conn: sqlite3.Connection) -> None:
    """丢掉向量表与它的元数据键（整表重建前调用）。

    ``vec0`` 的维度写在 ``CREATE VIRTUAL TABLE ... float[N]`` 里，只清行改不了维度，
    所以换模型必须 DROP 重建；meta 跟着一起清，让 ``ensure_media_vec`` 把新模型的
    名字与维度重新写一遍。

    **先加载扩展再 DROP**：``sqlite-vec`` 是按连接加载的，进程刚开始、还没碰过向量
    表的新连接去 DROP ``vec0`` 表会报 ``no such module: vec0``（``rag reindex`` 正是
    这条路径）。扩展加载不起来就说明本机没有向量能力，此时也没什么可 DROP 的，但
    meta 仍要清——否则换个能用的环境回来，旧 meta 会拦住新模型。
    """
    with conn:
        if db.load_sqlite_vec(conn):
            conn.execute(f"DROP TABLE IF EXISTS {VEC_MEDIA}")
        conn.execute(
            "DELETE FROM meta WHERE key IN (?, ?)", (EMBED_MODEL_META, EMBED_DIM_META)
        )


def reindex_media(conn: sqlite3.Connection, *, batch: int = 32) -> dict[str, int]:
    """重建两套索引并刷新分词版本（改分词策略后**必须**跑）。"""
    fts_count = rebuild_media_fts(conn)
    vec_count = rebuild_media_vec(conn, batch=batch)
    return {"media": fts_count, "fts": fts_count, "vec": vec_count}
