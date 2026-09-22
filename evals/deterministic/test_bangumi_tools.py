"""Bangumi live 工具（``bangumi_search`` / ``bangumi_subject``）：契约全在假 client 上钉。

纪律：**一条网络请求都不发**。``httpx.MockTransport`` 把 URL / query / body / 头
逐项钉死——Bangumi 的坑（分页只看 query、keyword 语义窄、关联接口顶层是 list、
别名要剥两层）全是"看起来对、实际拿错"的类型，只有逐项断言才抓得到。
"""

from __future__ import annotations

import json

import httpx

from yixiang.tools import bangumi, media

BANGUMI_ID = 346873


class SyncSleep:
    """记录睡眠时长但不真睡（与 ``test_ingest_fetch.py`` 同款）。"""

    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(float(seconds))


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _subjects_page(*ids: int) -> dict:
    """``POST /v0/search/subjects`` 的一页响应（字段按 2026-09-20 实测取）。"""
    return {
        "total": len(ids),
        "data": [
            {
                "id": subject_id,
                "name": f"Summer Time Render {subject_id}",
                "name_cn": "夏日重现",
                "date": "2022-04-15",
                "platform": "TV",
                "rating": {"score": 8.4, "rank": 120},
                "tags": [{"name": "悬疑"}, {"name": "科幻"}],
            }
            for subject_id in ids
        ],
    }


def _subject(subject_id: int = BANGUMI_ID) -> dict:
    """``GET /v0/subjects/{id}``：注意 infobox 与别名那两层（实测）。"""
    return {
        "id": subject_id,
        "name": "サマータイムレンダ",
        "name_cn": "夏日重现",
        "date": "2022-04-15",
        "platform": "TV",
        "total_episodes": 25,
        "rating": {"score": 8.4, "rank": 120},
        "tags": [{"name": "悬疑"}, {"name": "科幻"}],
        "summary": "时间循环题材的悬疑动画。",
        "infobox": [
            {"key": "别名", "value": [{"v": "夏日时光"}, {"v": "Summer Time Rendering"}]}
        ],
    }


def _relations() -> list:
    """``GET /v0/subjects/{id}/subjects``：顶层就是 list（实测 55 条）。"""
    return [{"id": 999001, "name_cn": "夏日重现 第二季", "relation": "续集"}]


def _persons() -> list:
    """``GET /v0/subjects/{id}/persons``：顶层是 list（实测 296 条）。"""
    return [
        {"id": 1, "name": "渡边步", "relation": "导演", "career": ["动画"], "eps": "1-25"},
        {"id": 2, "name": "小泉纪介", "relation": "音乐", "career": ["音乐"], "eps": ""},
        {"id": 3, "name": "某歌手", "relation": "主题歌演出", "career": ["声优"], "eps": ""},
    ]


# ------------------------------------------------------------------ 常量
def test_the_module_constants_pin_the_measured_limits():
    """常量里锁着三条实测事实：限速同源、单页上限 20、UA 可识别。"""
    from yixiang.rag import ingest

    assert bangumi.REQUEST_INTERVAL_S == ingest.REQUEST_INTERVAL_S, "两个模块对着同一条接口，限速必须同值"
    assert bangumi.MAX_LIMIT == 20, "实测 limit=50 被接口压回 20"
    assert bangumi.MAX_ATTEMPTS == 2
    assert bangumi.TIMEOUT_S <= 10.0, "注册表的 timeout_s 没人强制，超时必须由工具自己兜"
    assert "yixiang" in bangumi.USER_AGENT and "bgm.tv" in bangumi.USER_AGENT


