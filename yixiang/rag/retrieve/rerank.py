"""重排、解释与推荐留痕（``explain_search`` / ``retrieve_media`` / ``log_recommendation``）。

融合用 PART 2 的 ``rrf()``，硬过滤剔掉近 7 天已推，软加权只微调（口味成不了回声室）。
``explain_search`` 是「检索不是黑盒」的实物：五段中间结果一起返回。"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from typing import Any

from yixiang.memory import rrf
from yixiang.memory.semantic import CANDIDATE_LIMIT, RRF_K
from yixiang.rag import taste
from yixiang.rag.retrieve.context import current
from yixiang.rag.retrieve.models import MediaHit, SearchExplain
from yixiang.rag.retrieve.search import (
    CHANNEL_LABELS,
    _as_hits,
    _channel_map,
    _materialize,
    browse_media,
    search_fts,
    search_vec,
)
from yixiang.rag.retrieve.text import normalize_mtype
from yixiang.runtime.models import to_local_iso

# 检索口径（§8.3 冻结）：候选 20，默认交付 3
DEFAULT_TOP_K = 3


DEFAULT_EXCLUDE_RECENT_DAYS = 7


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
