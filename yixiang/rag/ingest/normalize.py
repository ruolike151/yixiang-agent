"""来源 → ``MediaItem``（Bangumi / TMDb / 本地文件）。

``local`` 是**离线等价入口**：没有网络也能把整条链路跑通。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from yixiang.rag import retrieve, taste
from yixiang.rag.ingest.models import MediaItem
from yixiang.rag.ingest.store import (
    _BANGUMI_PLATFORM,
    _MAX_GENRES,
    _bangumi_cover,
    _float_or_none,
    _year_of,
)


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
