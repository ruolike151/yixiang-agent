"""写库与向量（``upsert_item`` / ``ingest_items``）+ 全包共用的小工具与口径常数。

``normalize`` 与 ``fetch`` 都要用 ``_year_of`` / ``_float_or_none`` / ``_slug`` 这些
小工具，所以它们**放最下层**：``normalize`` 与 ``fetch`` 都 import 这里，
这里不 import 它们，依赖才不会成环（与任务文档的「严格单向」表述有偏差，见执行记录）。
抓取口径常数（``MAX_RETRIES`` / ``REQUEST_INTERVAL_S`` / ``CURSOR_META`` …）同理：
抓取与写库都要读，放在最下层。"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterable
from datetime import datetime
from typing import Any

from yixiang import db
from yixiang.rag import retrieve
from yixiang.rag.embed import EmbedUnavailable, ReindexRequired, content_hash, embed_with_cache
from yixiang.rag.ingest.models import IngestReport, MediaItem
from yixiang.runtime.models import Clock, SystemClock, to_local_iso

REQUEST_INTERVAL_S = 1.0


MAX_RETRIES = 3


PAGE_SIZE = 20


RAW_DIRNAME = "raw"


CURSOR_META = "ingest_cursor_{source}"


_YEAR_RE = re.compile(r"(\d{4})")


_MAX_GENRES = 6


# Bangumi ``platform`` → 库里的 mtype
_BANGUMI_PLATFORM = {
    "tv": "tv",
    "web": "tv",
    "ova": "ova",
    "oad": "ova",
    "剧场版": "movie",
    "movie": "movie",
}


# --------------------------------------------------------------------- 写库
def upsert_item(
    conn: sqlite3.Connection,
    item: MediaItem,
    *,
    embedder: Any = None,
    now: datetime | None = None,
) -> str:
    """幂等 upsert 一条：返回 ``inserted`` / ``updated`` / ``skipped``。

    ``skipped`` 表示"库里已有的那条与本次完全相同"——**连嵌入都不做**，
    这是 §8.1 幂等策略的省钱关键（500 部重跑一次是 0 次模型推理）。
    """
    moment = now or datetime.now().astimezone()
    stamp = to_local_iso(moment)
    text = retrieve.embed_text_for(
        title=item.title, mtype=item.mtype, genres=item.genres, synopsis=item.synopsis
    )
    digest = content_hash(text, _model_of(embedder))
    row = conn.execute(
        "SELECT id, title, title_zh, synopsis, embed_text_hash FROM media WHERE source_id = ?",
        (item.source_id,),
    ).fetchone()
    if row is not None and row["embed_text_hash"] == digest:
        return "skipped"

    if row is None:
        with conn:
            cursor = conn.execute(
                "INSERT INTO media(source_id, title, title_zh, mtype, year, genres, rating, "
                "synopsis, cover_url, embed_text_hash, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    item.source_id,
                    item.title,
                    item.title_zh,
                    item.mtype,
                    item.year,
                    "/".join(item.genres),
                    item.rating,
                    item.synopsis,
                    item.cover_url,
                    digest,
                    stamp,
                    stamp,
                ),
            )
        media_id = int(cursor.lastrowid or 0)
        action = "inserted"
    else:
        media_id = int(row["id"])
        # contentless 的 FTS 表删旧行必须带旧列值（§8.4 踩过的坑）
        retrieve.fts_unindex_row(
            conn,
            media_id,
            title=str(row["title"] or ""),
            title_zh=str(row["title_zh"] or ""),
            synopsis=str(row["synopsis"] or ""),
        )
        with conn:
            conn.execute(
                "UPDATE media SET title = ?, title_zh = ?, mtype = ?, year = ?, genres = ?, "
                "rating = ?, synopsis = ?, cover_url = ?, embed_text_hash = ?, updated_at = ? "
                "WHERE id = ?",
                (
                    item.title,
                    item.title_zh,
                    item.mtype,
                    item.year,
                    "/".join(item.genres),
                    item.rating,
                    item.synopsis,
                    item.cover_url,
                    digest,
                    stamp,
                    media_id,
                ),
            )
        action = "updated"

    with conn:
        retrieve.fts_index_row(
            conn,
            media_id,
            title=item.title,
            title_zh=item.title_zh,
            synopsis=item.synopsis,
        )
    _store_vector(conn, media_id, text, embedder=embedder, moment=moment)
    return action


def ingest_items(
    conn: sqlite3.Connection,
    items: Iterable[MediaItem],
    *,
    source: str = "local",
    embedder: Any = None,
    dry_run: bool = False,
    clock: Clock | None = None,
) -> IngestReport:
    """批量入库；``dry_run`` 只统计不写库（写库与抓取解耦的落点）。"""
    report = IngestReport(source=source, dry_run=dry_run)
    moment = (clock or SystemClock()).now()
    for item in items:
        try:
            if dry_run:
                action = _would_do(conn, item, embedder=embedder)
            else:
                action = upsert_item(conn, item, embedder=embedder, now=moment)
        except (EmbedUnavailable, ReindexRequired) as exc:
            report.failed += 1
            report.errors.append(f"{item.source_id}：嵌入失败（{exc}）")
            continue
        except sqlite3.Error as exc:
            report.failed += 1
            report.errors.append(f"{item.source_id}：写库失败（{exc}）")
            continue
        setattr(report, action, getattr(report, action) + 1)
    if not dry_run:
        report.vec = _vec_count(conn)
    return report


def _would_do(conn: sqlite3.Connection, item: MediaItem, *, embedder: Any) -> str:
    text = retrieve.embed_text_for(
        title=item.title, mtype=item.mtype, genres=item.genres, synopsis=item.synopsis
    )
    digest = content_hash(text, _model_of(embedder))
    row = conn.execute(
        "SELECT embed_text_hash FROM media WHERE source_id = ?", (item.source_id,)
    ).fetchone()
    if row is None:
        return "inserted"
    return "skipped" if row["embed_text_hash"] == digest else "updated"


def _store_vector(
    conn: sqlite3.Connection,
    media_id: int,
    text: str,
    *,
    embedder: Any,
    moment: datetime,
) -> None:
    """嵌入并写 ``media_vec``；嵌入不可用就只留 FTS（D-24 的降级路径）。"""
    if embedder is None:
        return
    try:
        vectors = embed_with_cache(
            conn, embedder, [text], created_at=to_local_iso(moment)
        )
    except EmbedUnavailable:
        return
    if not vectors:
        return
    retrieve.write_media_vectors(
        conn,
        [(media_id, vectors[0])],
        model=_model_of(embedder),
        dim=len(vectors[0]),
    )


def _vec_count(conn: sqlite3.Connection) -> int:
    # 新建连接不会自动带上 vec0 —— 只查 media 表是否存在的表名不等于能查它
    # （第二次幂等入库时全被 skipped，没人调过 ensure_media_vec，这里就会炸）
    if not db.load_sqlite_vec(conn):
        return 0
    if retrieve.VEC_MEDIA not in db.table_names(conn):
        return 0
    try:
        row = conn.execute(f"SELECT COUNT(*) AS n FROM {retrieve.VEC_MEDIA}").fetchone()
    except sqlite3.OperationalError:
        return 0
    return int(row["n"]) if row else 0


# --------------------------------------------------------------------- 小工具
def _model_of(embedder: Any) -> str:
    return str(getattr(embedder, "model", "") or "none")


def _year_of(value: Any) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, int) and not isinstance(value, bool):
        return value if 1900 <= value <= 2100 else None
    match = _YEAR_RE.search(str(value))
    if not match:
        return None
    year = int(match.group(1))
    return year if 1900 <= year <= 2100 else None


def _float_or_none(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result or None


def _int_or_none(value: Any) -> int | None:
    """``rating.total`` 在实测里是整数，但接口不给保证（回落到 ``count`` 时更要当心）。"""
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _bangumi_cover(images: Any) -> str:
    if not isinstance(images, dict):
        return ""
    return str(images.get("common") or images.get("medium") or images.get("large") or "")


def _slug(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "-", value).strip("-") or "all"
