"""记忆写入链路（D-03、D-04、D-05、D-14、D-15、D-17、D-18）。

用 ``HashEmbedder`` + 内存库：**一次真嵌入都不发生**，全部离线可重复。
"""

from __future__ import annotations

import asyncio
import json

import pytest
from fake_provider import FakeProvider, text_reply, tool_round

from yixiang import memory
from yixiang.app import App
from yixiang.memory import consolidate, core_files, memory_admin, semantic
from yixiang.runtime.session import detect_intent


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
    """往 chat_log 塞一轮未巩固对话，返回它的 id。"""
    with conn:
        cursor = conn.execute(
            "INSERT INTO chat_log(session_id, source, user_text, reply_text, tools_json, created_at) "
            "VALUES('cli:test', 'cli', ?, ?, '[]', '2026-09-19T10:00:00+08:00')",
            (user_text, reply_text),
        )
    return int(cursor.lastrowid)


def _facts(conn) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM facts WHERE deleted = 0").fetchone()[0])


def _batch(episode: str, items: list[dict[str, object]]) -> str:
    return json.dumps(
        {"episode": {"summary": episode}, "candidates": items}, ensure_ascii=False
    )


# ------------------------------------------------------------------ D-04
def test_d04_same_fact_second_write_updates_instead_of_inserting(registry, conn, settings, mem):
    first = json.loads(registry.execute("save_memory", {"subject": "偏好", "content": "用户喜欢看 NBA 篮球比赛"}))
    second = json.loads(registry.execute("save_memory", {"subject": "偏好", "content": "用户喜欢看 NBA 篮球比赛"}))

    assert first["action"] == "insert"
    assert second == {"id": first["id"], "action": "update"}
    assert _facts(conn) == 1  # 行数不变：同一件事没有存成两条
    text = (settings.data_dir / "memory.md").read_text(encoding="utf-8")
    assert "用户喜欢看 NBA 篮球比赛" in text


def test_d04_remember_intent_forces_write_then_reports_it(settings, clock):
    provider = FakeProvider(
        text_reply("好的，我知道了"),  # 第一次：没调工具 → 后验校验不过
        tool_round(("save_memory", {"subject": "偏好", "content": "用户喜欢悬疑小说"})),
        text_reply("已记住：用户喜欢悬疑小说"),
    )
    app = App.from_settings(
        settings, provider=provider, clock=clock, embedder=semantic.HashEmbedder()
    )
    try:
        result = app.ask("记住我喜欢悬疑小说", stream=False)
        assert result.intent == {"remember": True, "intent": "REMEMBER"}
        assert result.memory_write_failed is False
        assert result.reply == "已记住：用户喜欢悬疑小说"
        assert len(provider.requests) == 3  # 1 次失败 + 1 次工具 + 1 次收尾
        assert _facts(app.conn) == 1
    finally:
        app.close()
        memory.reset()


# ------------------------------------------------------------------ D-05
def test_d05_memo_and_memory_are_split_by_rules(registry, conn, mem):
    assert detect_intent("记住我喜欢悬疑小说") == "REMEMBER"
    assert detect_intent("帮我记住我下个月要搬家") == "REMEMBER"
    assert detect_intent("记一下我周五中午前交材料") == ""  # 时间意图 → memo
    assert detect_intent("我喜欢悬疑小说") == ""  # 弱陈述不打标

    memo = json.loads(
        registry.execute("add_memo", {"content": "周五中午前交材料", "due_at": "周五中午"})
    )
    assert memo["due_at"].startswith("2026-09-25")
    assert _facts(conn) == 0  # 待办不落 facts
    assert "save_memory" in registry.names()
    assert "add_memo" in registry.names()


# ------------------------------------------------------------------ D-03 / D-17 / D-18
def test_d03_consolidation_extracts_episode_and_fact(settings, conn, clock, mem):
    chat_id = _chat(conn, "我最近在做 yixiang 这个个人助手项目，主要想补记忆系统")
    provider = FakeProvider(
        text_reply(
            _batch(
                "用户介绍了 yixiang 个人助手项目的记忆系统目标",
                [{"section": "项目", "content": "用户在做 yixiang 个人助手项目", "confidence": 0.95}],
            )
        )
    )
    result = asyncio.run(consolidate.consolidate(conn, provider, settings=settings, clock=clock, force=True))

    assert result.ran is True
    assert result.episode is True
    assert result.auto_written == 1
    assert _facts(conn) == 1
    assert conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0] == 1
    assert consolidate.watermark(conn) == chat_id
    assert int(conn.execute("SELECT consolidated FROM chat_log WHERE id = ?", (chat_id,)).fetchone()[0]) == 1
    assert "yixiang 个人助手项目" in (settings.data_dir / "memory.md").read_text(encoding="utf-8")


