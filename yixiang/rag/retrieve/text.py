"""冻结文本与分词口径（``embed_text_for`` / ``fts_tokens`` / 重建 FTS）。

这两样都是**版本化的契约**：嵌入文本写死进 ``content_hash``，分词策略写进
``meta.fts_tokenizer_version``。改这里等于换一份索引，先读 ``rebuild_media_fts`` 的注释。"""

from __future__ import annotations

import sqlite3
from typing import Any

from yixiang import db
from yixiang.memory.semantic import _cut
from yixiang.rag import taste

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


# ------------------------------------------------------------------ 召回
def normalize_mtype(value: Any) -> str:
    """``"番剧" / "tv" / "电视剧"`` → ``"tv"``；不认识的原样小写返回。"""
    raw = str(value or "").strip().lower()
    if not raw:
        return ""
    return MTYPE_ALIASES.get(raw, raw)
