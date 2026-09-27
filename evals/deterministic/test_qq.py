"""QQ 网关：解析 / 白名单 / 幂等 / 分片 / 收图（TECH §10.2、PART-4 附录 A-2 / D-13）。

网关的价值全在**入口那一层**：方向（反向 WS）、谁的话能进来、同一条消息只回一次、
发太长怎么切、发来的图怎么变成模型能看的像素。除收图那几步是 IO，其余都是同步
纯函数，所以离线用例能钉死它们，不用起真的 WebSocket。

纪律：不真连网络、不真 sleep、不真调模型（`FakeProvider` 演剧本、`FakeFetch`
演下载）。
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from conftest import PNG_1X1
from fake_provider import FakeProvider, text_reply, tool_round

from yixiang.app import App
from yixiang.gateway.qq import (
    IMAGE_ACK,
    PendingImages,
    QQGateway,
    claim_message,
    cq_images,
    parse_allowlist,
    parse_event,
    parse_listen,
    prune_processed,
    read_local_image,
    split_reply,
    strip_cq,
)

# 用户发来的那张图在 OneBot 事件里的取处（NapCat 给的是腾讯 CDN 的 http 地址）
IMAGE_URL = "http://cdn.qq.com/x.jpg"
# 落进工作区之后的名字：``YYYY-MM-DD-<原名>``，日期来自固定时钟
SAVED_NAME = "2026-09-19-x.jpg"


# ------------------------------------------------------------------ 纯函数层
def test_listen_is_parsed_as_host_and_port():
    assert parse_listen("127.0.0.1:8766") == ("127.0.0.1", 8766)
    with pytest.raises(ValueError):
        parse_listen("8766")  # 缺 host：配错了要当场炸，不能静默回落


def test_allowlist_is_empty_by_default_which_means_refuse_everything():
    assert parse_allowlist("") == frozenset()
    assert parse_allowlist("10001, 10002;10003  10004") == {
        "10001",
        "10002",
        "10003",
        "10004",
    }


def test_cq_codes_are_stripped_and_image_segments_keep_their_source():
    """图片段不再降级成 ``[图片]``：它带走 ``data.url``，文本只剩真正说的话。"""
    assert strip_cq("[CQ:at,qq=10001] 在吗") == "在吗"

    event = {
        "post_type": "message",
        "message_type": "private",
        "message_id": 1,
        "user_id": 10001,
        "message": [
            {"type": "at", "data": {"qq": "10001"}},
            {"type": "text", "data": {"text": "这张图"}},
            {"type": "image", "data": {"file": "x.jpg", "url": "http://cdn.qq.com/x.jpg"}},
            {"type": "face", "data": {"id": "1"}},
        ],
    }
    incoming = parse_event(event)

    assert incoming is not None
    assert incoming.text == "这张图"
    assert incoming.images == ("http://cdn.qq.com/x.jpg",)  # 像素的取处留着，等网关去取
    assert incoming.user_id == "10001"
    assert incoming.message_id == "1"
    assert incoming.group_id == ""


def test_a_string_message_with_a_cq_image_still_yields_its_source():
    """``messagePostFormat: "string"``：图藏在 CQ 码里，剥壳时不能连它一起丢掉。

    本机配的是 ``array``，但那是别人手边随时能改的配置——只认段数组的话，换个格式
    就退回"图静默消失"，正是要修的那个现象。``file`` 兜底也一并钉住（没 ``url`` 时）。
    """
    event = {
        "post_type": "message",
        "message_type": "private",
        "message_id": 5,
        "user_id": 10001,
        "message": "看这个[CQ:image,file=x.jpg,url=http://cdn.qq.com/x.jpg]好看吗",
    }
    incoming = parse_event(event)

    assert incoming is not None
    assert incoming.text == "看这个好看吗"
    assert incoming.images == ("http://cdn.qq.com/x.jpg",)
    assert cq_images("[CQ:image,file=C:/缓存/图.png]") == ("C:/缓存/图.png",)


def test_an_image_with_no_text_around_it_is_still_an_event():
    """先发图、再打字是 QQ 上的常态：只有图的那条也是一条要处理的消息。

    反过来说，一个既没字又没图的段数组仍然是垃圾（撤回、表情包这类），照旧丢弃。
    """
    only_image = {
        "post_type": "message",
        "message_type": "private",
        "message_id": 4,
        "user_id": 10001,
        "message": [{"type": "image", "data": {"url": "http://cdn.qq.com/x.jpg"}}],
    }
    incoming = parse_event(only_image)

    assert incoming is not None
    assert incoming.text == ""
    assert incoming.images == ("http://cdn.qq.com/x.jpg",)
    assert parse_event(only_image | {"message": [{"type": "face", "data": {"id": "1"}}]}) is None
    # 段落里既没 url 也没 file：没有像素可取，等于那条消息不存在
    assert parse_event(only_image | {"message": [{"type": "image", "data": {}}]}) is None


def test_pending_images_keep_the_last_ten_drop_the_stale_and_are_spent_once(clock):
    """先到的图替下一条文字消息留着：10 张封顶、5 分钟作废、拿走即清、按人分账。"""
    now = clock.now()
    stash = PendingImages()
    stash.add("10001", [f"uploads/{index}.jpg" for index in range(11)], now=now)

    assert stash.take("10001", now=now) == [f"uploads/{index}.jpg" for index in range(1, 11)]
    assert stash.take("10001", now=now) == []  # 用过即清：同一张图不该进两轮

    stash.add("10001", ["uploads/旧.jpg"], now=now)
    assert stash.take("10001", now=now + timedelta(minutes=6)) == []

    stash.add("10001", ["uploads/我的.jpg"], now=now)
    assert stash.take("10002", now=now) == []  # 别人的图不会串到这条会话里
    assert stash.take("10001", now=now) == ["uploads/我的.jpg"]


def test_a_local_file_source_is_read_without_touching_the_network(tmp_path):
    """``data.file`` 是本机路径 / ``file://`` 时直接读盘——NapCat 两种配法都见过。"""
    target = tmp_path / "本地截图.png"
    target.write_bytes(PNG_1X1)

    assert read_local_image(target.as_uri()) == PNG_1X1
    assert read_local_image(str(target)) == PNG_1X1
    assert read_local_image("http://cdn.qq.com/x.jpg") is None  # 网址不归它管
    assert read_local_image(str(tmp_path / "没这张.png")) is None