# ------------------------------------------------------------------ bangumi_search
def test_search_puts_paging_in_query_and_filters_in_body(conn, clock):
    """分页走 query、语义走 body：这一条就是 Task 26 那个坑的翻版。"""
    seen: list[tuple[str, dict, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(
            (
                str(request.url).split("?")[0],
                dict(request.url.params),
                json.loads(request.content.decode("utf-8")),
            )
        )
        return httpx.Response(200, json=_subjects_page(BANGUMI_ID))

    client = _client(handler)
    try:
        bangumi.search_bangumi(
            conn,
            clock.now,
            "夏日",
            tag="悬疑",
            air_date_from="2020-01-01",
            rating_min=8,
            client=client,
            sleep=SyncSleep(),
        )
    finally:
        client.close()

    url, params, body = seen[0]
    assert url == bangumi.SEARCH_ENDPOINT
    assert params == {"limit": "5", "offset": "0"}
    assert body["keyword"] == "夏日"
    assert body["sort"] == "match", "关键词非空时 match 才排得出相关度（rank/score 实测无效）"
    assert body["filter"]["type"] == [2]
    assert body["filter"]["tag"] == ["悬疑"]
    assert body["filter"]["air_date"] == [">=2020-01-01"]
    assert body["filter"]["rating"] == [">=8"]
    assert "limit" not in body and "offset" not in body, "分页参数又被塞回 body 了"


def test_search_without_keyword_or_tag_is_an_actionable_error(conn, clock):
    """两个必填里至少要有一个——JSON Schema 表达不了 anyOf，所以由函数兜。"""

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError(f"空查询不该发请求：{request.url}")

    client = _client(handler)
    try:
        out = bangumi.search_bangumi(conn, clock.now, "   ", client=client, sleep=SyncSleep())
    finally:
        client.close()

    assert out.startswith("Error")
    assert "missing_query" in out


def test_search_accepts_a_tag_without_a_keyword(conn, clock):
    """只给 ``tag`` 也必须能搜——模型就是这么调的（"有关机器人的番剧" → ``{"tag": "机器人"}``）。

    2026-09-22 现场：``keyword`` 是位置必填，这个调用在进函数体之前就
    ``TypeError: missing 1 required positional argument`` 了，函数里那句
    "keyword 与 tag 至少要给一个"的兜底成了永远走不到的死代码。模型白烧一轮重试。
    """
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json=_subjects_page(BANGUMI_ID))

    client = _client(handler)
    try:
        out = bangumi.search_bangumi(conn, clock.now, tag="机器人", client=client, sleep=SyncSleep())
    finally:
        client.close()

    assert not out.startswith("Error"), f"只给 tag 不该报错：{out[:120]}"
    assert seen[0]["keyword"] == "", "空关键词要显式给空串（ingest 的实测口径）"
    assert seen[0]["sort"] == "heat", "没有关键词时 match 排不出相关度，只有 heat 能翻条目"
    assert seen[0]["filter"]["tag"] == ["机器人"]
    assert "机器人" in out
    assert "按热度" in out, "没有关键词就不是按相关度排的，别在标题里说反"


def test_search_wraps_the_rows_as_external_content(conn, clock):
    """出口唯一：``<external_content source="bangumi">``（§14.3-2）。"""
    client = _client(lambda request: httpx.Response(200, json=_subjects_page(BANGUMI_ID)))
    try:
        out = bangumi.search_bangumi(conn, clock.now, "夏日重现", client=client, sleep=SyncSleep())
    finally:
        client.close()

    assert out.startswith('<external_content source="bangumi">')
    assert out.endswith("</external_content>")
    assert "夏日重现" in out
    assert "8.4" in out
    assert f"id={BANGUMI_ID}" in out, "id 必须给出来，bangumi_subject 才有下行入口"


def test_search_with_no_hits_says_why_keyword_is_narrow(conn, clock):
    """空结果不是错误，但必须解释 keyword 只匹配标题/别名（实测「悬疑」→ 0 条）。"""
    client = _client(lambda request: httpx.Response(200, json={"total": 0, "data": []}))
    try:
        out = bangumi.search_bangumi(conn, clock.now, "悬疑", client=client, sleep=SyncSleep())
    finally:
        client.close()

    assert "没有匹配" in out
    assert "tag" in out and "别名" in out
    assert "<external_content" not in out, "没有内容就没有外部内容可包"


def test_search_retries_once_then_degrades_to_the_local_corpus(conn, clock):
    """Bangumi 挂了不能让「找番」整个消失：退一格用本地语料，并说清退到了哪。"""
    attempts: list[str] = []
    sleep = SyncSleep()

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(str(request.url))
        raise httpx.ConnectError("bgm 挂了", request=request)

    client = _client(handler)
    try:
        out = bangumi.search_bangumi(conn, clock.now, "讲时间循环的", client=client, sleep=sleep)
    finally:
        client.close()

    assert len(attempts) == 2, "失败要重试一次再降级"
    assert sleep.calls == [1.0], "限速只发生在两次尝试之间，失败后不再空等"
    # 降级出口就是 search_media：这里没装配 RAG，拿到的是它自己的可行动错误。
    # 本用例要保的不是"拿到什么"，而是"有降级、且没静默返回空串"。
    assert out.endswith(media.search_media(conn, clock.now, "讲时间循环的"))
    assert out.count("Bangumi") == 1


# ------------------------------------------------------------------ bangumi_subject
def test_subject_renders_aliases_relations_and_staff():
    """关联与职员两个接口的顶层是 list——按 ``{"data": ...}`` 解会静默拿到空。"""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path.endswith("/subjects"):
            return httpx.Response(200, json=_relations())
        if request.url.path.endswith("/persons"):
            return httpx.Response(200, json=_persons())
        return httpx.Response(200, json=_subject())

    client = _client(handler)
    try:
        out = bangumi.bangumi_subject(
            BANGUMI_ID, with_relations=True, with_staff=True, client=client, sleep=SyncSleep()
        )
    finally:
        client.close()

    assert seen == [
        f"/v0/subjects/{BANGUMI_ID}",
        f"/v0/subjects/{BANGUMI_ID}/subjects",
        f"/v0/subjects/{BANGUMI_ID}/persons",
    ]
    assert out.startswith('<external_content source="bangumi">')
    assert out.endswith("</external_content>")
    assert "别名：夏日时光、Summer Time Rendering" in out, "别名在 infobox 里还要再剥一层 v"
    assert "续集：夏日重现 第二季" in out
    assert "导演：渡边步" in out
    assert "主题歌演出" not in out, "职员只留关键岗位：296 条全塞进 prompt 会淹掉答案"
    assert f"id={BANGUMI_ID}" in out


def test_subject_keeps_the_answer_when_a_side_call_fails():
    """附属信息取不到就少一段并注明，别让整个条目查不成。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/subjects"):
            raise httpx.ConnectError("关系接口挂了", request=request)
        return httpx.Response(200, json=_subject())

    client = _client(handler)
    try:
        out = bangumi.bangumi_subject(
            BANGUMI_ID, with_relations=True, client=client, sleep=SyncSleep()
        )
    finally:
        client.close()

    assert out.startswith('<external_content source="bangumi">')
    assert "夏日重现" in out
    assert "暂时取不到" in out


def test_subject_failure_names_the_id_and_the_fix():
    """主体接口挂了才报错，且错误里要有可行动的下一步（§9.1）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("bgm 挂了", request=request)

    client = _client(handler)
    try:
        out = bangumi.bangumi_subject(999, client=client, sleep=SyncSleep())
    finally:
        client.close()

    assert out.startswith("Error")
    assert "bangumi_unavailable" in out
    assert "999" in out and "bangumi_search" in out
