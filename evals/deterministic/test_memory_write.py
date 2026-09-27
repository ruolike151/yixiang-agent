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
from yixiang.memory import consolidate, core_files, memory_admin, semantic, sync
from yixiang.runtime.session import SessionManager, detect_intent


@pytest.fixture
def mem(settings, conn, clock):
    """装配记忆子系统（假嵌入）+ 落一份 ``memory.md``；用例结束清全局上下文。"""
    memory.configure(
        conn,
        data_dir=settings.data_dir,
        clock=clock,
        settings=settings,
        embedder=semantic.HashEmbedder(),
    )
    # App 启动时会做这一步（``_configure_memory``）：data/memory.md 一定存在
    core_files.ensure_memory_file(settings.data_dir, template_dir=settings.templates_dir)
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


# ------------------------------------------- 由用户触发的 memory.md 正文编辑（§7.4 人机共治）
def _edit(registry, **args) -> str:
    return registry.execute("manage_memory", {"action": "edit", **args})


def _memory_md(settings) -> str:
    return (settings.data_dir / "memory.md").read_text(encoding="utf-8")


def _with_manual(text: str, *lines: str) -> str:
    """往 ``## 手写笔记`` 段里塞几行——这一段没有 id、不入库，是纯人肉区。"""
    return text.replace("## 手写笔记", "\n".join(["## 手写笔记", *lines]))


def test_edit_add_appends_line_to_manual_section(registry, settings, conn, mem):
    """手写笔记段没有 id、不落 facts：除了改文件，没有别的路能往里写。"""
    out = json.loads(
        _edit(registry, op="add", section="手写笔记", content="体检报告放在抽屉第二层")
    )

    assert out["ok"] is True and out["op"] == "add"
    assert "- 体检报告放在抽屉第二层" in _memory_md(settings)
    assert _facts(conn) == 0


def test_edit_add_into_fact_section_imports_new_fact(registry, settings, conn, mem):
    """往事实段落加行 = 新增一条事实：同步导入后把新 id 回写进文件。"""
    out = json.loads(_edit(registry, op="add", section="偏好", content="用户喜欢在雨天听爵士"))

    assert out["ok"] is True and out["id"] == 1
    assert _facts(conn) == 1
    assert "- [1] 用户喜欢在雨天听爵士" in _memory_md(settings)


def test_edit_replace_keeps_id_and_updates_fact(registry, settings, conn, mem):
    """按原文片段替换：条目换正文但 id 不变，库里那条跟着变（不是插一条新的）。"""
    created = json.loads(
        registry.execute("save_memory", {"subject": "偏好", "content": "用户喜欢看 NBA"})
    )

    out = json.loads(
        _edit(registry, op="replace", match="用户喜欢看 NBA", content="用户喜欢看 NBA 和 CBA")
    )

    assert out["ok"] is True and out["id"] == created["id"]
    assert f"- [{created['id']}] 用户喜欢看 NBA 和 CBA" in _memory_md(settings)
    row = conn.execute(
        "SELECT content, deleted FROM facts WHERE id = ?", (created["id"],)
    ).fetchone()
    assert row["content"] == "用户喜欢看 NBA 和 CBA"
    assert row["deleted"] == 0 and _facts(conn) == 1


def test_edit_remove_soft_deletes_fact_and_line(registry, settings, conn, mem):
    """按 id 删行：文件里那行消失，库里那条约等于软删（还能 restore 捞回来）。"""
    created = json.loads(
        registry.execute("save_memory", {"subject": "偏好", "content": "用户喜欢看 NBA"})
    )

    out = json.loads(_edit(registry, op="remove", match=f"[{created['id']}]"))

    assert out["ok"] is True and out["id"] == created["id"]
    assert "用户喜欢看 NBA" not in _memory_md(settings)
    deleted = conn.execute(
        "SELECT deleted FROM facts WHERE id = ?", (created["id"],)
    ).fetchone()[0]
    assert deleted == 1


