"""Bangumi 收藏 → 口味画像（``bangumi_my_collections``，TECH §9.2）。

**与 `search_media` / `recommend_media` 的分工**（2026-09-20 实测，2026-09-21 真跑校正）：

  * 搜索与条目详情免 token（Task 27）；**读自己的收藏要 PAT**：
    读自己的收藏要先 ``GET /v0/me`` 换成本人的用户名（见 ``SELF_USER``），token 走
    ``Authorization: Bearer``；不带 token 时 ``/v0/me`` 是 401。
  * 收藏里的 ``rate``（我打的分）是本地语料永远拿不到的信号：影视语料只有题材，
    没有"我喜欢什么"。
  * 标签必须落回 ``taste.taste_score`` 认得的词。它只对 ``media.genres`` 里出现的词
    加权，而 Bangumi 的高频标签大多是 ``剧场版`` / ``原创`` / ``TV`` / ``2025'8``
    这类**非类型词**——直接灌进 ``user.md``，检索排序一行都不会变。

五条不变量（改之前先读）：

  1. **分页只走 query、单页上限 100**：``subject_type`` / ``type`` / ``limit`` / ``offset``
     全是 query（与 Task 26 的搜索接口相反：那边 ``limit`` 会被压回 20）。
  2. **没有 ``/v0/users/-/collections`` 这个路由**：``-`` 只是本项目里"我"的占位符
      （官方 OpenAPI 里 ``-`` 只出现在单条收藏 ``/v0/users/-/collections/{subject_id}``），
      直接拿 ``-`` 去查列表会 404——那 404 是"用户名不存在"，不是"缺 token"。
      所以 ``user="-"`` 要先经 ``/v0/me`` 换成本人的用户名（``_resolve_self``）。
  3. **token 只走 ``Authorization: Bearer``**，绝不进 query——query 会进日志、进 trace。
  4. **默认 ``write=False``**：先出画像给人看；写 ``user.md`` 要显式 ``write=True``，
     且写前按**整行**查重（同一条偏好跑两遍不许出现两行）。
  5. **出口一律包 ``<external_content source="bangumi">``**（§14.3-2）：标签与简介是不可信
     文本，和影视语料走同一道防线。
"""

from __future__ import annotations

import re
import time
from collections import Counter
from collections.abc import Callable
from typing import Any

import httpx

from yixiang.memory import memory_admin
from yixiang.runtime.external import wrap_external
from yixiang.tools.registry import error_text

# ``API_ROOT`` 已经含 ``/v0``（与 ``tools/bangumi.py`` 同源），别再拼一次
API_ROOT = "https://api.bgm.tv/v0"
COLLECTIONS_ENDPOINT = f"{API_ROOT}/users/{{user}}/collections"
# "我是谁"只有一个来源：拿 token 问 ``/v0/me``（返回里有 ``username`` / ``id``）
ME_ENDPOINT = f"{API_ROOT}/me"

EXTERNAL_SOURCE = "bangumi"
TOKEN_PAGE = "https://next.bgm.tv/demo/access-token"
# Bangumi 要求可识别的 UA；默认的 httpx UA 更容易被限流挡下
# 必须纯 ASCII：httpx 按 ASCII 编码请求头，中文 UA 会在发请求前抛
# `'ascii' codec can't encode characters`（2026-09-21 真跑实测）。
# 口径与 `bangumi.USER_AGENT` 一致——同一个项目对同一个站点自称同一个名字。
USER_AGENT = "yixiang/0.1 (personal agent; https://bgm.tv)"
TIMEOUT_S = 8.0
# 失败重试 1 次（共 2 次尝试）。限速与 `yixiang.rag.ingest.REQUEST_INTERVAL_S` 同值：
# 两个模块对着同一台服务器，改一处别忘了另一处。
MAX_ATTEMPTS = 2
REQUEST_INTERVAL_S = 1.0
# ``-`` 是本项目里"我"的**占位符**，不是接口认的用户名：直接拿它查列表实测 404
# （2026-09-21 真跑 + 官方 OpenAPI 双向确认），所以 fetch 里先经 ``/v0/me`` 换成真用户名
SELF_USER = "-"
# 只看动画、只看"看过"（2 = 在看/看过里的"看过"语义由接口的 type=2 表达）
ANIME_TYPE = 2
COLLECTION_TYPE = 2
# 实测 limit=100 照样返回 100 行（这个接口不压缩分页），offset 真的生效
MAX_LIMIT = 100
MAX_PAGES = 5
# 强偏好的两条线：≥8 是喜欢，1~4 是不喜欢，5~7 与 0 都是没表态
LIKED_RATE = 8
DISLIKED_RATE = 4
TOP_TAGS = 8
# 角色词限长（taste._dedupe 的上限也是 12）：更长的多是制作公司 / 歌名，不是类型词
MAX_TAG_LEN = 12
# Bangumi 高频标签 ∩ 本地 31 部语料 genres 的并集（实测 24 词）。
# 只有这几个词能让 taste.taste_score 真的加/减分；扩词表之前先确认语料里出现过。
TASTE_VOCABULARY = frozenset(
    {
        "冒险", "剧情", "动作", "动画", "喜剧", "太空", "奇幻", "家庭",
        "心理", "悬疑", "惊悚", "推理", "日常", "智斗", "校园", "治愈",
        "热血", "爱情", "犯罪", "科幻", "职场", "赛博朋克", "运动", "音乐",
    }
)
# Bangumi 的常用标签 → 语料词表里的写法（实测高频词里这几个最常见）
TAG_ALIASES = {
    "搞笑": "喜剧",
    "战斗": "动作",
    "青春": "校园",
    "恋爱": "爱情",
    "恐怖": "惊悚",
    "机战": "科幻",
    "萝卜": "科幻",
    "太空歌剧": "太空",
}

