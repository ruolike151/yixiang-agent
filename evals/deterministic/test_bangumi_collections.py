"""Bangumi 收藏 → 口味画像（``bangumi_my_collections``）：契约全在假 client 上钉。

纪律：**一条网络请求都不发**、**一次 sleep 都不真睡**。收藏接口的坑（评分在
``subject.score`` 而不是 ``subject.rating``、高频标签大多是类型词以外的噪声、
``rate`` 实测集中在 5~7、**压根没有 ``/v0/users/-/collections`` 这个路由**）
都属于"看起来有数据、实际一行排序都不会变"的类型，只有逐项断言才抓得到。
"""

from __future__ import annotations

import httpx
import pytest

from yixiang.memory import semantic
from yixiang.tools import bangumi_collections as bc

# ``-`` 不是接口认的用户名，而是本项目的"我"：fetch 会先问 ``/v0/me`` 换成它
ME_USERNAME = "yixiang-probe"
COLLECTIONS_URL = bc.COLLECTIONS_ENDPOINT.format(user=ME_USERNAME)


def _me_payload() -> dict:
    """``GET /v0/me`` 的最小形状（字段名按 2026-09-21 真跑实测）。"""
    return {"id": 123456, "username": ME_USERNAME, "nickname": "以湘"}


class SyncSleep:
    """记录睡眠时长但不真睡（与 ``test_ingest_fetch.py`` 同款）。"""

    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(float(seconds))


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _router(handler, *, me: dict | None = None):
    """把 ``/v0/me``（"我是谁"）与收藏页的应答分开：fetch 第一步先问 ``/v0/me``。"""

    def dispatch(request: httpx.Request) -> httpx.Response:
        if str(request.url).split("?")[0] == bc.ME_ENDPOINT:
            return httpx.Response(200, json=_me_payload() if me is None else me)
        return handler(request)

    return dispatch


def _collection(
    subject_id: int,
    rate: int,
    *,
    tags: tuple[str, ...] = (),
    subject_tags: tuple[str, ...] = ("科幻", "悬疑"),
    private: bool = False,
) -> dict:
    """``GET /v0/users/{user}/collections`` 的一个条目（字段按 2026-09-20 实测取）。

    注意评分的两个位置：条目上是 ``rate``（我打的分），条目里的 ``subject.score``
    才是平均分——``subject["rating"]`` 实测恒为 ``None``，别按它写。
    """
    return {
        "private": private,
        "rate": rate,
        "subject_id": subject_id,
        "subject": {
            "id": subject_id,
            "name": f"Subject {subject_id}",
            "name_cn": "夏日重现",
            "score": 8.4,
            "rank": 120,
            "tags": [{"name": name} for name in subject_tags],
        },
        "tags": [{"name": name} for name in tags],
    }


def _page(rows: list[dict], *, total: int, limit: int = bc.MAX_LIMIT, offset: int = 0) -> dict:
    """一页响应：顶层就是 ``["data", "limit", "offset", "total"]``（实测）。"""
    return {"data": rows, "limit": limit, "offset": offset, "total": total}


@pytest.fixture
def mem(settings, conn, clock):
    """装配记忆子系统（假嵌入）：``update_user`` 需要一个可写的 ``data/``。"""
    from yixiang import memory

    memory.configure(
        conn,
        data_dir=settings.data_dir,
        clock=clock,
        settings=settings,
        embedder=semantic.HashEmbedder(),
    )
    yield memory.current()
    memory.reset()


@pytest.fixture
def token_settings(settings):
    """带 token 的配置：``Settings`` 是可变 dataclass，改一个字段不用重建。"""
    settings.bangumi_token = "tok-123"
    return settings