def test_d17_second_run_does_not_write_again(settings, conn, clock, mem):
    _chat(conn, "我最近在做 yixiang 这个项目")
    batch = _batch(
        "用户在做 yixiang 项目",
        [{"section": "项目", "content": "用户在做 yixiang 项目", "confidence": 0.95}],
    )
    asyncio.run(consolidate.consolidate(conn, FakeProvider(text_reply(batch)), settings=settings, clock=clock, force=True))
    facts_after_first = _facts(conn)

    # 第二次：没有未巩固对话 → 不该再调模型（空剧本一调就炸）
    result = asyncio.run(
        consolidate.consolidate(conn, FakeProvider(), settings=settings, clock=clock, force=True)
    )
    assert result.ran is False
    assert _facts(conn) == facts_after_first
    assert conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0] == 1


def test_d18_consolidation_drops_low_confidence_and_asks_before_writing(settings, conn, clock, mem):
    _chat(conn, "今天好累啊，学了三个小时")
    batch = _batch(
        "用户今天学习了三个小时，感觉比较累",
        [
            {"section": "偏好", "content": "用户今天很累", "confidence": 0.5},  # 情绪化 → 丢
            {"section": "偏好", "content": "用户喜欢在晚上学习", "confidence": 0.7},  # 中间档 → 待确认
        ],
    )
    provider = FakeProvider(text_reply(batch))
    result = asyncio.run(consolidate.consolidate(conn, provider, settings=settings, clock=clock, force=True))

    assert result.dropped == 1
    assert result.confirmed == 1
    assert result.auto_written == 0
    assert _facts(conn) == 1
    text = (settings.data_dir / "memory.md").read_text(encoding="utf-8")
    assert "## 待确认" in text and "用户喜欢在晚上学习" in text
    prompt = provider.requests[0].messages[0].content
    assert "不猜测" in prompt and "情绪" in prompt  # 负样本约束写进了 prompt


# ------------------------------------------------------------------ D-14 / D-15
def test_d14_update_soul_only_appends(settings, conn, clock, mem):
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    soul = settings.data_dir / "soul.md"
    soul.write_text("# Soul\n\n## 行为守则\n- 不要用 emoji 收尾\n", encoding="utf-8")

    ok = json.loads(memory_admin.update_soul("回答先给结论，再给理由"))
    after = soul.read_text(encoding="utf-8")

    assert ok["file"] == "soul.md"
    assert after.startswith("# Soul\n\n## 行为守则\n- 不要用 emoji 收尾\n")
    assert "## Learned rules" in after and "- 回答先给结论，再给理由" in after

    # 想删既有条款：接口上做不到，空 rule 被拒且文件一字未动
    assert memory_admin.update_soul("   ").startswith("Error")
    assert soul.read_text(encoding="utf-8") == after


def test_d15_update_user_rejects_when_over_limit(settings, conn, clock, mem):
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    user = settings.data_dir / "user.md"
    user.write_text("# 用户\n\n## 身份\n" + "- 填充行\n" * 900, encoding="utf-8")
    before = user.read_text(encoding="utf-8")

    out = memory_admin.update_user("身份", "新增一条")

    assert out.startswith("Error") and "4000" in out
    assert user.read_text(encoding="utf-8") == before


def test_n2_soul_limit_is_narrowed_to_3000(settings, conn, clock, mem):
    """N-2 已决策：soul.md 上限 3000 字符，写不下时整条拒绝且文件不动。"""
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    soul = settings.data_dir / "soul.md"
    soul.write_text("# Soul\n\n## Learned rules\n" + "- 填充行\n" * 900, encoding="utf-8")
    before = soul.read_text(encoding="utf-8")

    out = memory_admin.update_soul("再加一条")

    assert core_files.SOUL_MAX == 3000
    assert out.startswith("Error") and "3000" in out
    assert soul.read_text(encoding="utf-8") == before
