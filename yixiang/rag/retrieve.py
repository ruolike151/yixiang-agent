"""混合检索：FTS5 + 向量 → RRF → 硬过滤 → 口味软加权 → top-k（TECH §8.3~§8.5）。

四层，每层的失败方式都写在这里，改之前先读：

  1. **召回**：``search_fts``（jieba 预分词 + ``LIKE`` 兜底）与 ``search_vec``
     （sqlite-vec）各取 ``CANDIDATE_LIMIT`` 条。两路都可以缺席——**召回全空不
     等于功能不可用**，只是这一问没有答案。
  2. **融合**：直接用 PART 2 的 ``rrf()``。FTS5 的 bm25 与余弦不在同一量纲，
     融合名次对分数量纲免疫；这也让"记忆与语料共用一套融合口径"这句话有代码支撑。
  3. **硬过滤**：``recommend_log`` 近 7 天已推过的一律剔掉（D-12 的"两次推荐
     交集为空"就靠它）。
  4. **软加权**：``final = rrf_score * (1 + taste_score)``，``taste_score ∈
     [-0.5, +0.5]``——口味只微调，相关性始终是主信号（硬过滤会做成回声室）。

中文 FTS5 的坑（真实踩过的）：``unicode61`` 把连续 CJK 当一个 token，
``MATCH '悬疑'`` 命中 0 条；``trigram`` 又匹配不到 2 字查询。解法是 **jieba
预分词 + ``LIKE`` 兜底**，并把分词策略版本化到 ``meta.fts_tokenizer_version``——
否则会留下"一半旧分词一半新分词"的索引，极难排查（§8.4）。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from yixiang import db
from yixiang.errors import E_EMBED_UNAVAILABLE
from yixiang.memory import preprocess_for_fts, rrf
from yixiang.memory.semantic import CANDIDATE_LIMIT, RRF_K, Hit, _cut
from yixiang.rag import taste
from yixiang.rag.embed import (
    EMBED_DIM_META,
    EMBED_MODEL_META,
    ReindexRequired,
    build_embedder,
    embed_with_cache,
    to_blob,
)
from yixiang.runtime.models import Clock, SystemClock, to_local_iso

# 检索口径（§8.3 冻结）：候选 20，默认交付 3
DEFAULT_TOP_K = 3
DEFAULT_EXCLUDE_RECENT_DAYS = 7

# 向量表与分词版本
VEC_MEDIA = "media_vec"
TOKENIZER_VERSION = "jieba-v1"
TOKENIZER_META = "fts_tokenizer_version"

# mtype 归一：模型与用户都爱说人话，库里只存规范值
MTYPE_ALIASES: dict[str, str] = {
    "tv": "tv",
    "番": "tv",
    "番剧": "tv",
    "动画": "tv",
    "剧集": "tv",
    "电视剧": "tv",
    "anime": "tv",
    "movie": "movie",
    "电影": "movie",
    "剧场版": "movie",
    "film": "movie",
    "ova": "ova",
    "oad": "ova",
    "特别篇": "ova",
}

CHANNEL_LABELS = {"fts": "关键词", "vec": "语义", "browse": "评分"}

# 简介摘要长度：够显示"这条为什么像"的语义线索，又不至于把 top-3 撑成一篇长文
SYNOPSIS_CHARS = 80


def synopsis_snippet(synopsis: str, limit: int = SYNOPSIS_CHARS) -> str:
    """摘一段简介用于展示：空白压平、超长截断加省略号。

    这段文本是**不可信的外部内容**（T-2 / D-23），只允许经
    ``tools.media.wrap_external`` 进 prompt——所以它必须跟着 ``MediaHit.render()``
    一起出现，否则"包裹住不可信文本"这条纪律就没有实物可验。
    """
    text = " ".join(str(synopsis or "").split())
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


# --------------------------------------------------------------------- 数据结构
@dataclass(slots=True)
class MediaHit:
    """一条影视命中（PART-3 §4 的冻结字段在前，其余是实现细节）。

    ``reason`` 必须能引用命中字段（"为什么是它"是产品指标的一部分）：演示时
    照着念就是一段可核对的理由，而不是"模型觉得像"。
    """

    id: int
    title: str
    year: int | None = None
    mtype: str = ""
    rating: float | None = None
    genres: list[str] = field(default_factory=list)
    rrf_score: float = 0.0
    taste_score: float = 0.0
    final: float = 0.0
    reason: str = ""
    source_id: str = ""
    synopsis: str = ""
    channels: list[str] = field(default_factory=list)

    def render(self) -> str:
        """可核对的展示文本：首行字段（标题 / 年份 / 类型 / 评分 / 标签 / 理由），

        次行是简介摘要（不可信文本，进 prompt 前由调用方包裹，见 ``synopsis_snippet``）。
        """
        meta = [str(self.year)] if self.year else []
        if self.mtype:
            meta.append(self.mtype)
        if self.rating is not None:
            meta.append(f"{self.rating:g} 分")
        text = f"《{self.title}》"
        if meta:
            text += "（" + " · ".join(meta) + "）"
        if self.genres:
            text += " · " + "、".join(self.genres)
        if self.reason:
            text += f" —— {self.reason}"
        snippet = synopsis_snippet(self.synopsis)
        if snippet:
            text += f"\n   简介：{snippet}"
        return text

    def as_trace(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "year": self.year,
            "mtype": self.mtype,
            "rating": self.rating,
            "rrf": round(self.rrf_score, 4),
            "taste": round(self.taste_score, 4),
            "final": round(self.final, 4),
            "reason": self.reason,
            "channels": list(self.channels),
        }


@dataclass(slots=True)
class SearchExplain:
    """``explain_search`` 的返回：五段中间结果齐全（§8.3，检索不是黑盒）。"""

    query: str
    fts: list[MediaHit] = field(default_factory=list)
    vec: list[MediaHit] = field(default_factory=list)
    fused: list[MediaHit] = field(default_factory=list)
    filtered_out: list[tuple[MediaHit, str]] = field(default_factory=list)
    ranked: list[MediaHit] = field(default_factory=list)
    embed: str = "skipped"
    filters: dict[str, Any] = field(default_factory=dict)
    profile: Any = None

    @property
    def degraded(self) -> bool:
        """这一问是不是降级答的（embed 没真正跑起来）。"""
        return self.embed != "ok"


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


# ------------------------------------------------------------------ 冻结文本
def embed_text_for(*, title: str, mtype: str, genres: Any, synopsis: str) -> str:
    """**冻结的嵌入文本**（PART-3 §4）：标题重复两次是刻意的。

    重复标题让"标题命中"在短文本里占更大权重：很多查询（"怪物"）就是片名，
    而简介里的同名字符串往往是噪声。
    """
    tags = " ".join(taste.split_genres(genres))
    return f"{title} {title} {mtype} {tags} {(synopsis or '')[:500]}"


def row_embed_text(row: sqlite3.Row | dict[str, Any]) -> str:
    """按冻结口径拼一部作品的嵌入文本（行 / dict 都吃）。"""
    if isinstance(row, dict):
        title = str(row.get("title_zh") or row.get("title") or "")
        mtype = str(row.get("mtype") or "")
        genres = row.get("genres")
        synopsis = str(row.get("synopsis") or "")
    else:
        title = str(row["title_zh"] or row["title"] or "")
        mtype = str(row["mtype"] or "")
        genres = row["genres"]
        synopsis = str(row["synopsis"] or "")
    return embed_text_for(title=title, mtype=mtype, genres=genres, synopsis=synopsis)


def fts_tokens(
    *, title: str, title_zh: str = "", synopsis: str = ""
) -> tuple[str, str]:
    """``media_fts`` 的两个预分词列（写入与重建共用一份口径）。"""
    title_text = " ".join(part for part in (title, title_zh) if part)
    body = synopsis or title_text
    return " ".join(_cut(title_text)), " ".join(_cut(body))


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


def rebuild_media_fts(conn: sqlite3.Connection) -> int:
    """全量重建 FTS 索引（``media_fts`` 是 contentless 表，只能整表重插）。"""
    rows = conn.execute(
        "SELECT id, title, title_zh, synopsis FROM media ORDER BY id"
    ).fetchall()
    with conn:
        conn.execute("INSERT INTO media_fts(media_fts) VALUES('delete-all')")
        conn.executemany(
            "INSERT INTO media_fts(rowid, title_tok, synopsis_tok) VALUES (?, ?, ?)",
            [
                (
                    row["id"],
                    *fts_tokens(
                        title=str(row["title"] or ""),
                        title_zh=str(row["title_zh"] or ""),
                        synopsis=str(row["synopsis"] or ""),
                    ),
                )
                for row in rows
            ],
        )
    db.set_meta(conn, TOKENIZER_META, TOKENIZER_VERSION)
    return len(rows)


def fts_index_row(
    conn: sqlite3.Connection,
    rowid: int,
    *,
    title: str,
    title_zh: str = "",
    synopsis: str = "",
) -> None:
    """单行写入 FTS（入库路径用，省掉整表重建）。"""
    title_tok, synopsis_tok = fts_tokens(
        title=title, title_zh=title_zh, synopsis=synopsis
    )
    conn.execute(
        "INSERT INTO media_fts(rowid, title_tok, synopsis_tok) VALUES (?, ?, ?)",
        (rowid, title_tok, synopsis_tok),
    )


def fts_unindex_row(
    conn: sqlite3.Connection,
    rowid: int,
    *,
    title: str,
    title_zh: str = "",
    synopsis: str = "",
) -> None:
    """contentless 表按 rowid 删**必须带上旧的列值**（踩过的坑，别改成 DELETE）。"""
    title_tok, synopsis_tok = fts_tokens(
        title=title, title_zh=title_zh, synopsis=synopsis
    )
    conn.execute(
        "INSERT INTO media_fts(media_fts, rowid, title_tok, synopsis_tok) "
        "VALUES('delete', ?, ?, ?)",
        (rowid, title_tok, synopsis_tok),
    )


def embed_texts(texts: list[str], *, batch: int = 32) -> list[list[float]] | None:
    """带缓存的批量嵌入；不可用返回 ``None`` 并记降级标记（D-24）。"""
    ctx = current()
    if not texts:
        return []
    if ctx.embedder is None or ctx.conn is None:
        _mark_unavailable("没有可用的嵌入后端")
        return None
    try:
        vectors = embed_with_cache(ctx.conn, ctx.embedder, texts, batch=batch)
    except Exception as exc:  # 任何嵌入失败都降级，绝不让检索炸掉（§8.2）
        _mark_unavailable(str(exc))
        return None
    if len(vectors) != len(texts):
        _mark_unavailable(f"嵌入数量不匹配（{len(vectors)} != {len(texts)}）")
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
    marks = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT id, title, title_zh, mtype, genres, synopsis FROM media WHERE id IN ({marks}) "
        "ORDER BY id",
        ids,
    ).fetchall()
    if not rows:
        return 0
    ctx = current()
    vectors = embed_texts([row_embed_text(row) for row in rows], batch=batch)
    if vectors is None:
        return 0
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
    """全量重建向量索引（``rag reindex`` 用；嵌入不可用则返回 0）。"""
    ids = [
        int(row["id"]) for row in conn.execute("SELECT id FROM media ORDER BY id").fetchall()
    ]
    return index_media_vectors(conn, ids, batch=batch)


def reindex_media(conn: sqlite3.Connection, *, batch: int = 32) -> dict[str, int]:
    """重建两套索引并刷新分词版本（改分词策略后**必须**跑）。"""
    fts_count = rebuild_media_fts(conn)
    vec_count = rebuild_media_vec(conn, batch=batch)
    return {"media": fts_count, "fts": fts_count, "vec": vec_count}


def _mark_unavailable(reason: str) -> None:
    ctx = _context
    if ctx is None:
        return
    ctx.embed = "unavailable"
    if E_EMBED_UNAVAILABLE not in ctx.warnings:
        ctx.warnings.append(E_EMBED_UNAVAILABLE)
    if reason and reason not in ctx.warnings:
        ctx.warnings.append(reason)


# ------------------------------------------------------------------ 召回
def normalize_mtype(value: Any) -> str:
    """``"番剧" / "tv" / "电视剧"`` → ``"tv"``；不认识的原样小写返回。"""
    raw = str(value or "").strip().lower()
    if not raw:
        return ""
    return MTYPE_ALIASES.get(raw, raw)


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


def _reason_for(hit: MediaHit, profile: taste.TasteProfile) -> str:
    """把"为什么是它"落到可核对的字段上（演示时照念即可）。"""
    parts: list[str] = []
    labels = [CHANNEL_LABELS[channel] for channel in hit.channels if channel in CHANNEL_LABELS]
    if labels:
        parts.append("+".join(labels) + "命中")
    liked = [tag for tag in hit.genres if tag in profile.liked_tags]
    disliked = [tag for tag in hit.genres if tag in profile.disliked_tags]
    if liked:
        parts.append("喜欢 " + "/".join(liked))
    if disliked:
        parts.append("不喜欢 " + "/".join(disliked))
    if hit.year:
        parts.append(f"{hit.year} 年")
    if hit.rating is not None:
        parts.append(f"{hit.rating:g} 分")
    return " · ".join(parts) if parts else "相关性排序（无画像信号）"


def _recently_recommended(
    conn: sqlite3.Connection, days: int, moment: datetime
) -> set[int]:
    """近 N 天已推过的 id（硬过滤；``days <= 0`` 表示不过滤）。"""
    if days <= 0:
        return set()
    since = (moment.date() - timedelta(days=int(days))).isoformat()
    try:
        rows = conn.execute(
            "SELECT DISTINCT media_id FROM recommend_log WHERE recommended_on >= ?",
            (since,),
        ).fetchall()
    except sqlite3.OperationalError:  # 还没迁移过的库
        return set()
    return {int(row["media_id"]) for row in rows}


def log_recommendation(
    conn: sqlite3.Connection,
    media_ids: list[int],
    *,
    channel: str,
    moment: datetime,
    feedback: str = "none",
) -> int:
    """写 ``recommend_log``（去重窗口的唯一数据来源；brief 与对话共用）。"""
    ids = [int(value) for value in media_ids]
    if not ids:
        return 0
    day = moment.date().isoformat()
    stamp = to_local_iso(moment)
    try:
        with conn:
            conn.executemany(
                "INSERT INTO recommend_log(media_id, recommended_on, channel, feedback, "
                "created_at) VALUES (?, ?, ?, ?, ?)",
                [(media_id, day, channel, feedback, stamp) for media_id in ids],
            )
    except sqlite3.OperationalError:
        return 0
    return len(ids)


# ------------------------------------------------------------------ 冻结入口
def explain_search(
    query: str,
    *,
    top_k: int = DEFAULT_TOP_K,
    mtype: Any = None,
    year_from: Any = None,
    year_to: Any = None,
    exclude_recent_days: int = DEFAULT_EXCLUDE_RECENT_DAYS,
    limit: int = CANDIDATE_LIMIT,
    use_taste: bool = True,
) -> SearchExplain:
    """一次完整检索的五段中间结果（``ops explain-search`` 靠它打印全过程）。

    ``use_taste=False`` 关掉口味软加权：评测口径要用它（期望集标注的是"哪些作品
    相关"，不是"这个用户喜不喜欢"）。带上口味会让同一个数字随 ``user.md`` 变化，
    CI（PART 4 的 L3 回归集就是 ``media.jsonl``）就没法比。
    """
    ctx = current()
    if ctx.conn is None:
        raise RuntimeError("RAG 上下文缺少数据库连接")
    conn = ctx.conn
    moment = ctx.clock.now()

    fts = search_fts(conn, query, limit, mtype=mtype, year_from=year_from, year_to=year_to)
    vec = search_vec(conn, query, limit, mtype=mtype, year_from=year_from, year_to=year_to)
    # 两路都空（空查询串 / 语料里真没这条）→ 用评分兜底，别返回"没有"
    browse = (
        []
        if (fts or vec or (query or "").strip())
        else browse_media(conn, limit, mtype=mtype, year_from=year_from, year_to=year_to)
    )

    channels = _channel_map(fts, vec, browse)
    fused_hits = rrf([_as_hits(ranking) for ranking in (fts, vec, browse)], k=RRF_K)
    fused = _materialize(conn, fused_hits, channels)

    recent = _recently_recommended(conn, int(exclude_recent_days or 0), moment)
    kept: list[MediaHit] = []
    filtered: list[tuple[MediaHit, str]] = []
    for hit in fused:
        if hit.id in recent:
            filtered.append((hit, f"近 {int(exclude_recent_days)} 天已推荐"))
        else:
            kept.append(hit)

    # 关掉口味时用**空画像**（而不是跳过赋值）：taste=0、final=rrf、理由里也不会
    # 冒出"喜欢 X"——一条代码路径，少一个只在评测里生效的分支
    profile = (
        taste.build_profile(conn, ctx.data_dir, clock=ctx.clock)
        if use_taste
        else taste.TasteProfile()
    )
    for hit in kept:
        hit.taste_score = taste.taste_score(hit, profile)
        hit.final = hit.rrf_score * (1.0 + hit.taste_score)
        hit.reason = _reason_for(hit, profile)
    # 相关性是主信号：先 final（含口味），同分再看纯相关性，最后用 id 保确定性
    kept.sort(key=lambda item: (-item.final, -item.rrf_score, item.id))

    return SearchExplain(
        query=query,
        fts=fts,
        vec=vec,
        fused=fused,
        filtered_out=filtered,
        ranked=kept[: max(int(top_k), 1)],
        embed=ctx.embed,
        filters={
            "top_k": int(top_k),
            "mtype": normalize_mtype(mtype),
            "year_from": year_from,
            "year_to": year_to,
            "exclude_recent_days": int(exclude_recent_days or 0),
            "use_taste": bool(use_taste),
        },
        profile=profile,
    )


def retrieve_media(
    query: str,
    *,
    top_k: int = DEFAULT_TOP_K,
    mtype: Any = None,
    year_from: Any = None,
    year_to: Any = None,
    exclude_recent_days: int = DEFAULT_EXCLUDE_RECENT_DAYS,
    use_taste: bool = True,
) -> list[MediaHit]:
    """冻结签名（PART-3 §4）：默认 ``top_k=3``，**工具层不得放大**。"""
    return explain_search(
        query,
        top_k=top_k,
        mtype=mtype,
        year_from=year_from,
        year_to=year_to,
        exclude_recent_days=exclude_recent_days,
        use_taste=use_taste,
    ).ranked


__all__ = [
    "CANDIDATE_LIMIT",
    "DEFAULT_EXCLUDE_RECENT_DAYS",
    "DEFAULT_TOP_K",
    "MediaHit",
    "RetrievalContext",
    "SearchExplain",
    "browse_media",
    "clear_warnings",
    "configure",
    "current",
    "embed_text_for",
    "embed_texts",
    "ensure_configured",
    "ensure_media_vec",
    "explain_search",
    "fts_index_row",
    "fts_tokens",
    "fts_unindex_row",
    "index_media_vectors",
    "is_configured",
    "log_recommendation",
    "normalize_mtype",
    "rebuild_media_fts",
    "rebuild_media_vec",
    "reindex_media",
    "reset",
    "retrieve_media",
    "row_embed_text",
    "search_fts",
    "search_like",
    "search_vec",
    "synopsis_snippet",
    "trace_info",
    "write_media_vectors",
]