def test_group_and_non_message_events_are_dropped_by_default():
    group = {
        "post_type": "message",
        "message_type": "group",
        "message_id": 2,
        "user_id": 10001,
        "group_id": 5,
        "raw_message": "大家好",
    }
    assert parse_event(group) is None  # 群消息默认忽略（§10.2.5-2）
    assert parse_event(group, group_enabled=True) is not None
    assert parse_event({"post_type": "notice", "user_id": 10001}) is None
    assert (
        parse_event(
            {
                "post_type": "message",
                "message_type": "private",
                "message_id": 3,
                "user_id": 10001,
                "raw_message": "   ",
            }
        )
        is None
    )


def test_long_replies_are_split_without_losing_characters():
    body = "\n".join(f"第 {index} 段：" + "字" * 30 for index in range(1, 6))

    chunks = split_reply(body, limit=80)

    assert len(chunks) > 1
    assert chunks[0].startswith("(1/")
    assert all(len(chunk) <= 120 for chunk in chunks)
    joined = "".join(chunk.split(" ", 1)[1] for chunk in chunks).replace("\n", "")
    assert joined == body.replace("\n", "")  # 分片不丢字、不切在句子中间


# ------------------------------------------------------------------ 幂等表
def test_the_same_message_id_is_claimed_once(conn, clock):
    now = clock.now()

    assert claim_message(conn, "42", now=now) is True
    assert claim_message(conn, "42", now=now) is False
    assert conn.execute("SELECT COUNT(*) AS n FROM processed_messages").fetchone()["n"] == 1


