"""情景记忆（TECH §7.6、§7.7）：``episodes`` 的写入与**纯向量**检索。

为什么 episodes 不像 facts 那样走向量+关键词混合检索：

  * 情节是一句自然语言（"用户开始两周 RAG 复习计划"），jieba 分出来的词
    基本没有区分度，关键词召回只会引入噪声；
  * 条数天然少（每天 1~2 条），全量向量扫描的代价可以忽略。

时间近因加权 ``1.0 + 0.2 * exp(-days / 30)``：30 天内的情节最多 +20%，
让"上周说的"压过"半年前说的"，又不至于把语义相似度整个淹没（§7.6）。

嵌入不可用（没装模型 / 没有 sqlite-vec）时检索返回空列表：情景是**补充**
信息，宁可这轮少注入几条，也不能因为拼不出向量把整轮打挂（§7.12）。
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from yixiang.memory import semantic
from yixiang.memory.semantic import Hit, to_blob
from yixiang.runtime.models import Clock, SystemClock, to_local_iso

# 单条 episode 的正文上限：情节只是"发生了什么"的一句话，太长就该拆
SUMMARY_LIMIT = 500
# 向量召回的候选数（§7.6 的 limit=10），再按近因加权取 top-k
CANDIDATE_LIMIT = 10
RECENCY_GAIN = 0.2
RECENCY_SCALE_DAYS = 30.0


@dataclass(slots=True)
class EpisodicContext:
    """情景记忆的运行时上下文（由 ``yixiang.memory.configure`` 注入）。"""

    conn: sqlite3.Connection
    data_dir: Path
    clock: Clock


_context: EpisodicContext | None = None


def configure(
    conn: sqlite3.Connection,
    *,
    data_dir: Path | str,
    clock: Clock | None = None,
) -> EpisodicContext:
    global _context
    _context = EpisodicContext(
        conn=conn,
        data_dir=Path(data_dir),
        clock=clock or SystemClock(),
    )
    return _context


def reset() -> None:
    global _context
    _context = None


def context() -> EpisodicContext:
    if _context is None:
        raise RuntimeError("情景记忆未装配：先调用 yixiang.memory.configure(...)")
    return _context


def _now() -> str:
    return to_local_iso(context().clock.now())


# ------------------------------------------------------------------ 写入
def add_episode(
    happened_at: str,
    summary: str,
    *,
    session_id: str = "",
    source_chat_id: int | None = None,
) -> int:
    """写一条情节：主表 + 向量索引；``happened_at`` 为空时取当前时间。"""
    ctx = context()
    text = (summary or "").strip()
    if not text:
        raise ValueError("add_episode 的 summary 不能为空")
    when = (happened_at or "").strip() or _now()
    with ctx.conn:
        cursor = ctx.conn.execute(
            """
            INSERT INTO episodes(happened_at, summary, session_id, source_chat_id, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (when, text[:SUMMARY_LIMIT], session_id or None, source_chat_id, _now()),
        )
    episode_id = int(cursor.lastrowid)
    _index(episode_id, text[:SUMMARY_LIMIT])
    return episode_id


def delete_episode(episode_id: int) -> bool:
    """物理删除一条情节（``memory`` 治理命令用；episode 不参与软删体系）。"""
    ctx = context()
    row = ctx.conn.execute("SELECT id FROM episodes WHERE id = ?", (episode_id,)).fetchone()
    if row is None:
        return False
    with ctx.conn:
        ctx.conn.execute("DELETE FROM episodes WHERE id = ?", (episode_id,))
        if semantic.ensure_vec_tables(ctx.conn):
            ctx.conn.execute(
                f"DELETE FROM {semantic.VEC_EPISODES} WHERE rowid = ?", (episode_id,)
            )
    return True