# ------------------------------------------------------------------ 常量与分工
def test_the_module_constants_pin_the_measured_limits(registry):
    """常量里锁着四条实测事实：上限 100、榜单口径、词表可用、注册位置在末位之前。"""
    assert bc.MAX_LIMIT == 100, "收藏接口 limit=100 实测照样返回 100 行（不像搜索接口被压回 20）"
    assert bc.MAX_PAGES >= 2, "单页上限 100：公开账号实测 total 上千（1592），一页拿不完"
    assert bc.LIKED_RATE == 8 and bc.DISLIKED_RATE == 4, "强偏好的两条线：≥8 喜欢、1~4 不喜欢"
    assert bc.SELF_USER == "-" and bc.ANIME_TYPE == 2 and bc.COLLECTION_TYPE == 2
    assert bc.ME_ENDPOINT.endswith("/v0/me"), "`-` 不是接口认的用户名，要靠 /v0/me 换"
    assert bc.TIMEOUT_S <= 10.0, "注册表的 timeout_s 没人强制，超时必须由工具自己兜"
    assert "yixiang" in bc.USER_AGENT and "bgm.tv" in bc.USER_AGENT
    for alias, target in bc.TAG_ALIASES.items():
        assert target in bc.TASTE_VOCABULARY, f"{alias} 映射到 {target}，但 {target} 不在词表里"
    assert all(1 <= len(word) <= bc.MAX_TAG_LEN for word in bc.TASTE_VOCABULARY)

    names = registry.names()
    assert "bangumi_my_collections" in names
    assert names.index("daily_brief") < names.index("bangumi_my_collections") < names.index("read_file")


# ------------------------------------------------------------------ fetch_collections
def test_fetch_asks_me_first_then_puts_paging_in_query_and_token_in_the_header():
    """先 ``/v0/me`` 换用户名，再分页：分页走 query、token 走头（query 会进日志与 trace）。"""
    seen: list[tuple[str, dict, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(
            (
                str(request.url).split("?")[0],
                dict(request.url.params),
                request.headers.get("Authorization", ""),
            )
        )
        if str(request.url).split("?")[0] == bc.ME_ENDPOINT:
            return httpx.Response(200, json=_me_payload())
        return httpx.Response(200, json=_page([_collection(1, 9)], total=1))

    client = _client(handler)
    try:
        rows = bc.fetch_collections(
            token="tok-123", limit=50, pages=1, client=client, sleep=SyncSleep()
        )
    finally:
        client.close()

    assert seen[0] == (bc.ME_ENDPOINT, {}, "Bearer tok-123"), "`-` 要先换成 /v0/me 里的用户名"
    url, params, auth = seen[1]
    assert url == COLLECTIONS_URL
    assert params == {"subject_type": "2", "type": "2", "limit": "50", "offset": "0"}
    assert auth == "Bearer tok-123"
    assert all("tok-123" not in str(entry[1]) for entry in seen), "token 绝不能进 query"
    assert len(rows) == 1


def test_explicit_user_skips_the_me_hop():
    """给了用户名就直接查：公开收藏免 token，没理由多打一次 ``/v0/me``。"""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url).split("?")[0])
        return httpx.Response(200, json=_page([_collection(3, 9)], total=1))

    client = _client(handler)
    try:
        rows = bc.fetch_collections("sai", pages=1, client=client, sleep=SyncSleep())
    finally:
        client.close()

    assert seen == [bc.COLLECTIONS_ENDPOINT.format(user="sai")]
    assert len(rows) == 1


def test_fetch_falls_back_to_the_uid_when_me_has_no_username():
    """``/v0/me`` 只给 id 时照样能读：路径参数接受 uid（2026-09-21 实测两种都通）。"""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url).split("?")[0])
        return httpx.Response(200, json=_page([_collection(5, 9)], total=1))

    client = _client(_router(handler, me={"id": 123456}))
    try:
        rows = bc.fetch_collections(token="tok-123", pages=1, client=client, sleep=SyncSleep())
    finally:
        client.close()

    assert seen == [bc.COLLECTIONS_ENDPOINT.format(user="123456")]
    assert len(rows) == 1


def test_fetch_walks_offsets_and_stops_on_a_short_page():
    """``offset`` 实测生效：短页就是"到底了"，别再空翻一页。"""
    offsets: list[str] = []
    sleep = SyncSleep()

    def handler(request: httpx.Request) -> httpx.Response:
        offset = int(dict(request.url.params)["offset"])
        offsets.append(str(offset))
        count = 3 if offset == 0 else 2  # 第二页只有 2 条（< limit=3）→ 结束
        rows = [_collection(100 + offset + index, 8) for index in range(count)]
        return httpx.Response(200, json=_page(rows, total=5, limit=3, offset=offset))

    client = _client(_router(handler))
    try:
        rows = bc.fetch_collections(
            token="tok-123", limit=3, pages=5, client=client, sleep=sleep
        )
    finally:
        client.close()

    assert offsets == ["0", "3"]
    assert len(rows) == 5
    assert sleep.calls == [bc.REQUEST_INTERVAL_S], "翻页之间让一步，别把接口打疼"