def test_processed_messages_older_than_the_retention_window_are_pruned(conn, clock):
    now = clock.now()
    claim_message(conn, "old", now=now - timedelta(days=8))
    claim_message(conn, "new", now=now)

    assert prune_processed(conn, before=now - timedelta(days=7)) == 1
    rows = conn.execute("SELECT message_id FROM processed_messages").fetchall()
    assert [row["message_id"] for row in rows] == ["new"]


# ------------------------------------------------------------------ 网关编排
def _gateway(settings, conn, clock, provider, **overrides):
    """装配一个能收消息的网关：白名单只有 10001，模型是假 Provider。

    ``fetch`` / ``image_limit`` 是网关自己的参数（不是 settings 的字段），
    单独挑出来——否则会被下面的 ``setattr`` 塞进 Settings 上。
    """
    settings.qq_enabled = True
    settings.qq_allowed = str(overrides.pop("allowed", "10001"))
    gateway_kwargs = {
        key: overrides.pop(key) for key in ("fetch", "image_limit") if key in overrides
    }
    for key, value in overrides.items():
        setattr(settings, key, value)
    app = App.from_settings(settings, provider=provider, conn=conn, clock=clock)
    return app, QQGateway(app, settings=settings, conn=conn, clock=clock, **gateway_kwargs)


def _event(message_id: object, user_id: object, text: str = "在吗") -> dict:
    return {
        "post_type": "message",
        "message_type": "private",
        "message_id": message_id,
        "user_id": user_id,
        "raw_message": text,
    }


def _image_event(message_id: object, user_id: object, text: str = "") -> dict:
    """一条带图的事件（图 + 可选的话，就是 QQ 里一次发出的那条）。"""
    message: list[dict] = []
    if text:
        message.append({"type": "text", "data": {"text": text}})
    message.append({"type": "image", "data": {"file": "x.jpg", "url": IMAGE_URL}})
    return _event(message_id, user_id) | {"message": message}


class FakeFetch:
    """假图片下载器：按剧本回字节 / 抛异常，并把 URL 记下来。一次真网络都不出。"""

    def __init__(self, *script: bytes | Exception) -> None:
        self.script: list[bytes | Exception] = list(script)
        self.urls: list[str] = []

    async def __call__(self, url: str) -> bytes:
        self.urls.append(url)
        if not self.script:
            raise AssertionError(f"假下载器剧本用完了：这是第 {len(self.urls)} 次")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _nothing_in_the_workspace(settings) -> bool:
    """工作区里一张图都没留下——"没写成"是结果侧该断言的事。"""
    uploads = settings.data_dir / "uploads"
    return not uploads.exists() or not list(uploads.iterdir())


def test_a_stranger_never_reaches_the_model(settings, conn, clock):
    app, gateway = _gateway(settings, conn, clock, FakeProvider(text_reply("不该发生")))
    try:
        replies = asyncio.run(gateway.handle_event(_event(1, 99999)))
        wrote = conn.execute("SELECT COUNT(*) AS n FROM chat_log").fetchone()["n"]
    finally:
        app.close()

    assert replies == []
    assert wrote == 0  # 白名单外连模型都不进（§10.2.5-1）


def test_a_group_message_from_the_allowlisted_user_is_ignored(settings, conn, clock):
    app, gateway = _gateway(settings, conn, clock, FakeProvider(text_reply("不该发生")))
    try:
        event = _event(2, 10001) | {"message_type": "group", "group_id": 7}
        assert asyncio.run(gateway.handle_event(event)) == []
    finally:
        app.close()


def test_a_final_answer_survives_a_failed_tool_call(settings, conn, clock):
    """工具失败过一次、但模型最终给出了答案：QQ 必须把**答案**发出去。

    2026-09-22 的现场：``bangumi_search`` 第一次调用报错，模型换参数重试成功，
    答案也写完了（trace 里 ``finish_reason=stop``），网关却因为 ``result.error``
    非空把整条答案丢掉、只回"这一条我没处理成功"。用户看到的现象就是
    "脚本启动了但消息处理不成功"——这是网关把"本轮有失败"错当成"答案不可用"。
    """
    provider = FakeProvider(
        tool_round(("no_such_tool", {"x": 1})),
        text_reply("这是答案"),
    )
    app, gateway = _gateway(settings, conn, clock, provider)
    try:
        replies = asyncio.run(gateway.handle_event(_event(8, 10001)))
    finally:
        app.close()

    assert replies, "工具失败一次不该把整条答案吞掉"
    assert replies[0].startswith("这是答案")


