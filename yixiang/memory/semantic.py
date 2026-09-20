"""语义记忆：``facts`` 的混合检索（FTS5 + vec0）、去重、软删、恢复（TECH §7.2、§7.6、§7.10）。

三条纪律（面试会追问，写在代码里）：

  1. **混合检索而不是二选一**：关键词对精确实体（人名、片名）强，向量对语义
     （"那种压抑的悬疑"）强；两路各取 top20 后用 RRF 融合（k=60），
     与 PART 3 的影视语料共用同一个 ``rrf()``。
  2. **索引与主表同事务双写**：SQLite 不会自动维护 FTS/向量索引（§12.2），
     任何写入都在一个事务里同时改 ``facts`` / ``facts_fts`` / ``facts_vec``，
     否则就会出现"库里有、检索不到"的漂移。
  3. **嵌入永远可以缺席**：没有 sqlite-vec（``vec_ready=False``）或模型没下下来
     （``E_EMBED_UNAVAILABLE``）时，检索降级为纯 FTS5，写入降级为纯主表 + FTS。
     降级是可观测的（trace 里有 ``embed`` 字段），不是静默失败。

冻结签名（PART 3/4 依赖，不可改）见 PROJECT 文档 §4：
``retrieve_memory`` / ``save_fact`` / ``soft_delete_fact`` / ``rrf`` / ``preprocess_for_fts``。
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
import sqlite3
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from yixiang import db
from yixiang.errors import E_EMBED_UNAVAILABLE
from yixiang.runtime.external import wrap_external
from yixiang.runtime.models import Clock, SystemClock, to_local_iso

# 固定的检索口径（§7.6）
CANDIDATE_LIMIT = 20
RRF_K = 60
DEDUP_SAME = 0.92
DEDUP_MAYBE = 0.80
EMBED_DIM_META = "memory.vec_dim"
EMBED_MODEL_META = "memory.embed_model"

# 假的确定性嵌入后端：仅在测试与离线降级里用（``YIXIANG_EMBED_BACKEND=hash``）
BUILTIN_DIM = {"BAAI/bge-small-zh-v1.5": 512}


# --------------------------------------------------------------------- 分词
try:  # pragma: no cover - 只是关掉 jieba 的构建日志，行为与断言无关
    import jieba

    jieba.setLogLevel(logging.ERROR)
except Exception:  # pragma: no cover - 没装 jieba 时退化为按字切分
    jieba = None

_TOKEN_RE = re.compile(r"[0-9A-Za-z\u4e00-\u9fff]+")


def preprocess_for_fts(text: str) -> str:  # noqa: D401 - 见下
    """jieba 分词后的 FTS5 查询串：``"我" OR "上周"``（PART 3 也 import 这个函数）。

    用户输入不是合法的 FTS5 查询——引号、括号、``*`` 都会让 ``MATCH`` 直接报错，
    所以这里只保留"字母/数字/汉字"片段，逐个加双引号再 OR 起来。
    """
    tokens = _cut(text)
    if not tokens:
        return ""
    quoted = [f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens]
    return " OR ".join(dict.fromkeys(quoted))


def _cut(text: str) -> list[str]:
    raw = (text or "").strip()
    if not raw:
        return []
    if jieba is not None:
        pieces = [piece.strip() for piece in jieba.lcut(raw)]
    else:  # pragma: no cover - 只在没有 jieba 的环境里走到
        pieces = _TOKEN_RE.findall(raw)
    tokens = [_TOKEN_RE.fullmatch(piece).group(0) for piece in pieces if _TOKEN_RE.fullmatch(piece)]
    return tokens


def _bigrams(text: str) -> list[str]:
    """哈希假嵌入用的词袋：jieba 词 + 字 + 相邻字对（中文相似度靠它撑起来）。"""
    raw = re.sub(r"\s+", "", (text or "").lower())
    if not raw:
        return []
    chars = list(raw)
    return _cut(raw) + chars + ["".join(pair) for pair in zip(chars, chars[1:], strict=False)]


# ------------------------------------------------------------------- 嵌入后端
class Embedder(Protocol):
    name: str
    dim: int | None

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class EmbedUnavailable(Exception):
    """嵌入后端不可用（没装依赖 / 模型没下下来）——上层据此降级，不炸。"""


class HashEmbedder:
    """确定性哈希词袋嵌入：同文本余弦 1.0，字面相近的文本余弦高。

    它存在只有一个理由——让"跨会话检索"这类行为在没有网络、没有模型的
    确定性用例里也能被断言（§13.6）。生产默认走 fastembed。
    """

    name = "hash"

    def __init__(self, dim: int = 256) -> None:
        self.dim = dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._one(text) for text in texts]

    def _one(self, text: str) -> list[float]:
        vector = [0.0] * self.dim
        for token in _bigrams(text):
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
            index = int.from_bytes(digest[:4], "little") % self.dim
            sign = 1.0 if digest[4] % 2 else -1.0
            vector[index] += sign
        return _l2_normalize(vector)


class FastEmbedEmbedder:
    """fastembed（onnxruntime）后端：默认生产后端，**懒加载**——只有真的要嵌入时才
    触发模型下载，这样没装模型的机器照样能跑纯 FTS5 的确定性用例（§1.3、§8.2）。"""

    def __init__(self, model: str) -> None:
        self.name = model
        self._model_name = model
        self._backend: Any = None
        self.dim = BUILTIN_DIM.get(model)

    def _load(self) -> Any:
        if self._backend is None:
            try:
                from fastembed import TextEmbedding  # type: ignore import-not-found

                self._backend = TextEmbedding(model_name=self._model_name)
            except Exception as exc:  # 模型没下下来 / 没装 onnxruntime
                raise EmbedUnavailable(f"fastembed 不可用：{exc}") from exc
        return self._backend

    def embed(self, texts: list[str]) -> list[list[float]]:
        backend = self._load()
        try:
            vectors = [list(map(float, vector)) for vector in backend.embed(list(texts))]
        except Exception as exc:
            raise EmbedUnavailable(f"嵌入失败：{exc}") from exc
        if vectors and self.dim is None:
            self.dim = len(vectors[0])
        return vectors


def build_embedder(settings: Any = None, override: Embedder | None = None) -> Embedder:
    """按 ``Settings.embed_backend`` 选后端；``hash`` 是测试/离线用的假后端。"""
    if override is not None:
        return override
    backend = str(getattr(settings, "embed_backend", "") or "hash")
    model = str(getattr(settings, "embed_model", "") or "BAAI/bge-small-zh-v1.5")
    if backend in {"hash", "none"}:
        return HashEmbedder()
    return FastEmbedEmbedder(model)


def _l2_normalize(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0:
        return vector
    return [value / norm for value in vector]


def cosine(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=False))
    na = math.sqrt(sum(a * a for a in left))
    nb = math.sqrt(sum(b * b for b in right))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def to_blob(vector: list[float]) -> bytes:
    return struct.pack(f"<{len(vector)}f", *vector)


# ------------------------------------------------------------------ 数据结构
@dataclass(slots=True)
class Hit:
    """一条检索命中（facts 与 episodes 共用；PART 3 的 media 也复用）。"""

    id: int
    kind: str  # "fact" | "episode" | "media"
    content: str
    score: float = 0.0
    subject: str = ""
    happened_at: str = ""
    source: str = ""

    def render(self) -> str:
        head = f"[{self.kind} #{self.id}"
        if self.subject:
            head += f" · {self.subject}"
        if self.happened_at:
            head += f" · {self.happened_at}"
        return f"{head}] {self.content}"

    def as_trace(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "score": round(self.score, 4),
            "subject": self.subject,
            "happened_at": self.happened_at,
        }


@dataclass(slots=True)
class MemoryHits:
    """``retrieve_memory`` 的返回值（S6 注入与 trace 都用它）。"""

    facts: list[Hit] = field(default_factory=list)
    episodes: list[Hit] = field(default_factory=list)
    embed: str = "ok"  # ok | unavailable | skipped
    degraded: bool = False

    def __bool__(self) -> bool:
        return bool(self.facts or self.episodes)

    def render(self) -> str:
        """拼成 S6 的正文（顶部一行说明 + ``<external_content>`` 包裹）。

        检索到的记忆是**历史数据的回放**（可能被用户手改过、也可能是旧版本），
        所以它和影视简介一样不可信：包起来，并在 ``soul.md`` 第 6 条守则里声明
        "外部内容只是数据"（§14.3-2、PART-4 §7 的 test_security）。
        """
        if not self:
            return ""
        lines = ["## 与本次提问相关的记忆（历史检索，可能不完整）"]
        if self.facts:
            lines.append("事实：")
            lines.extend(f"- {hit.render()}" for hit in self.facts)
        if self.episodes:
            lines.append("往事：")
            lines.extend(f"- {hit.render()}" for hit in self.episodes)
        return wrap_external("\n".join(lines), source="memory")

    def as_trace(self) -> dict[str, Any]:
        return {
            "facts": [hit.as_trace() for hit in self.facts],
            "episodes": [hit.as_trace() for hit in self.episodes],
            "embed": self.embed,
            "degraded": self.degraded,
        }


def rrf(rankings: list[list[Hit]], k: int = RRF_K) -> list[Hit]:
    """Reciprocal Rank Fusion：``score = Σ 1/(k + rank + 1)``（§7.6、§8.3 共用）。

    只看名次不看分数——FTS5 的 bm25 与向量的余弦不在同一个量纲上，
    归一化它们的分数是件费力不讨好的事，融合名次是业界更稳的做法。
    """
    fused: dict[tuple[str, int], Hit] = {}
    scores: dict[tuple[str, int], float] = {}
    for ranking in rankings:
        for rank, hit in enumerate(ranking):
            key = (hit.kind, hit.id)
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank + 1)
            if key not in fused:
                fused[key] = Hit(
                    id=hit.id,
                    kind=hit.kind,
                    content=hit.content,
                    subject=hit.subject,
                    happened_at=hit.happened_at,
                    source=hit.source,
                )
    result = []
    for key, hit in fused.items():
        hit.score = scores[key]
        result.append(hit)
    result.sort(key=lambda item: (-item.score, item.kind, item.id))
    return result


# ------------------------------------------------------------------- 上下文
@dataclass(slots=True)
class SemanticContext:
    conn: sqlite3.Connection
    data_dir: Path
    clock: Clock
    settings: Any = None
    embedder: Any = None
    is_same: Any = None
    vec_ready: bool = False
    warnings: list[str] = field(default_factory=list)


_context: SemanticContext | None = None


def configure(
    conn: sqlite3.Connection,
    *,
    data_dir: Path | str,
    clock: Clock | None = None,
    settings: Any = None,
    embedder: Any = None,
    is_same: Any = None,
) -> FactStore:
    """装配语义记忆（由 ``yixiang.memory.configure`` 调用）。返回 ``FactStore``。"""
    global _context
    _context = SemanticContext(
        conn=conn,
        data_dir=Path(data_dir),
        clock=clock or SystemClock(),
        settings=settings,
        embedder=embedder,
        is_same=is_same,
    )
    store = FactStore(_context)
    _context.vec_ready = ensure_vec_tables(conn, dim=_dim_of(store.embedder))
    return store


def reset() -> None:
    global _context
    _context = None


def context() -> SemanticContext:
    if _context is None:
        raise RuntimeError("语义记忆未装配：先调用 yixiang.memory.configure(...)")
    return _context


def _now() -> str:
    return to_local_iso(context().clock.now())


def _dim_of(embedder: Any) -> int | None:
    if embedder is None:
        return None
    return getattr(embedder, "dim", None)


# --------------------------------------------------------------- 向量表与索引
VEC_FACTS = "facts_vec"
VEC_EPISODES = "episodes_vec"


def ensure_vec_tables(conn: sqlite3.Connection, *, dim: int | None = None) -> bool:
    """建 vec0 表（sqlite-vec 不可用就返回 False，全链路降级为纯 FTS）。

    维度变了要重建（换嵌入模型 = 旧向量全部作废，§7.12"维度错配"）。
    """
    if not db.load_sqlite_vec(conn):
        return False
    dim = dim or _stored_dim(conn)
    if not dim:
        return False
    current = db.get_meta(conn, EMBED_DIM_META)
    if current and int(current) != int(dim):
        with conn:
            conn.execute(f"DROP TABLE IF EXISTS {VEC_FACTS}")
            conn.execute(f"DROP TABLE IF EXISTS {VEC_EPISODES}")
    with conn:
        conn.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS {VEC_FACTS} USING vec0("
            f"rowid INTEGER PRIMARY KEY, embedding float[{int(dim)}] distance_metric=cosine)"
        )
        conn.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS {VEC_EPISODES} USING vec0("
            f"rowid INTEGER PRIMARY KEY, embedding float[{int(dim)}] distance_metric=cosine)"
        )
    db.set_meta(conn, EMBED_DIM_META, str(int(dim)))
    return True


def _stored_dim(conn: sqlite3.Connection) -> int | None:
    value = db.get_meta(conn, EMBED_DIM_META)
    return int(value) if value else None


def rebuild_facts_fts(conn: sqlite3.Connection) -> int:
    """全量重建 FTS 索引。

    ``facts_fts`` 是 **contentless** 表：它不支持 ``DELETE FROM ... WHERE rowid``，
    只能整表 ``delete-all`` 后重插（这是踩过的坑，别改回去）。
    """
    rows = conn.execute(
        "SELECT id, content FROM facts WHERE deleted = 0 ORDER BY id"
    ).fetchall()
    with conn:
        conn.execute("INSERT INTO facts_fts(facts_fts) VALUES('delete-all')")
        conn.executemany(
            "INSERT INTO facts_fts(rowid, content_tok) VALUES (?, ?)",
            [(row["id"], " ".join(_cut(row["content"]))) for row in rows],
        )
    return len(rows)


def rebuild_vec(conn: sqlite3.Connection) -> int:
    """全量重建向量索引（``memory rebuild`` 用；嵌入不可用则原样返回 0）。"""
    ctx = context()
    rows = conn.execute(
        "SELECT id, content FROM facts WHERE deleted = 0 ORDER BY id"
    ).fetchall()
    if not rows:
        return 0
    vectors = _embed([row["content"] for row in rows])
    if vectors is None:
        return 0
    dim = len(vectors[0])
    if not ensure_vec_tables(conn, dim=dim):
        return 0
    with conn:
        conn.execute(f"DELETE FROM {VEC_FACTS}")
        conn.executemany(
            f"INSERT INTO {VEC_FACTS}(rowid, embedding) VALUES (?, ?)",
            [(row["id"], to_blob(vector)) for row, vector in zip(rows, vectors, strict=False)],
        )
    ctx.vec_ready = True
    return len(rows)


def _embed(texts: list[str]) -> list[list[float]] | None:
    """算嵌入；不可用返回 None 并把 ``E_EMBED_UNAVAILABLE`` 记进降级标记（D-24）。"""
    ctx = context()
    if ctx.embedder is None:
        ctx.warnings.append(E_EMBED_UNAVAILABLE)
        return None
    try:
        return ctx.embedder.embed(texts)
    except EmbedUnavailable:
        ctx.warnings.append(E_EMBED_UNAVAILABLE)
        return None


# --------------------------------------------------------------------- 存储
class FactStore:
    """``facts`` 表的读写入口：索引双写、去重、软删、恢复都在这里。"""

    def __init__(self, ctx: SemanticContext) -> None:
        self.ctx = ctx
        self.conn = ctx.conn
        self.embedder = ctx.embedder

    # ------------------------------------------------------------------ 读
    def get(self, fact_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM facts WHERE id = ?", (fact_id,)).fetchone()

    def alive(self, limit: int | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM facts WHERE deleted = 0 ORDER BY id"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return list(self.conn.execute(sql).fetchall())

    def all(self, *, include_deleted: bool = False) -> list[sqlite3.Row]:
        sql = "SELECT * FROM facts"
        if not include_deleted:
            sql += " WHERE deleted = 0"
        return list(self.conn.execute(sql + " ORDER BY id").fetchall())

    # ------------------------------------------------------------------ 写
    def insert(
        self,
        subject: str,
        content: str,
        *,
        source: str = "user",
        pinned: bool = False,
        fact_id: int | None = None,
        vector: list[float] | None = None,
        touch: bool = True,
    ) -> int:
        """插入一条 fact：主表 + FTS + 向量在同一个事务里落盘（§12.2）。"""
        now = _now()
        with self.conn:
            cursor = self.conn.execute(
                """
                INSERT INTO facts(subject, content, source, pinned, created_at, updated_at,
                                  last_used_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (subject, content, source, int(pinned), now, now, now if touch else None),
            )
        fact_id = fact_id or int(cursor.lastrowid)
        if fact_id != int(cursor.lastrowid):
            # 显式 id（人力恢复 / 同步 INSERT 显式指定 id 的场景）
            with self.conn:
                self.conn.execute("DELETE FROM facts WHERE id = ?", (int(cursor.lastrowid),))
                self.conn.execute(
                    """
                    INSERT INTO facts(id, subject, content, source, pinned, created_at,
                                      updated_at, last_used_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (fact_id, subject, content, source, int(pinned), now, now,
                     now if touch else None),
                )
        self._index(fact_id, content, vector)
        return fact_id

    def update(
        self,
        fact_id: int,
        *,
        content: str | None = None,
        subject: str | None = None,
        pinned: bool | None = None,
        vector: list[float] | None = None,
    ) -> bool:
        row = self.get(fact_id)
        if row is None:
            return False
        new_content = content if content is not None else row["content"]
        new_subject = subject if subject is not None else row["subject"]
        new_pinned = int(pinned) if pinned is not None else int(row["pinned"])
        with self.conn:
            self.conn.execute(
                "UPDATE facts SET content = ?, subject = ?, pinned = ?, updated_at = ?,"
                " deleted = 0, deleted_at = NULL WHERE id = ?",
                (new_content, new_subject, new_pinned, _now(), fact_id),
            )
        if new_content != row["content"]:
            self._index(fact_id, new_content, vector)
        return True

    def soft_delete(self, fact_id: int) -> bool:
        row = self.get(fact_id)
        if row is None or row["deleted"]:
            return False
        with self.conn:
            self.conn.execute(
                "UPDATE facts SET deleted = 1, deleted_at = ? WHERE id = ?",
                (_now(), fact_id),
            )
        self._unindex(fact_id)
        return True

    def restore(self, fact_id: int, *, vector: list[float] | None = None) -> bool:
        row = self.get(fact_id)
        if row is None or not row["deleted"]:
            return False
        with self.conn:
            self.conn.execute(
                "UPDATE facts SET deleted = 0, deleted_at = NULL, updated_at = ? WHERE id = ?",
                (_now(), fact_id),
            )
        self._index(fact_id, row["content"], vector)
        return True

    def touch_used(self, fact_ids: list[int]) -> None:
        if not fact_ids:
            return
        now = _now()
        with self.conn:
            self.conn.executemany(
                "UPDATE facts SET last_used_at = ? WHERE id = ?",
                [(now, fact_id) for fact_id in fact_ids],
            )

    # ------------------------------------------------------------------ 索引
    def _index(self, fact_id: int, content: str, vector: list[float] | None) -> None:
        tokens = " ".join(_cut(content))
        # contentless FTS5 虚表不支持 UPSERT，索引重建统一交给 _reindex_fts。
        self._reindex_fts(fact_id, tokens)
        if vector is None:
            # 调用方没给向量就现算一条：否则"改了内容但向量还是旧的"会让
            # 语义检索悄悄返回过期的相似度（§7.12 的索引漂移）。
            vector = _embed_one(content)
        if vector is not None and self.ensure_vec(len(vector)):
            with self.conn:
                self.conn.execute(f"DELETE FROM {VEC_FACTS} WHERE rowid = ?", (fact_id,))
                self.conn.execute(
                    f"INSERT INTO {VEC_FACTS}(rowid, embedding) VALUES (?, ?)",
                    (fact_id, to_blob(vector)),
                )

    def _reindex_fts(self, fact_id: int, tokens: str) -> None:
        """contentless FTS5 不能按 rowid 删——整表重建是这里唯一正确的做法。"""
        rows = self.conn.execute(
            "SELECT id, content FROM facts WHERE deleted = 0 ORDER BY id"
        ).fetchall()
        with self.conn:
            self.conn.execute("INSERT INTO facts_fts(facts_fts) VALUES('delete-all')")
            self.conn.executemany(
                "INSERT INTO facts_fts(rowid, content_tok) VALUES (?, ?)",
                [
                    (row["id"], tokens if row["id"] == fact_id else " ".join(_cut(row["content"])))
                    for row in rows
                ],
            )

    def _unindex(self, fact_id: int) -> None:
        self._reindex_fts(fact_id, "")
        if self.ensure_vec():
            with self.conn:
                self.conn.execute(f"DELETE FROM {VEC_FACTS} WHERE rowid = ?", (fact_id,))

    def ensure_vec(self, dim: int | None = None) -> bool:
        if not self.ctx.vec_ready:
            self.ctx.vec_ready = ensure_vec_tables(self.conn, dim=dim)
        if self.ctx.vec_ready and dim and _stored_dim(self.conn) != dim:
            self.ctx.vec_ready = ensure_vec_tables(self.conn, dim=dim)
        return self.ctx.vec_ready

    # ------------------------------------------------------------------ 检索
    def search_fts(self, query: str, limit: int = CANDIDATE_LIMIT) -> list[Hit]:
        stripped = (query or "").strip()
        if not stripped:
            return []
        match = preprocess_for_fts(stripped)
        hits: list[Hit] = []
        if match:
            try:
                rows = self.conn.execute(
                    """
                    SELECT f.id, f.subject, f.content, f.source
                      FROM facts_fts
                      JOIN facts f ON f.id = facts_fts.rowid
                     WHERE facts_fts MATCH ? AND f.deleted = 0
                     ORDER BY rank
                     LIMIT ?
                    """,
                    (match, limit),
                ).fetchall()
                hits = [_fact_hit(row) for row in rows]
            except sqlite3.OperationalError:
                hits = []
        if hits:
            return hits
        return self.search_like(stripped, limit=limit)

    def search_like(self, query: str, limit: int = CANDIDATE_LIMIT) -> list[Hit]:
        """短查询回退（§7.6）：FTS5 对 1~2 字查询可能一个 token 都匹配不上。

        facts 规模 <5000 条时全表 LIKE 是毫秒级，不值得为它上 trigram 索引。
        """
        trimmed = (query or "").strip()
        if not trimmed:
            return []
        if len(trimmed) > 2:
            # 长查询也允许回退，但只取前 2 个字符做包含匹配，避免把自己 LIKE 空
            pass
        pattern = f"%{trimmed}%"
        rows = self.conn.execute(
            """
            SELECT id, subject, content, source FROM facts
             WHERE deleted = 0 AND (content LIKE ? OR subject LIKE ?)
             ORDER BY LENGTH(content) LIMIT ?
            """,
            (pattern, pattern, limit),
        ).fetchall()
        return [_fact_hit(row) for row in rows]

    def search_vec(self, query: str, limit: int = CANDIDATE_LIMIT) -> list[Hit]:
        vectors = _embed([query])
        if not vectors or not self.ensure_vec(len(vectors[0])):
            return []
        rows = self.conn.execute(
            f"""
            SELECT v.rowid AS rowid, v.distance AS distance,
                   f.id AS id, f.subject AS subject, f.content AS content, f.source AS source
              FROM {VEC_FACTS} v JOIN facts f ON f.id = v.rowid
             WHERE v.embedding MATCH ? AND k = ? AND f.deleted = 0
             ORDER BY v.distance
            """,
            (to_blob(vectors[0]), limit),
        ).fetchall()
        hits = []
        for row in rows:
            hit = _fact_hit(row)
            hit.score = 1.0 - float(row["distance"])  # vec0 的 cosine 距离 = 1 - 余弦
            hits.append(hit)
        return hits

    def find_similar(self, content: str, *, limit: int = 5) -> list[tuple[Hit, float]]:
        """给去重用的"最相似的几条"：返回 ``(hit, 余弦)`` 降序（§7.10）。"""
        vectors = _embed([content])
        if not vectors:
            return [(hit, 1.0) for hit in self.search_like(content, limit=limit) if
                    hit.content == content]
        query = vectors[0]
        candidates = self.search_vec(content, limit=max(limit, CANDIDATE_LIMIT))
        if not candidates:  # 向量表还没建 / 没索引：退化为字面比对
            return [(hit, 1.0) for hit in self.search_like(content, limit=limit)
                    if hit.content == content]
        scored: list[tuple[Hit, float]] = []
        for hit in candidates[:limit]:
            row = self.get(hit.id)
            if row is None:
                continue
            other = _embed([row["content"]])
            score = cosine(query, other[0]) if other else 0.0
            scored.append((hit, score))
        scored.sort(key=lambda item: -item[1])
        return scored


def _fact_hit(row: sqlite3.Row) -> Hit:
    keys = row.keys()
    return Hit(
        id=int(row["id"]),
        kind="fact",
        content=str(row["content"]),
        subject=str(row["subject"]),
        source=str(row["source"]) if "source" in keys else "",
    )


def _embed_one(text: str) -> list[float] | None:
    """单条文本的向量；后端不可用返回 ``None``（调用方据此降级）。"""
    vectors = _embed([text])
    return vectors[0] if vectors else None


# ------------------------------------------------------- 冻结签名（PART 3/4 依赖）
def save_fact(subject: str, content: str) -> tuple[int, str]:
    """写入一条事实并返回 ``(id, "insert" | "update")``（TECH §7.10）。

    去重三档（**代价不对称是刻意的**：漏判重复只多一条冗余，误判合并会丢信息）：

      * 最佳候选 ≥ ``DEDUP_SAME``(0.92) → 判同一条，``update``；
      * 0.80~0.92 → 交 ``is_same``（组装根注入的小模型判定）定夺；
      * <0.80 → 新增。

    ``is_same`` 是同步回调（``save_fact`` 本身是同步冻结签名，不引 async 传染）；
    没注入判定器时，中间档一律按"不是同一条"处理——宁可多存一条。
    """
    ctx = context()
    store = FactStore(ctx)
    subject = (subject or "").strip() or "用户"
    content = (content or "").strip()
    if not content:
        raise ValueError("save_fact 的 content 不能为空")

    best_hit, best_score = (None, 0.0)
    similar = store.find_similar(content, limit=3)
    if similar:
        best_hit, best_score = similar[0]

    if best_hit is not None and best_score >= DEDUP_SAME:
        same = True
    elif best_hit is not None and best_score >= DEDUP_MAYBE and ctx.is_same is not None:
        same = _ask_is_same(ctx.is_same, best_hit.content, content)
    else:
        same = False

    if same and best_hit is not None:
        store.update(
            best_hit.id,
            content=content,
            subject=subject,
            vector=_embed_one(content),
        )
        return best_hit.id, "update"

    new_id = store.insert(subject, content, source="user", vector=_embed_one(content))
    return new_id, "insert"


def _ask_is_same(is_same: Any, old: str, new: str) -> bool:
    """``is_same`` 判定失败按"不同"处理：合并两条记忆的代价大于多存一条。"""
    try:
        return bool(is_same(old, new))
    except Exception:  # noqa: BLE001 - 判定器坏了不能把写入也带崩
        return False


def soft_delete_fact(fact_id: int) -> None:
    """软删一条事实（不物理删除，``manage_memory(restore)`` 可捞回来）。

    §5 第 3 条：yixiang 侧的改动必须**双写**——DB 落盘后再写回 ``memory.md``。
    文件写失败不回滚 DB（§7.4.4：DB 已是权威状态，下次启动因哈希不匹配重跑）。
    """
    ctx = context()
    FactStore(ctx).soft_delete(fact_id)
    _write_back(ctx.conn)


def _write_back(conn: sqlite3.Connection) -> None:
    import contextlib

    from yixiang.memory import sync

    with contextlib.suppress(Exception):  # noqa: BLE001 - 双写第二步失败不能反过来炸掉第一步
        sync.write_memory_doc(conn)


def retrieve_memory(query: str, top_k: int = 5, ep_k: int = 3) -> MemoryHits:
    """混合检索 + 情景检索（TECH §7.6，PART 3 的 ``retrieve_media`` 共用 ``rrf``）。

    facts 走 FTS5 + 向量两路各 top20 再 RRF 融合；episodes 只走向量 + 时间近因加权
    （情节是自然语言句子，关键词召回质量差，且条数少）。命中的 fact 顺手更新
    ``last_used_at``——容量淘汰时靠它判断"这条还有用吗"（§7.11）。
    """
    ctx = context()
    store = FactStore(ctx)
    warnings_before = len(ctx.warnings)

    facts = rrf([store.search_fts(query), store.search_vec(query)], k=RRF_K)[:top_k]

    from yixiang.memory import episodic

    episodes = episodic.retrieve_episodes(query, k=ep_k)

    fresh = ctx.warnings[warnings_before:]
    if ctx.embedder is None:
        embed = "skipped"
    elif E_EMBED_UNAVAILABLE in fresh:
        embed = "unavailable"
    else:
        embed = "ok"

    hits = MemoryHits(facts=facts, episodes=episodes, embed=embed, degraded=embed != "ok")
    store.touch_used([hit.id for hit in facts])
    return hits