def test_fetch_retries_once_on_5xx():
    """5xx 是接口的锅：重试一次；再失败才抛（抛的是 ``BangumiError``）。"""
    attempts: list[str] = []
    sleep = SyncSleep()

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(str(request.url))
        if len(attempts) == 1:
            return httpx.Response(503, text="busy")
        return httpx.Response(200, json=_page([_collection(7, 9)], total=1))

    client = _client(_router(handler))
    try:
        rows = bc.fetch_collections(token="tok-123", pages=1, client=client, sleep=sleep)
    finally:
        client.close()

    assert len(attempts) == 2
    assert sleep.calls == [bc.REQUEST_INTERVAL_S]
    assert len(rows) == 1


def test_forbidden_raises_with_the_status_code():
    """403 是 token 的锅，不能悄悄返空——那会让画像看起来"就是没有偏好"。"""
    client = _client(lambda request: httpx.Response(403, text="Forbidden"))
    with pytest.raises(bc.BangumiError) as info:
        bc.fetch_collections(token="bad-token", pages=1, client=client, sleep=SyncSleep())
    client.close()

    assert "403" in str(info.value)


def test_self_without_token_sends_nothing_and_points_at_the_token_page():
    """没 token 时 ``user="-"`` 连 ``/v0/me`` 都不问：直接给申请地址（§9.1 可行动）。"""

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError(f"没有 token 不该发请求：{request.url}")

    client = _client(handler)
    with pytest.raises(bc.BangumiError) as info:
        bc.fetch_collections(pages=1, client=client, sleep=SyncSleep())
    client.close()

    assert "next.bgm.tv/demo/access-token" in str(info.value)


def test_404_on_the_me_hop_points_at_the_token_page():
    """``/v0/me`` 404 只有一种解释：token 不行了——错误里必须给出重新申请的地址。"""
    client = _client(lambda request: httpx.Response(404, text="Not Found"))
    with pytest.raises(bc.BangumiError) as info:
        bc.fetch_collections(token="stale", pages=1, client=client, sleep=SyncSleep())
    client.close()

    assert "next.bgm.tv/demo/access-token" in str(info.value)


# ------------------------------------------------------------------ taste_tags
def test_taste_tags_keep_only_vocabulary_words_and_drop_noise():
    """噪声标签（剧场版 / 制作公司 / 2025'8 / 超长词）一个都不许进画像。"""
    rows = [
        _collection(1, 9, subject_tags=("科幻", "剧场版", "ProductionI.G")),
        _collection(2, 8, subject_tags=("搞笑", "2025'8")),  # 别名 → 喜剧；年份串丢弃
        _collection(3, 8, subject_tags=("战斗", "ロックンロール・マウンテン")),
        _collection(4, 8, subject_tags=("悬疑",), private=True),  # 私密条目不算
        _collection(5, 6, subject_tags=("治愈",)),  # 5~7 分是没表态
    ]

    liked, disliked = bc.taste_tags(rows)

    assert set(liked) == {"动作", "科幻", "喜剧"}, "只留词表里的词，别名映射后再判"
    assert "剧场版" not in liked and "ProductionI.G" not in liked
    assert "2025'8" not in liked and "ロックンロール・マウンテン" not in liked
    assert "悬疑" not in liked, "私密收藏不该进画像"
    assert "治愈" not in liked, "5~7 分既不算喜欢也不算不喜欢"
    assert disliked == []


def test_taste_tags_count_each_subject_once_and_sort_by_frequency():
    """同一部片的 item 级与 subject 级标签是同一批词，取并集但不能重复计数。"""
    rows = [
        _collection(1, 9, tags=("科幻",), subject_tags=("科幻", "悬疑")),
        _collection(2, 9, subject_tags=("科幻",)),
        _collection(3, 10, subject_tags=("悬疑",)),
        _collection(4, 8, subject_tags=("科幻",)),
    ]

    liked, disliked = bc.taste_tags(rows)

    assert liked == ["科幻", "悬疑"], "按频次降序；科幻 3 次、悬疑 2 次"
    assert disliked == []


