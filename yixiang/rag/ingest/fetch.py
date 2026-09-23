"""抓取与原始 JSON 缓存（``fetch_bangumi`` / ``fetch_tmdb`` / 退避重试）。

单线程 + ``sleep(1.0)``；失败退避重试 ``MAX_RETRIES`` 次后跳过并记日志——
一条坏数据不该毁掉一次 500 部的入库。原始 JSON 缓存的文件名**含排序与过滤口径**，
换排序 / 换地板就是换一份数据，不许复用旧缓存。"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from yixiang import db
from yixiang.rag.ingest.models import MediaItem
from yixiang.rag.ingest.normalize import normalize_item
from yixiang.rag.ingest.store import (
    CURSOR_META,
    MAX_RETRIES,
    PAGE_SIZE,
    RAW_DIRNAME,
    REQUEST_INTERVAL_S,
    _float_or_none,
    _int_or_none,
    _slug,
)

# 抓取口径（§8.1 / §5 硬约束 9）：单线程，≥1 req/s
BANGUMI_ENDPOINT = "https://api.bgm.tv/v0/search/subjects"


TMDB_ENDPOINT = "https://api.themoviedb.org/3/discover/movie"


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
    proxy: str = "",
) -> list[MediaItem]:
    """抓 Bangumi 动画条目（``/v0/search/subjects``）；单线程 + 限速 + 缓存。

    ``since`` 是**手动增量**（``air_date >= YYYY-MM-DD``），不做季度自动追更（PART-3 §2）。
    ``sort`` 默认 ``heat``：实测关键词为空时 ``rank`` 的首条 ``rank=0``（无效排序），
    ``heat`` 才是批量拉取的口径。``min_rating`` / ``min_votes`` 是选番的地板
    （服务端 ``filter.rating`` 只认平均分，**票数只能在客户端判**）；``want`` 数的是
    **实收**——过滤 + 去重之后的条数到数就停，``0`` 表示只按 ``pages`` 抓。

    ``proxy`` 只喂给 Bangumi 这一条出口（``YIXIANG_BANGUMI_PROXY``）；``fetch_tmdb``
    有意**不收**它——TMDb 是另一个出口，别顺手带上。
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
            _bangumi_fetcher(
                body,
                limit=limit,
                offset=current,
                client=client,
                sleep=sleep,
                proxy=proxy,
            ),
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
    proxy: str = "",
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
        proxy=proxy,
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
    proxy: str = "",
) -> dict[str, Any]:
    """POST 也能带 query 参数：Bangumi 的分页参数**只认 URL**（见 ``_bangumi_fetcher``）。"""
    return _request_json(
        "POST", url, body=body, params=params, client=client, sleep=sleep, proxy=proxy
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
    proxy: str = "",
) -> dict[str, Any]:
    """一次重试封装的 JSON 请求：最多 ``MAX_RETRIES`` 次，全失败就抛。

    退避节奏（``REQUEST_INTERVAL_S = 1.0`` 时的**实测**序列，别照抄成 1/2/3）：

      * 第 1 次尝试失败 → 睡 ``1.0s``；
      * 第 2 次尝试前再睡 ``1.0s``，失败后再睡 ``1.0s``；
      * 第 3 次尝试前睡 ``2.0s``（``attempt × REQUEST_INTERVAL_S``），失败后再睡 ``1.0s``。

    也就是"每次失败固定 1s + 每次重试前 ``attempt × 1s``"，共 5 段睡眠。

    ``proxy`` 只作用于**自己新建**的客户端（``client=`` 传进来时由调用方决定出口）；
    留空时**不写** ``proxy`` 键，httpx 照旧看环境变量 / 系统代理。
    """
    import httpx

    pause = sleep or time.sleep
    owned = client is None
    kwargs: dict[str, Any] = {
        "timeout": 15.0,
        "headers": {"User-Agent": "yixiang/0.1 (personal agent)"},
    }
    if str(proxy or "").strip():
        kwargs["proxy"] = str(proxy).strip()
    http = client or httpx.Client(**kwargs)
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