# 年份串（``2025'8`` / ``2022``）不是类型词——实测高频标签里这类最多
_YEAR_RE = re.compile(r"^\d{4}")

_404_HINT = (
    "Bangumi 读不到这个用户的收藏（HTTP 404：用户名不存在）。"
    f"读**自己的**收藏需要 Personal Access Token（``user='-'`` 会先问 /v0/me）；申请地址 {TOKEN_PAGE}"
)
_404_WITH_TOKEN_HINT = f"Bangumi 返回 HTTP 404：用户名不存在，或 token 已失效；重新申请 {TOKEN_PAGE}"
_MISSING_TOKEN_HINT = (
    "读自己的 Bangumi 收藏需要 Personal Access Token：登录后在 "
    f"{TOKEN_PAGE} 一键生成，填进 .env 的 YIXIANG_BANGUMI_TOKEN；"
    "只想搜番 / 看评分不需要它（bangumi_search 免 token）"
)


class BangumiError(RuntimeError):
    """收藏链路里"能说清原因"的失败；文案直接给用户看。"""


# ------------------------------------------------------------------ 读收藏
def fetch_collections(
    user: str = SELF_USER,
    *,
    token: str = "",
    subject_type: int = ANIME_TYPE,
    collection_type: int = COLLECTION_TYPE,
    limit: int = MAX_LIMIT,
    pages: int = MAX_PAGES,
    client: Any = None,
    sleep: Callable[[float], None] | None = None,
) -> list[dict]:
    """按 ``offset`` 翻页读某个用户的收藏；``user="-"`` 先经 ``/v0/me`` 换成本人用户名。"""
    pause = sleep or time.sleep
    page_size = _clamp_int(limit, 1, MAX_LIMIT, MAX_LIMIT)
    token_text = str(token or "").strip()
    who = str(user or "").strip() or SELF_USER
    headers = {"Authorization": f"Bearer {token_text}"} if token_text else {}
    owned = client is None
    http = client or _new_client()
    rows: list[dict[str, Any]] = []
    total: int | None = None
    try:
        if who == SELF_USER:
            who = _resolve_self(token_text, client=http, sleep=pause)
        url = COLLECTIONS_ENDPOINT.format(user=who)
        for page in range(_clamp_int(pages, 1, MAX_PAGES, MAX_PAGES)):
            if page:
                pause(REQUEST_INTERVAL_S)  # 翻页之间让一步：1 req/s
            params = {
                "subject_type": _clamp_int(subject_type, 1, 6, ANIME_TYPE),
                "type": _clamp_int(collection_type, 1, 5, COLLECTION_TYPE),
                "limit": page_size,
                "offset": page * page_size,
            }
            payload = _request_json(
                url, params=params, headers=headers, token=token_text, client=http, sleep=pause
            )
            data = payload.get("data") or []
            rows.extend(item for item in data if isinstance(item, dict))
            if isinstance(payload.get("total"), int):
                total = int(payload["total"])
            if len(data) < page_size:
                break  # 短页就是到底了，别再空翻
            if total is not None and len(rows) >= total:
                break
    finally:
        if owned:
            http.close()
    return rows


def _resolve_self(token: str, *, client: Any, sleep: Callable[[float], None]) -> str:
    """把 ``user="-"`` 换成本人的用户名或 uid：``GET /v0/me`` 是唯一的"我是谁"。"""
    if not token:
        # 先查 /v0/me 也没用：没 token 时它一定 401，不如直接给可行动的提示
        raise BangumiError(_MISSING_TOKEN_HINT)
    payload = _request_json(
        ME_ENDPOINT,
        params={},
        headers={"Authorization": f"Bearer {token}"},
        token=token,
        client=client,
        sleep=sleep,
    )
    who = str(payload.get("username") or payload.get("id") or "").strip()
    if not who:
        raise BangumiError("Bangumi 在 /v0/me 里没给出用户名或 uid，读不了自己的收藏；稍后重试")
    return who