def test_a_picture_sent_with_text_is_downloaded_and_the_model_gets_the_pixels(
    settings, conn, clock
):
    """QQ 里一次发出的"图 + 话"：图落到工作区，路径随这一轮进模型（多模态那条路）。

    断言打在**请求侧**（模型收到了什么）：``messages[-1].images`` 就是 provider
    读成 data URI 的那张清单；``chat_log.user_text`` 里的"（附图：…）"是同一件事
    落在历史里的形态。
    """
    fetch = FakeFetch(PNG_1X1)
    provider = FakeProvider(text_reply("看到了"))
    app, gateway = _gateway(settings, conn, clock, provider, fetch=fetch)
    try:
        replies = asyncio.run(gateway.handle_event(_image_event(1, 10001, "鉴赏这个排版")))
        row = conn.execute("SELECT user_text FROM chat_log").fetchone()
        saved = sorted((settings.data_dir / "uploads").iterdir())
    finally:
        app.close()

    assert replies == ["看到了"]
    assert fetch.urls == [IMAGE_URL]
    assert [item.name for item in saved] == [SAVED_NAME]
    assert saved[0].read_bytes() == PNG_1X1  # 落的是原字节，不是被重编码的半张图
    assert row["user_text"] == f"鉴赏这个排版\n（附图：uploads/{SAVED_NAME}）"
    assert provider.requests[0].messages[-1].images == [f"uploads/{SAVED_NAME}"]


def test_a_picture_sent_on_its_own_gets_a_short_receipt_and_rides_the_next_text(
    settings, conn, clock
):
    """先发图、再打字（2026-09-27 那条真实记录就是这样，隔了 13 秒）。

    只有图的那条：**不花模型的钱**，回一句短回执，图先替下一条文字留着。
    下一条文字到达时图随它一起进模型——用户看到的才是"我发了图它看懂了"。
    """
    fetch = FakeFetch(PNG_1X1)
    provider = FakeProvider(text_reply("看到了"))
    app, gateway = _gateway(settings, conn, clock, provider, fetch=fetch)
    try:
        receipt = asyncio.run(gateway.handle_event(_image_event(1, 10001)))
        assert receipt == [IMAGE_ACK]
        assert provider.requests == []  # 回执不该调模型
        assert conn.execute("SELECT COUNT(*) AS n FROM chat_log").fetchone()["n"] == 0

        replies = asyncio.run(gateway.handle_event(_event(2, 10001, "鉴赏这个排版")))
        rows = conn.execute("SELECT user_text FROM chat_log").fetchall()
    finally:
        app.close()

    assert replies == ["看到了"]
    assert len(rows) == 1  # 回执那一轮不落 chat_log：它不是一轮对话
    assert (
        rows[0]["user_text"] == f"鉴赏这个排版\n（附图：uploads/{SAVED_NAME}）"
    )
    assert provider.requests[0].messages[-1].images == [f"uploads/{SAVED_NAME}"]


def test_a_picture_that_cannot_be_fetched_degrades_instead_of_breaking_the_turn(
    settings, conn, clock
):
    """下载失败（地址过期 / 网络不通）：这一轮照常跑，但要说清"那张图我没取到"。"""
    fetch = FakeFetch(RuntimeError("连不上"))
    provider = FakeProvider(text_reply("这张我没看到"))
    app, gateway = _gateway(settings, conn, clock, provider, fetch=fetch)
    try:
        replies = asyncio.run(gateway.handle_event(_image_event(1, 10001, "鉴赏这个排版")))
        asked = provider.requests[0].messages[-1]
    finally:
        app.close()

    assert replies == ["这张我没看到"]
    assert "没取到" in asked.content  # 模型知道自己漏了一张图，而不是以为用户只说了半句
    assert asked.images is None  # 没有像素可发，退回纯文本形态
    assert _nothing_in_the_workspace(settings) is True


