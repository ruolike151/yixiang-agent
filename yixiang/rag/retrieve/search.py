"""四路召回与浏览（``search_fts`` / ``search_like`` / ``search_vec`` / ``browse_media``）。

召回全空**不等于功能不可用**，只是这一问没有答案；这一层只负责「取候选」，
融合、过滤与打分都在 ``rerank``。``_filters`` 是四条路共用的硬过滤口径。"""

from __future__ import annotations

import sqlite3
from typing import Any

from yixiang.memory import preprocess_for_fts
from yixiang.memory.semantic import CANDIDATE_LIMIT, Hit
from yixiang.rag import taste
from yixiang.rag.embed import ReindexRequired, to_blob
from yixiang.rag.retrieve.context import current
from yixiang.rag.retrieve.models import MediaHit
from yixiang.rag.retrieve.text import normalize_mtype
from yixiang.rag.retrieve.vector import VEC_MEDIA, embed_texts, ensure_media_vec

CHANNEL_LABELS = {"fts": "关键词", "vec": "语义", "browse": "评分"}


def _filters(
    *, mtype: Any = None, year_from: Any = None, year_to: Any = None
) -> tuple[list[str], list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    canonical = normalize_mtype(mtype)
    if canonical:
        clauses.append("m.mtype = ?")
        params.append(canonical)
    if year_from is not None:
        clauses.append("m.year >= ?")
        params.append(int(year_from))
    if year_to is not None:
        clauses.append("m.year <= ?")
        params.append(int(year_to))
    return clauses, params


def search_fts(
    conn: sqlite3.Connection,
    query: str,
    limit: int = CANDIDATE_LIMIT,
    *,
    mtype: Any = None,
    year_from: Any = None,
    year_to: Any = None,
) -> list[MediaHit]:
    """FTS5 召回（jieba 预分词）；命中 0 条时退到 ``LIKE``（§8.4）。"""
    trimmed = (query or "").strip()
    if not trimmed:
        return []
    clauses, params = _filters(mtype=mtype, year_from=year_from, year_to=year_to)
    extra = "".join(f" AND {clause}" for clause in clauses)
    hits: list[MediaHit] = []
    match = preprocess_for_fts(trimmed)
    if match:
        try:
            rows = conn.execute(
                "SELECT m.* FROM media_fts JOIN media m ON m.id = media_fts.rowid "
                f"WHERE media_fts MATCH ?{extra} ORDER BY rank LIMIT ?",
                (match, *params, int(limit)),
            ).fetchall()
            hits = [_hit_from_row(row, channels=["fts"]) for row in rows]
        except sqlite3.OperationalError:  # FTS 表还没建 / 查询串非法
            hits = []
    if hits:
        return hits
    return search_like(
        conn, trimmed, limit=limit, mtype=mtype, year_from=year_from, year_to=year_to
    )


def search_like(
    conn: sqlite3.Connection,
    query: str,
    limit: int = CANDIDATE_LIMIT,
    *,
    mtype: Any = None,
    year_from: Any = None,
    year_to: Any = None,
) -> list[MediaHit]:
    """短查询兜底：1~2 字查询在 FTS5 里可能一个 token 都匹配不上。

    语料 <5000 条时全表 ``LIKE`` 是毫秒级，不值得为它上 trigram 索引。
    """
    trimmed = (query or "").strip()
    if not trimmed:
        return []
    clauses, params = _filters(mtype=mtype, year_from=year_from, year_to=year_to)
    extra = "".join(f" AND {clause}" for clause in clauses)
    pattern = f"%{trimmed}%"
    rows = conn.execute(
        "SELECT m.* FROM media m WHERE (m.title LIKE ? OR m.title_zh LIKE ? "
        f"OR m.synopsis LIKE ? OR m.genres LIKE ?){extra} "
        "ORDER BY (m.rating IS NULL), m.rating DESC, m.id LIMIT ?",
        (pattern, pattern, pattern, pattern, *params, int(limit)),
    ).fetchall()
    return [_hit_from_row(row, channels=["fts"]) for row in rows]


def browse_media(
    conn: sqlite3.Connection,
    limit: int = CANDIDATE_LIMIT,
    *,
    mtype: Any = None,
    year_from: Any = None,
    year_to: Any = None,
) -> list[MediaHit]:
    """无查询串时的兜底召回（"随便推一部"）：按评分排序。"""
    clauses, params = _filters(mtype=mtype, year_from=year_from, year_to=year_to)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = conn.execute(
        f"SELECT m.* FROM media m{where} "
        "ORDER BY (m.rating IS NULL), m.rating DESC, m.year DESC, m.id LIMIT ?",
        (*params, int(limit)),
    ).fetchall()
    return [_hit_from_row(row, channels=["browse"]) for row in rows]


def search_vec(
    conn: sqlite3.Connection,
    query: str,
    limit: int = CANDIDATE_LIMIT,
    *,
    mtype: Any = None,
    year_from: Any = None,
    year_to: Any = None,
) -> list[MediaHit]:
    """向量召回（sqlite-vec KNN）。嵌入/扩展不可用一律返回空，由上层降级。"""
    trimmed = (query or "").strip()
    if not trimmed:
        return []
    ctx = current()
    vectors = embed_texts([trimmed])
    if not vectors:
        return []
    try:
        if not ensure_media_vec(conn, dim=len(vectors[0]), model=str(ctx.embedder.model)):
            return []
    except ReindexRequired as exc:
        ctx.reindex_required = str(exc)
        return []
    clauses, params = _filters(mtype=mtype, year_from=year_from, year_to=year_to)
    extra = "".join(f" AND {clause}" for clause in clauses)
    try:
        rows = conn.execute(
            f"SELECT m.*, v.distance AS distance FROM {VEC_MEDIA} v JOIN media m ON m.id = v.rowid "
            f"WHERE v.embedding MATCH ? AND k = ?{extra} ORDER BY v.distance",
            (to_blob(vectors[0]), int(limit), *params),
        ).fetchall()
    except sqlite3.OperationalError:  # 向量表还没建
        return []
    ctx.vec_ready = True
    return [_hit_from_row(row, channels=["vec"]) for row in rows]


# ------------------------------------------------------------------ 融合与排序
def _as_hits(hits: list[MediaHit]) -> list[Hit]:
    return [
        Hit(
            id=hit.id,
            kind="media",
            content=hit.title,
            subject=hit.mtype,
            source=hit.source_id,
        )
        for hit in hits
    ]


def _channel_map(*rankings: list[MediaHit]) -> dict[int, list[str]]:
    mapping: dict[int, list[str]] = {}
    for ranking in rankings:
        for hit in ranking:
            bucket = mapping.setdefault(hit.id, [])
            for channel in hit.channels:
                if channel not in bucket:
                    bucket.append(channel)
    return mapping


def _materialize(
    conn: sqlite3.Connection,
    fused: list[Hit],
    channels: dict[int, list[str]],
) -> list[MediaHit]:
    """把融合后的 Hit（只有 id/score）补回完整字段，顺序按融合名次保持。"""
    if not fused:
        return []
    ids = [hit.id for hit in fused]
    marks = ",".join("?" * len(ids))
    rows = {
        int(row["id"]): row
        for row in conn.execute(f"SELECT * FROM media WHERE id IN ({marks})", ids).fetchall()
    }
    result: list[MediaHit] = []
    for hit in fused:
        row = rows.get(int(hit.id))
        if row is None:  # 融合期间被删掉的记录：跳过，不留幽灵
            continue
        result.append(
            _hit_from_row(
                row,
                channels=channels.get(int(hit.id), []),
                rrf_score=float(hit.score),
            )
        )
    return result


def _hit_from_row(
    row: sqlite3.Row,
    *,
    channels: list[str] | None = None,
    rrf_score: float = 0.0,
) -> MediaHit:
    keys = row.keys()
    return MediaHit(
        id=int(row["id"]),
        title=str(row["title_zh"] or row["title"] or ""),
        year=int(row["year"]) if row["year"] is not None else None,
        mtype=str(row["mtype"] or ""),
        rating=float(row["rating"]) if row["rating"] is not None else None,
        genres=taste.split_genres(row["genres"]),
        rrf_score=rrf_score,
        source_id=str(row["source_id"] or ""),
        synopsis=str(row["synopsis"] or "") if "synopsis" in keys else "",
        channels=list(channels or []),
    )
