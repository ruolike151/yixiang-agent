"""Bangumi live 工具：``bangumi_search`` / ``bangumi_subject``（TECH §9.2）。

**与 `search_media` 的分工**（2026-09-20 实测，别互相替代）：

  * ``keyword`` 只匹配**标题 / 别名**：``keyword="悬疑"`` → total **0**，而
    ``filter.tag=["悬疑"]`` → total **628**。所以"有没有叫 X 的番"走这里，
    "讲什么题材的"仍走本地语料 + 向量（``search_media``）。
  * 免 token：这两个接口不带认证头就能用（实测）。PAT 只在读 ``users/-`` 自称接口、
    私密收藏与 R18 过滤时才需要（Task 28）。
  * 硬过滤是 live 才有的能力：``air_date [">=2020-01-01"] + rating [">=8"]`` → total **175**。

四条不变量（改之前先读）：

  1. **分页参数只走 query**：``limit`` 塞进 body 会被接口**静默忽略**（Task 26 的同一个坑），
     且单页上限实测为 **20**（``limit=50`` 被压回 20）。
  2. **出口一律包 ``<external_content source="bangumi">``**（§14.3-2）：番剧简介与标签
     是不可信文本，和影视语料走同一道防线。
  3. **失败降级，不静默空手而归**：Bangumi 不可达时退到本地语料检索（``search_media``），
     并说清"下面这段是本地兜底"。
  4. **时间预算由本模块兜住**：``Tool.timeout_s`` 目前没有任何地方强制
     （``registry.run`` 只是声明），所以必须给 httpx 显式的 ``timeout=``。
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

from yixiang.runtime.external import open_tag, wrap_external
from yixiang.tools import media
from yixiang.tools.registry import error_text

API_ROOT = "https://api.bgm.tv/v0"
SEARCH_ENDPOINT = f"{API_ROOT}/search/subjects"
SUBJECT_ENDPOINT = f"{API_ROOT}/subjects/{{id}}"

EXTERNAL_SOURCE = "bangumi"
EXTERNAL_OPEN = open_tag(EXTERNAL_SOURCE)

# Bangumi 要求带可识别的 UA；默认的 httpx UA 更容易被限流挡下
USER_AGENT = "yixiang/0.1 (personal agent; https://api.bgm.tv)"
TIMEOUT_S = 8.0
# 实测 limit=50 被压回 20——单页上限就是 20，别再往上要
MAX_LIMIT = 20
DEFAULT_LIMIT = 5
# 失败重试 1 次（共 2 次尝试）。限速与 `yixiang.rag.ingest.REQUEST_INTERVAL_S` 同值：
# 两个模块对着同一条接口，改一处别忘了另一处（有用例钉着这个相等关系）。
MAX_ATTEMPTS = 2
REQUEST_INTERVAL_S = 1.0
# 单次输出自限（注册表还有 2000 字的兜底，这里按"分组条数"先截）
MAX_ROWS = 10
# 职员只留关键岗位：实测 /persons 有 296 条，全塞进 prompt 会淹掉答案
STAFF_KEYWORDS = ("导演", "脚本", "系列构成", "原作", "人物设定", "音乐")


def subject_url(subject_id: int) -> str:
    return SUBJECT_ENDPOINT.format(id=int(subject_id))


def relations_url(subject_id: int) -> str:
    return f"{subject_url(subject_id)}/subjects"


def persons_url(subject_id: int) -> str:
    return f"{subject_url(subject_id)}/persons"


# ------------------------------------------------------------------ 检索
def search_bangumi(
    conn: sqlite3.Connection | None,
    now: Callable[[], datetime],
    keyword: str | None = None,
    tag: str | None = None,
    air_date_from: str | None = None,
    rating_min: float | None = None,
    limit: int = DEFAULT_LIMIT,
    client: Any = None,
    sleep: Callable[[float], None] | None = None,
) -> str:
    """按标题/别名 + 硬过滤在 Bangumi 上**实时**搜番，返回条目行与检索口径。

    ``keyword`` 与 ``tag`` 至少要给一个；两个都不给是**可行动的错误**，不是异常。
    所以 ``keyword`` 必须有默认值：位置必填会让"只按题材找"的调用在进函数体之前
    就 ``TypeError``，下面那句 missing_query 兜底永远走不到（2026-09-22 实测）。
    """
    text = str(keyword or "").strip()
    tag_text = str(tag or "").strip()
    if not text and not tag_text:
        # JSON Schema 表达不了 anyOf（registry 的 _validate 只看 required/type/enum），
        # 所以"keyword 或 tag 至少一个"由这里兜，并给出可行动的提示
        return error_text(
            "missing_query",
            "keyword",
            "keyword 与 tag 至少要给一个：按片名找传 keyword（如 '夏日重现'），按题材找传 tag（如 '悬疑'）",
        )

    # 只有 tag 时按题材翻条目：口径与 ``ingest.fetch_bangumi`` 的批量抓取一致
    # （``00-context.md`` 实测：关键词为空时 ``match`` / ``rank`` / ``score`` 全是陷阱，
    # ``heat`` 才是"按热度翻页"；`"悬疑"` → total 628 就是这么量出来的）。
    body: dict[str, Any] = {
        "keyword": text,
        "sort": "match" if text else "heat",
        "filter": {"type": [2]},
    }
    if tag_text:
        body["filter"]["tag"] = [tag_text]
    if air_date_from:
        body["filter"]["air_date"] = [f">={str(air_date_from).strip()}"]
    if rating_min is not None:
        # ``:g`` 与 ``ingest.fetch_bangumi`` 同口径：8 写成 "8"、7.5 写成 "7.5"
        body["filter"]["rating"] = [f">={_clamp_rating(rating_min):g}"]
    params = {"limit": _clamp_limit(limit), "offset": 0}

    try:
        payload = _request(SEARCH_ENDPOINT, body=body, params=params, client=client, sleep=sleep)
    except Exception as exc:  # 网络 / 状态码 / JSON 解析：都能降级
        return _degrade(conn, now, text or tag_text, exc)

    rows = payload.get("data") or []
    if not rows:
        return (
            f"（Bangumi 没有匹配「{text or tag_text}」的动画。注意 keyword 只匹配标题与别名，"
            "按题材找请改用 tag 参数，例如 tag='悬疑'）"
        )
    total = payload.get("total") or len(rows)
    # 排序口径要和上面 body 里的 sort 说的一致：没有关键词时是 heat，不是相关度
    order = "按相关度" if text else "按热度"
    lines = [f"Bangumi 实时检索「{text or tag_text}」的前 {len(rows)} 条（共 {total} 条，{order}）："]
    for index, row in enumerate(rows, start=1):
        lines.append(f"{index}. {_render_row(row)}")
    return wrap_external("\n".join(lines), source=EXTERNAL_SOURCE)


def _render_row(row: Any) -> str:
    """一行一条：中文名（原名）· 首播 · 评分 · 标签 · id（id 供 bangumi_subject 接着查）。"""
    if not isinstance(row, dict):
        return "（这条数据格式不认识）"
    title = str(row.get("name_cn") or "").strip()
    original = str(row.get("name") or "").strip()
    if title and original and original != title:
        name = f"{title}（{original}）"
    else:
        name = title or original or "（无标题）"
    tags = "、".join(
        str(item.get("name"))
        for item in (row.get("tags") or [])
        if isinstance(item, dict) and item.get("name")
    )
    tail = f" · 标签 {tags}" if tags else ""
    return (
        f"{name} · {str(row.get('date') or '未知首播').strip()}"
        f" · 评分 {_score_of(row)} · id={row.get('id')}{tail}"
    )


def _score_of(row: dict[str, Any]) -> str:
    """``rating`` 是 ``{"score": ..., "rank": ...}``；没评分时如实说"暂无"。"""
    rating = row.get("rating")
    value = rating.get("score") if isinstance(rating, dict) else rating
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not value:
        return "暂无"
    return f"{float(value):.1f}"


def _degrade(
    conn: sqlite3.Connection | None,
    now: Callable[[], datetime],
    query: str,
    exc: Exception,
) -> str:
    """Bangumi 不可达时的唯一出口：退回本地语料，并说清哪一段是哪一段。"""
    if conn is None:
        return error_text(
            "bangumi_unavailable",
            "keyword",
            f"Bangumi 不可达（{_reason(exc)}），且当前会话没有本地语料可退；稍后重试",
        )
    local = media.search_media(conn, now, query)
    return f"Bangumi 不可达（{_reason(exc)}）；下面是本地语料的检索结果：\n{local}"


def _reason(exc: Exception) -> str:
    text = str(exc).strip() or type(exc).__name__
    return text[:120]


# ------------------------------------------------------------------ 条目详情
def bangumi_subject(
    subject_id: int,
    *,
    with_relations: bool = False,
    with_staff: bool = False,
    client: Any = None,
    sleep: Callable[[float], None] | None = None,
) -> str:
    """按 id 取一部番的详情（原名 / 别名 / 首播 / 集数 / 评分 / 简介），可选关联条目与职员。"""
    try:
        sid = int(subject_id)
    except (TypeError, ValueError):
        return error_text("bad_type", "subject_id", "需要一个 Bangumi 条目 id（整数），例如 346873")
    if sid <= 0:
        return error_text("bad_value", "subject_id", f"id 必须是正整数，收到 {sid}")

    owned = client is None
    http = client or _new_client()
    pause = sleep or time.sleep
    try:
        try:
            payload = _request(subject_url(sid), client=http, sleep=pause)
        except Exception as exc:
            return error_text(
                "bangumi_unavailable",
                "subject_id",
                f"Bangumi 打不开 id={sid}（{_reason(exc)}）；先用 bangumi_search 确认这个 id，或稍后重试",
            )
        lines = _render_subject(payload)
        if with_relations:
            lines.extend(_side_call(relations_url(sid), _render_relations, http, pause))
        if with_staff:
            pause(REQUEST_INTERVAL_S)  # 1 req/s：附属请求之间也要让一步
            lines.extend(_side_call(persons_url(sid), _render_staff, http, pause))
    finally:
        if owned:
            http.close()
    return wrap_external("\n".join(lines), source=EXTERNAL_SOURCE)


def _side_call(url: str, render, http: Any, pause: Callable[[float], None]) -> list[str]:
    """附属信息：拿不到就少一段并注明（不重试、不失败——它不该拖住整条答案）。"""
    try:
        payload = _request(url, client=http, sleep=pause, attempts=1)
    except Exception as exc:
        return [f"（这段暂时取不到：{_reason(exc)}）"]
    return render(payload)


def _render_subject(row: Any) -> list[str]:
    if not isinstance(row, dict):
        return ["（Bangumi 返回的条目格式不认识）"]
    title = str(row.get("name_cn") or "").strip()
    original = str(row.get("name") or "").strip()
    lines = [f"Bangumi 条目 id={row.get('id')}：{title or original or '（无标题）'}"]
    if title and original and original != title:
        lines.append(f"原名：{original}")
    lines.append(
        f"首播：{str(row.get('date') or '未知').strip()}"
        f" · 平台：{row.get('platform') or '未知'}"
        f" · 集数：{row.get('total_episodes') or '未知'}"
        f" · 评分：{_score_of(row)}"
    )
    aliases = _aliases_of(row)
    if aliases:
        lines.append("别名：" + "、".join(aliases))
    tags = "、".join(
        str(item.get("name"))
        for item in (row.get("tags") or [])
        if isinstance(item, dict) and item.get("name")
    )
    if tags:
        lines.append("标签：" + tags)
    summary = str(row.get("summary") or "").strip()
    if summary:
        lines.append("简介：" + summary[:200])
    return lines


def _aliases_of(row: dict[str, Any], limit: int = 5) -> list[str]:
    """``infobox`` 是 ``[{"key","value"}]``，「别名」的 value 是 ``[{"v": ...}]``（实测）。"""
    for item in row.get("infobox") or []:
        if not isinstance(item, dict) or str(item.get("key") or "").strip() not in ("别名", "別名"):
            continue
        values = item.get("value")
        if not isinstance(values, list):
            continue
        return [
            str(entry.get("v")).strip()
            for entry in values
            if isinstance(entry, dict) and entry.get("v")
        ][:limit]
    return []


def _render_relations(rows: Any) -> list[str]:
    """``/subjects/{id}/subjects`` 的顶层是 **list**（实测 55 条），元素带 relation。"""
    picked: list[str] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        relation = str(row.get("relation") or "").strip()
        name = str(row.get("name_cn") or row.get("name") or "").strip()
        if not relation or not name:
            continue
        picked.append(f"  - {relation}：{name}（id={row.get('id')}）")
        if len(picked) >= MAX_ROWS:
            break
    return ["关联条目：", *picked] if picked else []


def _render_staff(rows: Any) -> list[str]:
    """``/persons`` 顶层也是 list（实测 296 条），只留关键岗位。"""
    picked: list[str] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        relation = str(row.get("relation") or "").strip()
        name = str(row.get("name") or "").strip()
        if not name or not any(key in relation for key in STAFF_KEYWORDS):
            continue
        picked.append(f"  - {relation}：{name}")
        if len(picked) >= MAX_ROWS:
            break
    return ["主要职员：", *picked] if picked else []


# ------------------------------------------------------------------ HTTP
def _new_client() -> Any:
    import httpx

    return httpx.Client(timeout=TIMEOUT_S, headers={"User-Agent": USER_AGENT})


def _request(
    url: str,
    *,
    body: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
    client: Any = None,
    sleep: Callable[[float], None] | None = None,
    attempts: int = MAX_ATTEMPTS,
) -> Any:
    """最多 ``attempts`` 次的 JSON 请求；全失败就把异常抛给调用方。

    与 ``rag.ingest._request_json`` 的差别是有意为之：① 次数少（live 工具在对话
    回路里，十几秒内就得给答案，不是抓语料的批处理）；② **不吞异常**——调用方
    要靠它决定"降级到本地语料"还是"报 id 不对"；③ 限速只发生在两次尝试之间。
    """
    pause = sleep or time.sleep
    owned = client is None
    http = client or _new_client()
    last: Exception | None = None
    try:
        for attempt in range(max(int(attempts), 1)):
            if attempt:
                pause(REQUEST_INTERVAL_S * attempt)
            try:
                if body is None:
                    response = http.get(url, params=params)
                else:
                    # 分页走 query：塞进 body 会被静默忽略（Task 26 实测）
                    response = http.post(url, json=body, params=params)
                response.raise_for_status()
                return response.json()
            except Exception as exc:
                last = exc
    finally:
        if owned:
            http.close()
    raise RuntimeError(f"{url} 请求失败（尝试 {max(int(attempts), 1)} 次）：{_reason(last or Exception('unknown'))}")


def _clamp_limit(limit: Any) -> int:
    try:
        value = int(limit)
    except (TypeError, ValueError):
        return DEFAULT_LIMIT
    return max(1, min(value, MAX_LIMIT))


def _clamp_rating(rating: Any) -> float:
    try:
        value = float(rating)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(round(value, 1), 10.0))


__all__ = [
    "API_ROOT",
    "DEFAULT_LIMIT",
    "EXTERNAL_OPEN",
    "EXTERNAL_SOURCE",
    "MAX_ATTEMPTS",
    "MAX_LIMIT",
    "REQUEST_INTERVAL_S",
    "SEARCH_ENDPOINT",
    "SUBJECT_ENDPOINT",
    "TIMEOUT_S",
    "USER_AGENT",
    "bangumi_subject",
    "persons_url",
    "relations_url",
    "search_bangumi",
    "subject_url",
]
