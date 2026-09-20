"""语料入库：Bangumi / TMDb / 本地 JSON → ``media`` + ``media_fts`` + ``media_vec``（TECH §8.1）。

三条纪律（改之前先读）：

  1. **幂等键是 ``source_id``**（``bangumi:12345`` / ``tmdb:603`` / ``local:001``）。
     重复入库不产生新行；标题、类型、标签、简介都没变时**连嵌入都不做**——比对的是
     冻结嵌入文本的 ``content_hash``，它同时覆盖上面四项。
  2. **抓取与写库解耦**：``dry_run=True`` 一个字节都不写；原始 JSON 缓存到
     ``data/raw/``，重跑不重抓；游标写 ``meta.ingest_cursor_<source>``，
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
    conn: sqlite3.Connection | None = None,
    resume: bool = False,
    client: Any = None,
    sleep: Callable[[float], None] | None = None,
    limit: int = PAGE_SIZE,
) -> list[MediaItem]:
    """抓 Bangumi 动画条目（``/v0/search/subjects``）；单线程 + 限速 + 缓存。

    ``since`` 是**手动增量**（``air_date >= YYYY-MM-DD``），不做季度自动追更（PART-3 §2）。
    """
    tag_list = [str(tag).strip() for tag in tags if str(tag).strip()]
    offset = _start_offset(conn, "bangumi", resume=resume)
    items: list[MediaItem] = []
    for _ in range(max(int(pages), 1)):
        current = offset
        offset += limit
        body = {"keyword": "", "sort": "rank", "filter": {"type": [2]}}
        if tag_list:
            body["filter"]["tag"] = tag_list
        if since:
            body["filter"]["air_date"] = [f">={since}"]
        payload = _cached_json(
            data_dir,
            f"bangumi-{_slug('-'.join(tag_list) or 'all')}-{_slug(since or 'all')}-{current}.json",
            _bangumi_fetcher(body, limit=limit, offset=current, client=client, sleep=sleep),
        )
        for record in payload.get("data") or []:
            item = normalize_item(record, source="bangumi")
            if item is not None:
                items.append(item)
        if conn is not None:
            db.set_meta(conn, CURSOR_META.format(source="bangumi"), str(offset))
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
    """绑定好本页参数再交给 ``_cached_json``（避免闭包捕获到循环变量）。"""
    payload = body | {"limit": limit, "offset": offset}
    return lambda: _post_json(BANGUMI_ENDPOINT, payload, client=client, sleep=sleep)


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
    client: Any = None,
    sleep: Callable[[float], None] | None = None,
) -> dict[str, Any]:
    return _request_json("POST", url, body=body, client=client, sleep=sleep)


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
    """一次重试封装的 JSON 请求：退避 ``1s / 2s / 3s``，3 次都失败就抛。"""
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
                    response = http.post(url, json=body)
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
