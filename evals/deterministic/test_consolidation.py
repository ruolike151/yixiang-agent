"""巩固质量（PART-2 §1：「不重复率 ≥95%；不臆造」）。

这一组用例把"巩固"当成一条**有质量门禁的流水线**来断言，而不是只看它写没写：

  · 与存量 fact 撞车的候选不重复落库（重复率必须为 0，门槛是 ≤5%）；
  · 模型没有可写的东西时**一条都不许编**（负样本：空候选 / 坏 JSON）；
  · 模型失败时水印与 ``chat_log.consolidated`` 都不许动（下次重试，§7.7.1）。
"""

from __future__ import annotations

import asyncio
import json

import pytest
from fake_provider import FakeProvider, text_reply

from yixiang import memory
from yixiang.memory import consolidate, semantic

#: PART-2 §1 的验收门槛：落库的 fact 里重复条目占比不得超过 5%
DUPLICATE_RATE_BAR = 0.05


@pytest.fixture
def mem(settings, conn, clock):
    """装配记忆子系统（假嵌入），用例结束清掉全局上下文。"""
    memory.configure(
        conn,
        data_dir=settings.data_dir,
        clock=clock,
        settings=settings,
        embedder=semantic.HashEmbedder(),
    )
    yield memory.current()
    memory.reset()


def _chat(conn, user_text: str, reply_text: str = "好的") -> int:
    with conn:
        cursor = conn.execute(
            "INSERT INTO chat_log(session_id, source, user_text, reply_text, tools_json, created_at) "
            "VALUES('cli:test', 'cli', ?, ?, '[]', '2026-09-19T10:00:00+08:00')",
            (user_text, reply_text),
        )
    return int(cursor.lastrowid)


def _batch(episode: str, items: list[dict[str, object]]) -> str:
    return json.dumps(
        {"episode": {"summary": episode}, "candidates": items}, ensure_ascii=False
    )


def _alive_contents(conn) -> list[str]:
    return [
        str(row["content"]).strip().lower()
        for row in conn.execute(
            "SELECT content FROM facts WHERE deleted = 0 ORDER BY id"
        ).fetchall()
    ]


def _run(conn, batch: str, settings, clock):
    return asyncio.run(
        consolidate.consolidate(
            conn, FakeProvider(text_reply(batch)), settings=settings, clock=clock, force=True
        )
    )


# ------------------------------------------------------------------ 不重复率
def test_no_duplicate_rate_is_within_the_bar(settings, conn, clock, mem):
    existing = [
        "用户喜欢科幻悬疑类影视作品",
        "用户在准备秋招面试",
        "用户习惯晚上十一点后不被打扰",
    ]
    for content in existing:
        semantic.save_fact("偏好", content)
    assert len(_alive_contents(conn)) == 3
    for index in range(1, 21):
        _chat(conn, f"第 {index} 轮闲聊", "嗯嗯")

    # 模型把三条存量原样又提了一遍（一次巩固最多收 3 条候选，§7.7.2）
    items = [
        {"section": "偏好", "content": content, "confidence": 0.95} for content in existing
    ]
    result = _run(conn, _batch("用户聊了很久的偏好", items), settings, clock)

    assert result.duplicates == 3  # 撞车的被识别出来，没重复写
    assert result.auto_written == 0
    contents = _alive_contents(conn)
    assert contents == [item.lower() for item in existing]
    duplicate_rate = 1 - len(set(contents)) / len(contents)
    assert duplicate_rate <= DUPLICATE_RATE_BAR


# ------------------------------------------------------------------ 不臆造（负样本）
def test_nothing_is_invented_when_the_model_lists_no_candidate(settings, conn, clock, mem):
    for index in range(1, 21):
        _chat(conn, f"第 {index} 轮闲聊", "嗯嗯")
    before = _alive_contents(conn)

    result = _run(conn, _batch("用户只是随便聊了几句，没有新事实", []), settings, clock)

    assert result.ran is True
    assert result.episode is True  # 情景记忆照写
    assert result.auto_written == 0
    assert _alive_contents(conn) == before
    assert conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0] == 1


def test_broken_json_is_a_failure_not_a_write(settings, conn, clock, mem):
    _chat(conn, "我最近在做 yixiang 项目")

    result = _run(conn, "模型今天不想输出结构化结果", settings, clock)

    assert result.ran is True
    assert result.auto_written == 0
    assert _alive_contents(conn) == []
    assert consolidate.watermark(conn) == 0
    assert int(conn.execute("SELECT COUNT(*) FROM chat_log WHERE consolidated = 1").fetchone()[0]) == 0


# ------------------------------------------------------------------ 失败不推水印
def test_model_failure_keeps_watermark_and_raw_chat_log(settings, conn, clock, mem):
    chat_id = _chat(conn, "记住我喜欢悬疑", "已记住：你喜欢悬疑")

    result = asyncio.run(
        consolidate.consolidate(conn, FakeProvider(), settings=settings, clock=clock, force=True)
    )

    assert result.ran is True
    assert result.episode is False
    assert "失败" in result.reason
    assert consolidate.watermark(conn) == 0
    assert consolidate.failure_streak(conn) == 1
    row = conn.execute("SELECT * FROM chat_log WHERE id = ?", (chat_id,)).fetchone()
    assert row is not None and int(row["consolidated"]) == 0
