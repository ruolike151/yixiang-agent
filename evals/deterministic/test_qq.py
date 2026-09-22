"""QQ 网关：解析 / 白名单 / 幂等 / 分片（TECH §10.2、PART-4 附录 A-2 / D-13）。

网关的价值全在**入口那一层**：方向（反向 WS）、谁的话能进来、同一条消息只回一次、
发太长怎么切。这些全是同步纯函数，所以离线用例能钉死它们，不用起真的 WebSocket。

纪律：不真连网络、不真 sleep、不真调模型（`FakeProvider` 演剧本）。
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from fake_provider import FakeProvider, text_reply, tool_round

from yixiang.app import App
from yixiang.gateway.qq import (
    QQGateway,
    claim_message,
    parse_allowlist,
    parse_event,
    parse_listen,
    prune_processed,
    split_reply,
    strip_cq,
)


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


def test_cq_codes_are_stripped_and_images_degrade_to_a_placeholder():
    assert strip_cq("[CQ:at,qq=10001] 在吗") == "在吗"

    event = {
        "post_type": "message",
        "message_type": "private",
        "message_id": 1,
        "user_id": 10001,
        "message": [
            {"type": "at", "data": {"qq": "10001"}},
            {"type": "text", "data": {"text": "这张图"}},
            {"type": "image", "data": {"file": "x.jpg"}},
        ],
    }
    incoming = parse_event(event)

    assert incoming is not None
    assert incoming.text == "这张图[图片]"  # 图片不下载：路径不存在就不该假装有
    assert incoming.user_id == "10001"
    assert incoming.message_id == "1"
    assert incoming.group_id == ""


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
    """装配一个能收消息的网关：白名单只有 10001，模型是假 Provider。"""
    settings.qq_enabled = True
    settings.qq_allowed = str(overrides.pop("allowed", "10001"))
    for key, value in overrides.items():
        setattr(settings, key, value)
    app = App.from_settings(settings, provider=provider, conn=conn, clock=clock)
    return app, QQGateway(app, settings=settings, conn=conn, clock=clock)


def _event(message_id: object, user_id: object, text: str = "在吗") -> dict:
    return {
        "post_type": "message",
        "message_type": "private",
        "message_id": message_id,
        "user_id": user_id,
        "raw_message": text,
    }


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