def _request_json(
    url: str,
    *,
    params: dict[str, Any],
    headers: dict[str, str],
    token: str,
    client: Any,
    sleep: Callable[[float], None],
    attempts: int = MAX_ATTEMPTS,
) -> dict:
    """最多 ``attempts`` 次的 JSON GET。4xx 立刻抛（重试没用），5xx 与网络错重试。"""
    last = ""
    response: httpx.Response | None = None
    for attempt in range(max(int(attempts), 1)):
        if attempt:
            sleep(REQUEST_INTERVAL_S)
        try:
            response = client.get(url, params=params, headers=headers, timeout=TIMEOUT_S)
        except httpx.HTTPError as exc:
            last = f"网络错误（{_reason(exc)}）"
            continue
        code = int(response.status_code)
        if code == 404:
            raise BangumiError(_404_WITH_TOKEN_HINT if token else _404_HINT)
        if 400 <= code < 500:
            # 401/403 在这里：token 无效或没开通权限。返空会让画像看起来"就是没偏好"
            hint = f"；token 可能已失效，重新申请 {TOKEN_PAGE}" if code == 401 else ""
            raise BangumiError(f"Bangumi 返回 HTTP {code}：{_snippet(response)}{hint}")
        if code >= 500:
            last = f"HTTP {code}：{_snippet(response)}"
            continue
        try:
            payload = response.json()
        except ValueError as exc:  # 不是 JSON：别把坏响应当空收藏
            raise BangumiError(f"Bangumi 返回的内容不是 JSON（{_reason(exc)}）") from exc
        if not isinstance(payload, dict):
            raise BangumiError("Bangumi 返回的收藏格式不认识（顶层不是对象）")
        return payload
    raise BangumiError(f"{url} 读取失败（尝试 {max(int(attempts), 1)} 次）：{last or '未知原因'}")


# ------------------------------------------------------------------ 算口味
def taste_tags(rows: list[dict], *, top: int = TOP_TAGS) -> tuple[list[str], list[str]]:
    """把收藏里的 ``rate`` + 标签折成 ``(喜欢, 不喜欢)`` 两组题材词（按频次降序）。"""
    liked: Counter[str] = Counter()
    disliked: Counter[str] = Counter()
    for row in rows:
        if not isinstance(row, dict) or row.get("private"):
            continue  # 私密条目不进画像：用户没打算让它出现在别处
        rate = _rate_of(row)
        if rate is None:
            continue
        if rate >= LIKED_RATE:
            bucket = liked
        elif 0 < rate <= DISLIKED_RATE:
            bucket = disliked
        else:
            continue  # 0（没打分）与 5~7（中间档）都不表态
        for word in _tags_of(row):
            bucket[word] += 1  # 同一部片只算一次（_tags_of 内部已去重）
    return _top_words(liked, top), _top_words(disliked, top)


def _tags_of(row: dict) -> list[str]:
    """条目级与 ``subject`` 级都带标签：取并集，但同一部片对同一个词只算一次。"""
    subject = row.get("subject")
    sources = [row.get("tags"), subject.get("tags") if isinstance(subject, dict) else None]
    words: list[str] = []
    for source in sources:
        for item in source or []:
            name = item.get("name") if isinstance(item, dict) else None
            word = _normalize(name)
            if word and word not in words:
                words.append(word)
    return words


def _normalize(raw: Any) -> str:
    """别名 → 去年份串 → 长度 1~12 → 必须在 ``TASTE_VOCABULARY`` 里，否则丢。"""
    text = str(raw or "").strip()
    if not text:
        return ""
    text = TAG_ALIASES.get(text, text)
    if _YEAR_RE.match(text):
        return ""
    if not 1 <= len(text) <= MAX_TAG_LEN:
        return ""
    return text if text in TASTE_VOCABULARY else ""


def _rate_of(row: dict) -> int | None:
    """``rate`` 是"我打的分"；``subject.score`` 是平均分（别拿错，也别取 ``subject.rating``）。"""
    value = row.get("rate")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _top_words(counter: Counter[str], top: int) -> list[str]:
    # 频次降序；频次相同时按"第一次出现的次序"（Counter 保序 + sorted 稳定）。
    # 不平手时再按词序排：那会让同一批收藏的画像顺序取决于汉字的码位，而不是收藏本身。
    picked = sorted(counter.items(), key=lambda item: -item[1])
    return [word for word, _ in picked[: _clamp_int(top, 1, TOP_TAGS, TOP_TAGS)]]


