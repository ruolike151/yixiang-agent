"""语料入库：Bangumi / TMDb / 本地 JSON → ``media`` + ``media_fts`` + ``media_vec``（TECH §8.1）。

三条纪律（改之前先读）：

  1. **幂等键是 ``source_id``**（``bangumi:12345`` / ``tmdb:603`` / ``local:001``）。
     重复入库不产生新行；标题、类型、标签、简介都没变时**连嵌入都不做**——比对的是
     冻结嵌入文本的 ``content_hash``，它同时覆盖上面四项。
  2. **抓取与写库解耦**：``dry_run=True`` 一个字节都不写；原始 JSON 缓存到
     ``data/raw/``，**文件名含排序与过滤口径**（换排序 / 换地板就是换一份数据，
     不许复用旧缓存），重跑不重抓；游标写 ``meta.ingest_cursor_<source>``，
     ``resume=True`` 接着上次的地方跑。
  3. **限速与容错**：单线程 + ``sleep(1.0)``；失败退避重试 3 次后**跳过并记日志**——
     一条坏数据不该毁掉一次 500 部的入库（§8 风险表）。

没有网络也能把整条链路跑通：``--source local --file <json|jsonl>`` 是**离线等价入口**，
``evals/fixtures/media_sample.json`` 就是它的输入（PART-3 §7 离线纪律）。
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from yixiang import db
from yixiang.rag import retrieve, taste
from yixiang.rag.embed import (
    EmbedUnavailable,
    ReindexRequired,
    content_hash,
    embed_with_cache,
)
from yixiang.runtime.models import Clock, SystemClock, to_local_iso

# 抓取口径（§8.1 / §5 硬约束 9）：单线程，≥1 req/s
BANGUMI_ENDPOINT = "https://api.bgm.tv/v0/search/subjects"
TMDB_ENDPOINT = "https://api.themoviedb.org/3/discover/movie"
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


# --------------------------------------------------------------------- 数据结构
@dataclass(slots=True)
class MediaItem:
    """入库前的一条规范记录（抓取结果与本地文件共用这一种载体）。"""

    source_id: str
    title: str
    title_zh: str = ""
    mtype: str = ""
    year: int | None = None
    genres: list[str] = field(default_factory=list)
    rating: float | None = None
    synopsis: str = ""
    cover_url: str = ""


@dataclass(slots=True)
class IngestReport:
    """一次入库的结果账：写进 stdout 的那几行就是它的 ``summary()``。"""

    source: str = ""
    inserted: int = 0
    updated: int = 0
    skipped: int = 0
    failed: int = 0
    embedded: int = 0
    vec: int = 0
    dry_run: bool = False
    errors: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.inserted + self.updated + self.skipped + self.failed

    def summary(self) -> str:
        parts = [
            f"{self.source or 'ingest'}：共 {self.total} 条",
            f"新增 {self.inserted}",
            f"更新 {self.updated}",
            f"跳过 {self.skipped}",
        ]
        if self.failed:
            parts.append(f"失败 {self.failed}")
        parts.append(f"向量 {self.vec}")
        if self.dry_run:
            parts.append("（--dry-run：未写库）")
        text = " · ".join(parts)
        if self.errors:
            text += "\n" + "\n".join(f"  · {item}" for item in self.errors[:5])
        return text


# --------------------------------------------------------------------- 归一化
def normalize_item(raw: dict[str, Any], *, source: str) -> MediaItem | None:
    """把一来源的原始记录规范化成 ``MediaItem``；不可用的记录返回 ``None``。

    **不入库空记录**（§8 风险表）：连标题都没有的记录直接丢，并让调用方记一条日志。
    """
    match source:
        case "bangumi":
            return _from_bangumi(raw)
        case "tmdb":
            return _from_tmdb(raw)
        case _:
            return _from_local(raw, source=source)


def _from_bangumi(raw: dict[str, Any]) -> MediaItem | None:
    subject_id = raw.get("id")
    name_cn = str(raw.get("name_cn") or "").strip()
    name = str(raw.get("name") or "").strip()
    title = name_cn or name
    if subject_id in (None, "") or not title:
        return None
    platform = str(raw.get("platform") or "").strip().lower()
    rating = raw.get("rating")
    score = rating.get("score") if isinstance(rating, dict) else rating
    tags = raw.get("tags")
    genres = [
        str(tag.get("name")).strip()
        for tag in tags or []
        if isinstance(tag, dict) and tag.get("name")
    ][:_MAX_GENRES]
    # 中文名优先（决策：title 与 title_zh 都写中文名，展示与冻结文本口径一致）
    return MediaItem(
        source_id=f"bangumi:{subject_id}",
        title=title,
        title_zh=name_cn or title,
        mtype=_BANGUMI_PLATFORM.get(platform, "tv"),
        year=_year_of(raw.get("date")),
        genres=genres,
        rating=_float_or_none(score),
        synopsis=str(raw.get("summary") or "").strip(),
        cover_url=_bangumi_cover(raw.get("images")),
    )


def _from_tmdb(raw: dict[str, Any]) -> MediaItem | None:
    movie_id = raw.get("id")
    title = str(raw.get("title") or raw.get("name") or "").strip()
    if movie_id in (None, "") or not title:
        return None
    genres = raw.get("genres")
    names = (
        [str(item.get("name")) for item in genres if isinstance(item, dict)]
        if isinstance(genres, list)
        else []
    )
    poster = str(raw.get("poster_path") or "")
    return MediaItem(
        source_id=f"tmdb:{movie_id}",
        title=title,
        title_zh=str(raw.get("title_zh") or title),
        mtype="movie" if raw.get("media_type", "movie") == "movie" else "tv",
        year=_year_of(raw.get("release_date") or raw.get("year")),
        genres=names[:_MAX_GENRES] or taste.split_genres(raw.get("genre_names")),
        rating=_float_or_none(raw.get("vote_average")),
        synopsis=str(raw.get("overview") or "").strip(),
        cover_url=f"https://image.tmdb.org/t/p/w500{poster}" if poster else "",
    )


def _from_local(raw: dict[str, Any], *, source: str) -> MediaItem | None:
    """本地 JSON / JSONL：字段名与 ``MediaItem`` 对齐，缺 ``source_id`` 时按序号补。"""
    key = raw.get("source_id") or raw.get("id")
    title = str(raw.get("title") or raw.get("title_zh") or "").strip()
    if not title:
        return None
    if key in (None, ""):
        return None
    source_id = str(key) if ":" in str(key) else f"{source}:{key}"
    return MediaItem(
        source_id=source_id,
        title=title,
        title_zh=str(raw.get("title_zh") or title),
        mtype=retrieve.normalize_mtype(raw.get("mtype")),
        year=_year_of(raw.get("year")),
        genres=taste.split_genres(raw.get("genres")),
        rating=_float_or_none(raw.get("rating")),
        synopsis=str(raw.get("synopsis") or raw.get("summary") or "").strip(),
        cover_url=str(raw.get("cover_url") or ""),
    )


def load_local(path: Path | str, *, source: str = "local") -> list[MediaItem]:
    """读本地语料：``.json``（数组）或 ``.jsonl``（每行一条）。

    ``source`` 只是没有 ``source_id`` 前缀时的兜底命名空间（``local:12``）——
    显式写了 ``bangumi:12345`` 的记录会原样保留自己的命名空间。
    """
    file_path = Path(path)
    raw_text = file_path.read_text(encoding="utf-8")
    records: list[dict[str, Any]] = []
    if file_path.suffix.lower() == ".jsonl":
        records = [json.loads(line) for line in raw_text.splitlines() if line.strip()]
    else:
        payload = json.loads(raw_text)
        records = payload if isinstance(payload, list) else list(payload.get("data") or [])
    items: list[MediaItem] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        item = _from_local(record, source=source)
        if item is not None:
            items.append(item)
    return items


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


# --------------------------------------------------------------------- 抓取
def fetch_bangumi(
    *,
    data_dir: Path | str,
    tags: Iterable[str] = (),
    pages: int = 1,
    since: str = "",
    sort: str = "heat",
    min_rating: float = 0.0,
    min_votes: int = 0,
    want: int = 0,
    conn: sqlite3.Connection | None = None,
    resume: bool = False,
    client: Any = None,
    sleep: Callable[[float], None] | None = None,
    limit: int = PAGE_SIZE,
) -> list[MediaItem]:
    """抓 Bangumi 动画条目（``/v0/search/subjects``）；单线程 + 限速 + 缓存。

    ``since`` 是**手动增量**（``air_date >= YYYY-MM-DD``），不做季度自动追更（PART-3 §2）。
    ``sort`` 默认 ``heat``：实测关键词为空时 ``rank`` 的首条 ``rank=0``（无效排序），
    ``heat`` 才是批量拉取的口径。``min_rating`` / ``min_votes`` 是选番的地板
    （服务端 ``filter.rating`` 只认平均分，**票数只能在客户端判**）；``want`` 数的是
    **实收**——过滤 + 去重之后的条数到数就停，``0`` 表示只按 ``pages`` 抓。
    """
    tag_list = [str(tag).strip() for tag in tags if str(tag).strip()]
    sort = str(sort or "heat").strip() or "heat"
    floor_rating = max(float(min_rating or 0.0), 0.0)
    floor_votes = max(int(min_votes or 0), 0)
    target = max(int(want or 0), 0)
    offset = _start_offset(conn, "bangumi", resume=resume)
    items: list[MediaItem] = []
    # 深翻时接口会重复吐同一批（offset 越过 total 之后就是这样）：实收数按去重后算
    seen: set[str] = set()
    for _ in range(max(int(pages), 1)):
        current = offset
        offset += limit
        body = {"keyword": "", "sort": sort, "filter": {"type": [2]}}
        if tag_list:
            body["filter"]["tag"] = tag_list
        if since:
            body["filter"]["air_date"] = [f">={since}"]
        if floor_rating > 0:
            # 这一道只是省流量（服务端按平均分筛）；判定权在下面的客户端地板
            body["filter"]["rating"] = [f">={floor_rating:g}"]
        payload = _cached_json(
            data_dir,
            _bangumi_cache_name(
                tag_list=tag_list,
                since=since,
                sort=sort,
                min_rating=floor_rating,
                min_votes=floor_votes,
                offset=current,
            ),
            _bangumi_fetcher(body, limit=limit, offset=current, client=client, sleep=sleep),
        )
        for record in payload.get("data") or []:
            if not _bangumi_meets_floor(
                record, min_rating=floor_rating, min_votes=floor_votes
            ):
                continue
            item = normalize_item(record, source="bangumi")
            if item is None or item.source_id in seen:
                continue
            seen.add(item.source_id)
            items.append(item)
        if conn is not None:
            db.set_meta(conn, CURSOR_META.format(source="bangumi"), str(offset))
        if target and len(items) >= target:
            break
    return items


def fetch_tmdb(
    *,
    data_dir: Path | str,
    api_key: str = "",
    genre_ids: Iterable[str] = (),
    pages: int = 1,
    since: str = "",
    conn: sqlite3.Connection | None = None,
    resume: bool = False,
    client: Any = None,
    sleep: Callable[[float], None] | None = None,
    limit: int = PAGE_SIZE,
) -> list[MediaItem]:
    """抓 TMDb ``/discover/movie``（``genre_ids`` 是 TMDb 的数字类型 id）。

    ``since`` 走 ``primary_release_date.gte``，与 Bangumi 一样只做手动增量。
    """
    genres = [str(item).strip() for item in genre_ids if str(item).strip()]
    offset = _start_offset(conn, "tmdb", resume=resume)
    items: list[MediaItem] = []
    for _ in range(max(int(pages), 1)):
        current = offset
        offset += limit
        params = {
            "api_key": api_key,
            "language": "zh-CN",
            "page": str(current // max(limit, 1) + 1),
        }
        if genres:
            params["with_genres"] = ",".join(genres)
        if since:
            params["primary_release_date.gte"] = since
        payload = _cached_json(
            data_dir,
            f"tmdb-{_slug(','.join(genres) or 'all')}-{_slug(since or 'all')}-{current}.json",
            _tmdb_fetcher(params, client=client, sleep=sleep),
        )
        for record in payload.get("results") or []:
            item = normalize_item(record, source="tmdb")
            if item is not None:
                items.append(item)
        if conn is not None:
            db.set_meta(conn, CURSOR_META.format(source="tmdb"), str(offset))
    return items


def _start_offset(conn: sqlite3.Connection | None, source: str, *, resume: bool) -> int:
    """``--resume`` 时接着游标跑；没有游标就从 0 开始（不是错误）。"""
    if conn is None or not resume:
        return 0
    stored = db.get_meta(conn, CURSOR_META.format(source=source))
    if not stored:
        return 0
    try:
        return int(stored)
    except ValueError:
        return 0


def _bangumi_fetcher(
    body: dict[str, Any],
    *,
    limit: int,
    offset: int,
    client: Any,
    sleep: Callable[[float], None] | None,
) -> Callable[[], dict[str, Any]]:
    """绑定好本页参数再交给 ``_cached_json``（避免闭包捕获到循环变量）。

    分页参数走 **query**：``/v0/search/subjects`` 只认 URL 上的 ``limit`` / ``offset``，
    塞进 body 会被**静默忽略**（实测 5 页拿回同一页 20 条）。
    """
    return lambda: _post_json(
        BANGUMI_ENDPOINT,
        body,
        params={"limit": limit, "offset": offset},
        client=client,
        sleep=sleep,
    )


def _bangumi_cache_name(
    *,
    tag_list: list[str],
    since: str,
    sort: str,
    min_rating: float,
    min_votes: int,
    offset: int,
) -> str:
    """原始 JSON 的文件名：**换排序 / 换地板就是换一份数据，不许复用旧缓存**。

    大盘（``>=8``，从头抓 200 部）与近五年增量（``>=7.5``）用的是**同一批 offset**，
    过滤口径不进文件名，第二段就会静默读到第一段写下的旧页。
    """
    parts = ["bangumi", _slug("-".join(tag_list) or "all"), _slug(since or "all"), sort]
    if min_rating > 0:
        parts.append(f"r{min_rating:g}")
    if min_votes > 0:
        parts.append(f"v{min_votes}")
    parts.append(str(offset))
    return "-".join(parts) + ".json"


def _bangumi_meets_floor(
    record: dict[str, Any], *, min_rating: float, min_votes: int
) -> bool:
    """客户端地板：``filter.rating`` 只认平均分，**票数下限只能自己判**。

    实测：``sort=score`` 的首条是"1 票 10 分"的冷门条目——只按平均分选番，
    "高分榜"里会混进没人看过的条目（2026 未播新番就是活例：8.9 分 / 20 票）。
    """
    if min_rating <= 0 and min_votes <= 0:
        return True
    rating = record.get("rating")
    stats = rating if isinstance(rating, dict) else {}
    if min_rating > 0:
        score = _float_or_none(stats.get("score"))
        if score is None or score < min_rating:
            return False
    if min_votes > 0:
        count = _bangumi_votes(stats)
        if count is None or count < min_votes:
            return False
    return True


def _bangumi_votes(stats: dict[str, Any]) -> int | None:
    """评分人数：``rating.total``；它缺失时用打分分布 ``rating.count`` **求和**。

    实测（2026-09-21，``/v0/search/subjects``）：``rating.total`` 是人数，
    ``rating.count`` 是**分布字典**（``{"10": 6895, …, "1": 196}``，求和后与 ``total``
    相等，如 孤独摇滚 40921）。所以 ``count`` 不能直接 ``int()``——那是另一种口径。
    """
    total = _int_or_none(stats.get("total"))
    if total is not None:
        return total
    counts = stats.get("count")
    if isinstance(counts, dict):
        values = [_int_or_none(value) for value in counts.values()]
        kept = [value for value in values if value is not None]
        return sum(kept) if kept else None
    return _int_or_none(counts)


def _tmdb_fetcher(
    params: dict[str, Any],
    *,
    client: Any,
    sleep: Callable[[float], None] | None,
) -> Callable[[], dict[str, Any]]:
    return lambda: _get_json(TMDB_ENDPOINT, params=params, client=client, sleep=sleep)


def _cached_json(
    data_dir: Path | str, name: str, fetcher: Callable[[], dict[str, Any]]
) -> dict[str, Any]:
    """原始 JSON 缓存：命中就不发请求（重跑不重抓，§8 风险表）。"""
    path = Path(data_dir) / RAW_DIRNAME / name
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    payload = fetcher()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def _post_json(
    url: str,
    body: dict[str, Any],
    *,
    params: dict[str, Any] | None = None,
    client: Any = None,
    sleep: Callable[[float], None] | None = None,
) -> dict[str, Any]:
    """POST 也能带 query 参数：Bangumi 的分页参数**只认 URL**（见 ``_bangumi_fetcher``）。"""
    return _request_json(
        "POST", url, body=body, params=params, client=client, sleep=sleep
    )


def _get_json(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    client: Any = None,
    sleep: Callable[[float], None] | None = None,
) -> dict[str, Any]:
    return _request_json("GET", url, params=params, client=client, sleep=sleep)


def _request_json(
    method: str,
    url: str,
    *,
    body: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
    client: Any = None,
    sleep: Callable[[float], None] | None = None,
) -> dict[str, Any]:
    """一次重试封装的 JSON 请求：最多 ``MAX_RETRIES`` 次，全失败就抛。

    退避节奏（``REQUEST_INTERVAL_S = 1.0`` 时的**实测**序列，别照抄成 1/2/3）：

      * 第 1 次尝试失败 → 睡 ``1.0s``；
      * 第 2 次尝试前再睡 ``1.0s``，失败后再睡 ``1.0s``；
      * 第 3 次尝试前睡 ``2.0s``（``attempt × REQUEST_INTERVAL_S``），失败后再睡 ``1.0s``。

    也就是"每次失败固定 1s + 每次重试前 ``attempt × 1s``"，共 5 段睡眠。
    """
    import httpx

    pause = sleep or time.sleep
    owned = client is None
    http = client or httpx.Client(
        timeout=15.0, headers={"User-Agent": "yixiang/0.1 (personal agent)"}
    )
    last: Exception | None = None
    try:
        for attempt in range(MAX_RETRIES):
            if attempt:
                pause(REQUEST_INTERVAL_S * attempt)
            try:
                if method == "POST":
                    response = http.post(url, json=body, params=params)
                else:
                    response = http.get(url, params=params)
                response.raise_for_status()
                return response.json()
            except Exception as exc:  # 网络 / 状态码 / JSON 解析：都重试
                last = exc
            pause(REQUEST_INTERVAL_S)
    finally:
        if owned:
            http.close()
    raise RuntimeError(f"{url} 抓取失败（重试 {MAX_RETRIES} 次）：{last}")


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


__all__ = [
    "BANGUMI_ENDPOINT",
    "CURSOR_META",
    "IngestReport",
    "MAX_RETRIES",
    "MediaItem",
    "REQUEST_INTERVAL_S",
    "TMDB_ENDPOINT",
    "fetch_bangumi",
    "fetch_tmdb",
    "ingest_items",
    "load_local",
    "normalize_item",
    "upsert_item",
]