def test_something_that_is_not_a_picture_never_lands_in_the_workspace(settings, conn, clock):
    """对面发来的"图"其实是段文字 / 坏数据：不写盘、不当图片发，降级成占位。"""
    fetch = FakeFetch("这不是图片，只是一段文字".encode(), RuntimeError("连不上"))
    provider = FakeProvider(text_reply("这张我没看到"), text_reply("这张我没看到"))
    app, gateway = _gateway(settings, conn, clock, provider, fetch=fetch)
    try:
        asyncio.run(gateway.handle_event(_image_event(1, 10001, "看看这张")))
        only_image = asyncio.run(gateway.handle_event(_image_event(2, 10001)))
    finally:
        app.close()

    assert _nothing_in_the_workspace(settings) is True
    assert len(only_image) == 1 and "没取到" in only_image[0]  # 光有图、又没取到：如实说


def test_an_oversize_picture_is_refused_before_anything_is_written(settings, conn, clock):
    """超过上限的图（对齐 Web 上传的 30MB 口径）不该落盘，也不该把这一轮撑爆。"""
    fetch = FakeFetch(PNG_1X1)
    provider = FakeProvider(text_reply("这张我没看到"))
    app, gateway = _gateway(settings, conn, clock, provider, fetch=fetch, image_limit=10)
    try:
        replies = asyncio.run(gateway.handle_event(_image_event(1, 10001, "看看这张")))
    finally:
        app.close()

    assert replies == ["这张我没看到"]
    assert fetch.urls == [IMAGE_URL]  # 取了才知道多大；取回来超限就丢
    assert _nothing_in_the_workspace(settings) is True


def test_flood_from_one_user_is_throttled_with_a_notice(settings, conn, clock):
    """单用户 20 条/分钟（§10.2.4）：第 21 条只回一句提示，不再喂给模型。"""
    provider = FakeProvider(*[text_reply("好") for _ in range(20)])
    app, gateway = _gateway(settings, conn, clock, provider)
    try:
        for index in range(20):
            assert asyncio.run(gateway.handle_event(_event(index, 10001))) == ["好"]
        over = asyncio.run(gateway.handle_event(_event(999, 10001)))
    finally:
        app.close()

    assert len(over) == 1 and "太快" in over[0]
    assert len(provider.requests) == 20


def test_handshake_requires_the_token_once_one_is_configured(settings, conn, clock):
    app, gateway = _gateway(settings, conn, clock, FakeProvider(text_reply("好")))

    class FakeConnection:
        def __init__(self, headers=None, path="/onebot/v11/ws"):
            self.request = type("Request", (), {"headers": headers or {}, "path": path})()

    try:
        assert gateway.handshake_ok(FakeConnection()) is True  # 没配 token：放行
        gateway.settings.qq_token = "s3cret"
        assert gateway.handshake_ok(FakeConnection({"Authorization": "Bearer s3cret"})) is True
        assert gateway.handshake_ok(FakeConnection()) is False
        assert gateway.handshake_ok(FakeConnection(path="/onebot/v11/ws?access_token=s3cret"))
    finally:
        app.close()


def test_a_failing_bind_backs_off_and_keeps_trying(settings, conn, clock):
    """bind 失败不许放弃：退避序列 1→2→4（封顶 60），且期间不真睡。"""
    app, gateway = _gateway(settings, conn, clock, FakeProvider(text_reply("好")))
    waits: list[float] = []
    attempts: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        waits.append(seconds)
        if len(waits) >= 3:  # 三次失败之后收工，别真转圈
            raise asyncio.CancelledError

    def boom(*args, **kwargs):
        attempts.append(1)
        raise OSError("端口被占用")

    gateway.sleep = fake_sleep
    try:
        with (
            pytest.raises(asyncio.CancelledError),
            pytest.MonkeyPatch.context() as patch,
        ):
            patch.setattr("yixiang.gateway.qq.ws_serve", boom)
            asyncio.run(gateway.serve_forever())
    finally:
        app.close()

    assert len(attempts) == 3
    assert waits == [1.0, 2.0, 4.0]