# ------------------------------------------------------------------ 同步画像
def sync_taste_profile(
    settings: Any,
    user: str = SELF_USER,
    *,
    write: bool = False,
    top: int = TOP_TAGS,
    client: Any = None,
    sleep: Callable[[float], None] | None = None,
) -> str:
    """读自己的收藏 → 生成口味画像；``write=True`` 才写 ``data/user.md`` 的「偏好」段。"""
    token = str(getattr(settings, "bangumi_token", "") or "").strip()
    if not token:
        # 不发请求、不落盘：没有 token 时"读自己的收藏"是结构性做不到，不是网络问题
        return error_text("missing_token", "bangumi_token", _MISSING_TOKEN_HINT)
    try:
        rows = fetch_collections(user, token=token, client=client, sleep=sleep)
    except BangumiError as exc:
        return error_text("bangumi_unavailable", "bangumi_token", f"读收藏失败：{exc}")
    except Exception as exc:  # 兜底：任何没预料到的异常也要变成可行动的错误
        return error_text(
            "bangumi_unavailable", "bangumi_token", f"读收藏失败（{_reason(exc)}）；稍后重试"
        )

    liked, disliked = taste_tags(rows, top=top)
    lines = [f"Bangumi 收藏口味画像（样本 {len(rows)} 条已收录动画，私密条目已排除）："]
    if liked:
        lines.append("喜欢：" + "、".join(liked))  # 「、」分隔：taste.split_genres 按它切
    if disliked:
        lines.append("不喜欢：" + "、".join(disliked))
    if not rows:
        # 0 条和"都是 5~7 分"是两回事：账号还没标过，说清下一步比说"没有强偏好"有用
        lines.append("（收藏里一条「看过」的动画都没有：先在 bgm.tv 标几个「看过」，再回来生成画像）")
    elif not liked and not disliked:
        lines.append("（评分集中在 5~7 分或没打分，暂时没有能加权的强偏好）")
    if write:
        lines.append(_write_preferences(settings, liked, disliked))
    return wrap_external("\n".join(lines), source=EXTERNAL_SOURCE)


def _write_preferences(settings: Any, liked: list[str], disliked: list[str]) -> str:
    """写 ``data/user.md``：**写前按整行查重**，重复的偏好不追加第二行。"""
    from pathlib import Path

    path = Path(settings.data_dir) / "user.md"
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    written: list[str] = []
    skipped: list[str] = []
    for label, words in (("喜欢", liked), ("不喜欢", disliked)):
        if not words:
            continue
        line = f"{label}：{'、'.join(words)}"
        if line in existing:
            skipped.append(line)
            continue
        result = memory_admin.update_user("偏好", line)
        if str(result).startswith("Error"):
            return f"（写 user.md 失败：{_reason_from(result)}）"
        written.append(line)
    if written:
        return "（已写进 user.md 的「偏好」段：" + "；".join(written) + "）"
    if skipped:
        return "（user.md 里已存在同样的行，未重复追加：" + "；".join(skipped) + "）"
    return "（没有可写的强偏好，user.md 未改动）"


# ------------------------------------------------------------------ 小工具
def _new_client() -> httpx.Client:
    return httpx.Client(timeout=TIMEOUT_S, headers={"User-Agent": USER_AGENT})


def _clamp_int(value: Any, low: int, high: int, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(number, high))


def _reason(exc: Exception) -> str:
    text = str(exc).strip() or type(exc).__name__
    return text[:120]


def _snippet(response: httpx.Response) -> str:
    try:
        return str(response.text or "").strip()[:120] or "（空响应）"
    except Exception:  # pragma: no cover - 读 body 失败不该盖掉真正的原因
        return "（响应体读不出来）"


def _reason_from(result: Any) -> str:
    return str(result).strip()[:200]


__all__ = [
    "ANIME_TYPE",
    "API_ROOT",
    "BangumiError",
    "COLLECTIONS_ENDPOINT",
    "COLLECTION_TYPE",
    "DISLIKED_RATE",
    "EXTERNAL_SOURCE",
    "LIKED_RATE",
    "MAX_ATTEMPTS",
    "MAX_LIMIT",
    "MAX_PAGES",
    "MAX_TAG_LEN",
    "ME_ENDPOINT",
    "REQUEST_INTERVAL_S",
    "SELF_USER",
    "TAG_ALIASES",
    "TASTE_VOCABULARY",
    "TIMEOUT_S",
    "TOKEN_PAGE",
    "TOP_TAGS",
    "USER_AGENT",
    "fetch_collections",
    "sync_taste_profile",
    "taste_tags",
]