def test_edit_remove_deletes_manual_line_without_fact(registry, settings, conn, mem):
    """用户截图里的场景：手写笔记那几行没有 id，只有文件级编辑删得掉。"""
    path = settings.data_dir / "memory.md"
    path.write_text(_with_manual(_memory_md(settings), "- 临时想法"), encoding="utf-8")

    out = json.loads(_edit(registry, op="remove", match="临时想法"))

    assert out["ok"] is True and out["id"] is None
    assert "临时想法" not in _memory_md(settings)
    assert _facts(conn) == 0


def test_edit_rejects_ambiguous_match(registry, settings, mem):
    """一句话命中两行：拒绝并把候选报出来，绝不猜一行改掉。"""
    path = settings.data_dir / "memory.md"
    path.write_text(
        _with_manual(_memory_md(settings), "- 悬疑小说看完了", "- 悬疑剧也看完了"),
        encoding="utf-8",
    )
    before = _memory_md(settings)

    out = _edit(registry, op="remove", match="悬疑")

    assert out.startswith("Error")
    assert "悬疑小说看完了" in out and "悬疑剧也看完了" in out
    assert _memory_md(settings) == before


def test_edit_rejects_missing_match(registry, settings, mem):
    before = _memory_md(settings)

    out = _edit(registry, op="remove", match="根本不存在的句子")

    assert out.startswith("Error")
    assert _memory_md(settings) == before


def test_edit_rejects_unknown_op(registry, settings, mem):
    """整份重写（rewrite）这一版刻意不做：op 只开放三个精确动作。"""
    before = _memory_md(settings)

    out = _edit(registry, op="rewrite", content="整份换掉")

    assert out.startswith("Error") and "rewrite" in out
    assert _memory_md(settings) == before


def test_edit_add_requires_content(registry, settings, mem):
    before = _memory_md(settings)

    out = _edit(registry, op="add", section="手写笔记")

    assert out.startswith("Error") and "content" in out
    assert _memory_md(settings) == before


def test_edit_rejects_over_limit_without_touching_file(registry, settings, mem):
    """150 行是硬线：再加一行整份拒绝，文件一个字节都不动（D-15 口径）。"""
    path = settings.data_dir / "memory.md"
    path.write_text(
        "\n".join(
            [
                "# Memory",
                "",
                core_files.MEMORY_FORMAT_MARKER,
                "",
                "## 手写笔记",
                *["- 填充行" for _ in range(145)],
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    before = _memory_md(settings)
    assert core_files.active_line_count(before) == core_files.MEMORY_MAX_LINES

    out = _edit(registry, op="add", section="手写笔记", content="再加一行")

    assert out.startswith("Error") and "150" in out
    assert _memory_md(settings) == before


def test_edit_leaves_file_and_db_in_sync(registry, settings, conn, mem):
    """编辑后立刻同步：sha256 收敛，下次启动不会再把这次改动重放一遍。"""
    registry.execute("save_memory", {"subject": "偏好", "content": "用户喜欢看 NBA"})

    _edit(registry, op="replace", match="NBA", content="用户喜欢看 NBA 直播")

    assert "用户喜欢看 NBA 直播" in _memory_md(settings)
    assert sync.should_sync(conn) is False
    assert (settings.data_dir / sync.HASH_FILE).is_file()


def test_detect_intent_marks_memory_file_edit_by_rules():
    """纯规则打标：点名要改记忆文件才打，只是提到 memory.md 不算。"""
    assert detect_intent("把 memory.md 里那条重复的删掉") == "MEMORY_EDIT"
    assert detect_intent("整理一下记忆文件") == "MEMORY_EDIT"
    assert detect_intent("记住我喜欢悬疑小说") == "REMEMBER"  # 既有打标不受影响
    assert detect_intent("我这边 memory.md 还是原来的") == ""


def test_memory_edit_contract_is_injected_only_on_user_request(settings, conn, clock, mem):
    """契约只在用户点名要改时注入，且写明"只在回复里列清单等于没改"。"""
    asked = SessionManager(settings, store=conn, session_id="cli:test", clock=clock)
    asked.begin_turn("把 memory.md 里那条重复的删掉")

    contract = asked.turn_contract_block()

    assert 'manage_memory(action="edit"' in contract
    assert "只在回复里列" in contract

    chatty = SessionManager(settings, store=conn, session_id="cli:test", clock=clock)
    chatty.begin_turn("我这边 memory.md 还是原来的")
    assert chatty.turn_contract_block() == ""