def _index(episode_id: int, summary: str) -> None:
    """把一条情节的向量写进 ``episodes_vec``；嵌入不可用就静默跳过（可检索性降级）。"""
    ctx = context()
    vectors = semantic._embed([summary])
    if not vectors:
        return
    if not semantic.ensure_vec_tables(ctx.conn, dim=len(vectors[0])):
        return
    with ctx.conn:
        ctx.conn.execute(f"DELETE FROM {semantic.VEC_EPISODES} WHERE rowid = ?", (episode_id,))
        ctx.conn.execute(
            f"INSERT INTO {semantic.VEC_EPISODES}(rowid, embedding) VALUES (?, ?)",
            (episode_id, to_blob(vectors[0])),
        )


def rebuild_vec() -> int:
    """全量重建 ``episodes_vec``（``memory rebuild`` 用）；嵌入不可用返回 0。"""
    ctx = context()
    rows = ctx.conn.execute("SELECT id, summary FROM episodes ORDER BY id").fetchall()
    if not rows:
        return 0
    vectors = semantic._embed([str(row["summary"]) for row in rows])
    if not vectors:
        return 0
    if not semantic.ensure_vec_tables(ctx.conn, dim=len(vectors[0])):
        return 0
    with ctx.conn:
        ctx.conn.execute(f"DELETE FROM {semantic.VEC_EPISODES}")
        ctx.conn.executemany(
            f"INSERT INTO {semantic.VEC_EPISODES}(rowid, embedding) VALUES (?, ?)",
            [(int(row["id"]), to_blob(vector)) for row, vector in zip(rows, vectors, strict=False)],
        )
    return len(rows)


# ------------------------------------------------------------------ 检索
def recency_boost(happened_at: str, now: datetime) -> float:
    """``1.0 + 0.2 * exp(-days / 30)``；时间解析不了就按 1.0（不加权，不报错）。"""
    moment = _parse(happened_at)
    if moment is None:
        return 1.0
    days = max((now - moment).total_seconds() / 86_400.0, 0.0)
    return 1.0 + RECENCY_GAIN * math.exp(-days / RECENCY_SCALE_DAYS)


def retrieve_episodes(query: str, k: int = 3) -> list[Hit]:
    """纯向量召回 + 近因加权，返回 top-k（嵌入不可用 → 空列表）。"""
    ctx = context()
    if k <= 0 or not (query or "").strip():
        return []
    vectors = semantic._embed([query])
    if not vectors:
        return []
    if not semantic.ensure_vec_tables(ctx.conn, dim=len(vectors[0])):
        return []
    rows = ctx.conn.execute(
        f"""
        SELECT e.id AS id, e.summary AS summary, e.happened_at AS happened_at,
               e.session_id AS session_id, v.distance AS distance
          FROM {semantic.VEC_EPISODES} v JOIN episodes e ON e.id = v.rowid
         WHERE v.embedding MATCH ? AND k = ?
         ORDER BY v.distance
        """,
        (to_blob(vectors[0]), max(int(k), CANDIDATE_LIMIT)),
    ).fetchall()
    now = ctx.clock.now()
    hits: list[Hit] = []
    for row in rows:
        hit = Hit(
            id=int(row["id"]),
            kind="episode",
            content=str(row["summary"]),
            happened_at=str(row["happened_at"] or ""),
            source=str(row["session_id"] or ""),
        )
        hit.score = (1.0 - float(row["distance"])) * recency_boost(hit.happened_at, now)
        hits.append(hit)
    hits.sort(key=lambda item: (-item.score, item.id))
    return hits[: int(k)]


def list_episodes(limit: int = 20) -> list[dict[str, Any]]:
    """最近的情节列表（``yixiang memory list`` 用；新→旧）。"""
    ctx = context()
    rows = ctx.conn.execute(
        """
        SELECT id, happened_at, summary, session_id, created_at
          FROM episodes
         ORDER BY id DESC
         LIMIT ?
        """,
        (int(limit),),
    ).fetchall()
    return [dict(row) for row in rows]


def _parse(value: str) -> datetime | None:
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else moment.astimezone()


__all__ = [
    "EpisodicContext",
    "add_episode",
    "configure",
    "context",
    "delete_episode",
    "list_episodes",
    "rebuild_vec",
    "recency_boost",
    "reset",
    "retrieve_episodes",
]
