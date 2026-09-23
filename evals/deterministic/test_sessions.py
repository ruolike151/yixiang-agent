"""会话管理的数据层：改名 / 搜索 / 导出 / 删除（TECH §10.1 历史面板的四个动作）。

这一组用例守的是同一件事的两面：
  * ``chat_log`` 是"聊过什么"，删掉它不该动到 ``facts`` / ``episodes``（"记住什么"）；
  * 标题是会话的元数据，缺省值永远是"首条用户消息前 60 字"——用户改了就优先用它，
    改回空的就回到缺省，不需要"重置"这种额外按钮。
"""

from __future__ import annotations

from yixiang.runtime.session import TITLE_LIMIT

CREATED_AT = "2026-09-19T10:00:00+08:00"


def seed(session) -> None:
    """两个会话、三条往来：给改名 / 搜索 / 导出 / 删除各留出可断言的数据。

    最后切回第一个会话：后面几条用例说的"当前会话"就是它。假时钟停在同一分钟，
    两个会话的 ``created_at`` 一模一样，列表顺序不保证——所以断言一律按
    ``session_id`` 找，不用下标。
    """
    session.add_exchange("悬疑小说的线索怎么铺", "先立三个可疑的人，再让最不像的那个动手。")
    session.add_exchange("RAG 的召回率怎么算", "召回 = 命中 / 应命中，跟精确率分开看。")
    session.new_session("复习")
    session.add_exchange("另一句", "记下了。")
    session.switch("cli:test")


def title_of(session, session_id: str) -> str:
    """按 id 取标题（列表顺序不保证，下标断言会变成随机失败）。"""
    return next(
        item["title"] for item in session.list_sessions() if item["session_id"] == session_id
    )


def test_rename_overrides_default_title_and_empty_restores_it(session):
    seed(session)
    assert session.list_sessions()[0]["title"] in {"悬疑小说的线索怎么铺", "另一句"}

    assert session.rename_session("cli:test", "九月的悬疑") is True
    assert title_of(session, "cli:test") == "九月的悬疑"

    assert session.rename_session("cli:test", "   ") is False
    assert title_of(session, "cli:test") == "悬疑小说的线索怎么铺"


def test_rename_truncates_to_title_limit(session):
    session.add_exchange("一句话", "好。")
    session.rename_session("cli:test", "长" * 200)
    assert len(title_of(session, "cli:test")) == TITLE_LIMIT


def test_search_hits_reply_text_not_only_the_title(session):
    seed(session)

    hits = session.search_sessions("应命中")
    assert [item["session_id"] for item in hits] == ["cli:test"]
    # 命中的是"会话"，不是"那几行"：轮数与默认标题仍按该会话全部往来算，
    # 否则搜"应命中"会把这条 2 轮的会话显示成"1 轮 · RAG 的召回率怎么算"。
    assert hits[0]["turns"] == 2
    assert hits[0]["title"] == "悬疑小说的线索怎么铺"
    # 用户改过名字就按用户的名字走（搜索命中的会话也一样）
    session.rename_session("cli:test", "九月的悬疑")
    assert session.search_sessions("应命中")[0]["title"] == "九月的悬疑"

    # 空查询 = 不筛：变成"列出全部"，前端清空搜索框时要的就是这个行为
    everything = {item["session_id"] for item in session.search_sessions("")}
    assert everything == {"cli:test", "cli:20260919-1000-复习"}
    assert session.search_sessions("完全不存在的话") == []


def test_export_is_chronological_and_complete(session):
    seed(session)

    payload = session.export_session("cli:test")
    assert payload["session_id"] == "cli:test"
    assert payload["exported_at"] == CREATED_AT
    assert [turn["user"] for turn in payload["turns"]] == [
        "悬疑小说的线索怎么铺",
        "RAG 的召回率怎么算",
    ]
    assert payload["turns"][0]["reply"] == "先立三个可疑的人，再让最不像的那个动手。"
    assert payload["turns"][0]["tools"] == []
    assert session.export_session("cli:不存在")["turns"] == []


def test_delete_removes_turns_but_keeps_long_term_memory(session):
    seed(session)
    session.store.execute(
        "INSERT INTO facts(subject, content, source, created_at, updated_at)"
        " VALUES ('偏好', '喜欢悬疑', 'user', ?, ?)",
        (CREATED_AT, CREATED_AT),
    )
    session.store.commit()

    assert session.delete_session("cli:test") == 2
    assert session.transcript() == []
    assert "cli:test" not in {item["session_id"] for item in session.list_sessions()}
    # 记忆不属于某一次会话：删历史不抹记忆
    assert session.store.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 1
    # 同一个会话再聊，往来表从 0 重新数
    session.add_exchange("重新开始", "好。")
    assert len(session.transcript()) == 1