def test_taste_tags_treat_only_low_scores_as_disliked():
    """``0<rate<=4`` 才是不喜欢；0 分是"没打分"，别把没表态算成讨厌。"""
    rows = [
        _collection(1, 0, subject_tags=("恐怖",)),
        _collection(2, 4, subject_tags=("恐怖",)),
        _collection(3, 5, subject_tags=("恐怖",)),
    ]

    liked, disliked = bc.taste_tags(rows)

    assert liked == []
    assert disliked == ["惊悚"], "别名 恐怖→惊悚；同一部片只算一次"


# ------------------------------------------------------------------ sync_taste_profile
def test_sync_without_token_is_an_actionable_error_and_sends_nothing(settings, mem):
    """没有 token 就不发请求、不落盘：报错要指向申请地址（§9.1 可行动）。"""

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError(f"没有 token 不该发请求：{request.url}")

    client = _client(handler)
    try:
        out = bc.sync_taste_profile(settings, client=client, sleep=SyncSleep())
    finally:
        client.close()

    assert out.startswith("Error")
    assert "missing_token" in out and "bangumi_token" in out
    assert "next.bgm.tv/demo/access-token" in out
    user = settings.data_dir / "user.md"
    assert "喜欢：" not in (user.read_text(encoding="utf-8") if user.is_file() else "")


def test_sync_defaults_to_dry_run_and_does_not_touch_user_md(token_settings, mem):
    """默认 ``write=False``：画像照样给出来，但 ``user.md`` 一个字节都不动。"""
    client = _client(
        _router(
            lambda request: httpx.Response(
                200, json=_page([_collection(1, 9, subject_tags=("科幻", "悬疑"))], total=1)
            )
        )
    )
    try:
        out = bc.sync_taste_profile(token_settings, client=client, sleep=SyncSleep())
    finally:
        client.close()

    assert out.startswith('<external_content source="bangumi">')
    assert out.endswith("</external_content>")
    assert "喜欢：科幻、悬疑" in out, "类型词用「、」分隔：taste.split_genres 按它切"
    user = token_settings.data_dir / "user.md"
    assert not user.is_file() or "喜欢：科幻、悬疑" not in user.read_text(encoding="utf-8")


def test_sync_write_appends_one_line_and_dedupes_the_second_time(token_settings, mem):
    """``write=True`` 落进 ``data/user.md`` 的「偏好」段；跑第二遍不许再追加一行。"""
    client = _client(
        _router(
            lambda request: httpx.Response(
                200, json=_page([_collection(1, 9, subject_tags=("科幻", "悬疑"))], total=1)
            )
        )
    )
    try:
        out = bc.sync_taste_profile(token_settings, write=True, client=client, sleep=SyncSleep())
    finally:
        client.close()

    user = token_settings.data_dir / "user.md"
    text = user.read_text(encoding="utf-8")
    assert "## 偏好" in text
    assert text.count("喜欢：科幻、悬疑") == 1
    assert "- 喜欢：科幻、悬疑" in text, "落盘走 update_user，行首自动补 '- '"
    assert "喜欢：科幻、悬疑" in out

    client = _client(
        _router(
            lambda request: httpx.Response(
                200, json=_page([_collection(1, 9, subject_tags=("科幻", "悬疑"))], total=1)
            )
        )
    )
    try:
        again = bc.sync_taste_profile(token_settings, write=True, client=client, sleep=SyncSleep())
    finally:
        client.close()

    assert user.read_text(encoding="utf-8").count("喜欢：科幻、悬疑") == 1, "第二遍重复追加了"
    assert "已存在" in again


def test_sync_says_the_collection_is_empty_instead_of_blaming_the_scores(token_settings):
    """0 条和"都是 5~7 分"是两回事：账号还没标过，要说清下一步（2026-09-21 真跑实测 0 条）。"""
    client = _client(_router(lambda request: httpx.Response(200, json=_page([], total=0))))
    try:
        out = bc.sync_taste_profile(token_settings, client=client, sleep=SyncSleep())
    finally:
        client.close()

    assert "样本 0 条" in out
    assert "一条「看过」的动画都没有" in out
    assert "强偏好" not in out, "0 条时别说成「只是评分不高」"
    assert "喜欢：" not in out
