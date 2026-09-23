"""``yixiang.rag.ingest`` 包门面（Task 24 拆分，对外名字逐字不变：13 个）。

    models.py     载体（MediaItem / IngestReport）
    normalize.py  来源 → MediaItem（Bangumi / TMDb / 本地文件）
    store.py      写库与向量（upsert_item / ingest_items）+ 共用工具与抓取口径常数
    fetch.py      抓取与原始 JSON 缓存（fetch_bangumi / fetch_tmdb / 退避重试）

依赖是单向的：``models ← store ← normalize ← fetch``；``store`` 故意放最下层，
因为 ``normalize`` 与 ``fetch`` 都要用它的年份 / 浮点 / slug 小工具。**别合回去**。
"""

from __future__ import annotations

from yixiang.rag.ingest.fetch import (
    BANGUMI_ENDPOINT,
    TMDB_ENDPOINT,
    fetch_bangumi,
    fetch_tmdb,
)
from yixiang.rag.ingest.models import IngestReport, MediaItem
from yixiang.rag.ingest.normalize import load_local, normalize_item
from yixiang.rag.ingest.store import (
    CURSOR_META,
    MAX_RETRIES,
    REQUEST_INTERVAL_S,
    ingest_items,
    upsert_item,
)
from yixiang.rag.ingest.store import RAW_DIRNAME as RAW_DIRNAME

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
