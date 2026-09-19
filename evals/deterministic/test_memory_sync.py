"""``memory.md ⟷ facts`` 双向同步（D-06、D-07、D-08）。

这是 PART 2 最容易丢数据的一处，所以单独一个文件压住：**文件为准**，
条目级 id 是人对齐的唯一锚点。
"""

from __future__ import annotations

import json

import pytest

from yixiang import memory
from yixiang.memory import core_files, semantic, sync


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
    core_files.ensure_memory_file(settings.data_dir)
    yield memory.current()
    memory.reset()


def _path(settings):
    return settings.data_dir / sync.MEMORY_FILE


def _read(settings) -> str:
    return _path(settings).read_text(encoding="utf-8")


def _write(settings, text: str) -> None:
    """模拟"人直接改文件"：原子写一条也不落，哈希自然变。"""
    core_files.atomic_write_text(_path(settings), text)


def _alive(conn) -> list[int]:
    return [
        int(row["id"])
        for row in conn.execute("SELECT id FROM facts WHERE deleted = 0 ORDER BY id").fetchall()
    ]


def _save(registry, content: str, subject: str = "偏好") -> int:
    payload = json.loads(
        registry.execute("save_memory", {"subject": subject, "content": content})
    )
    return int(payload["id"])


# ------------------------------------------------------------------ D-06
def test_d06_deleting_a_line_soft_deletes_and_stops_injection(registry, conn, settings, mem):
    keep = _save(registry, "用户喜欢看 NBA 篮球比赛")
    drop = _save(registry, "用户周末喜欢睡到十点")

    store = semantic.FactStore(semantic.context())
    assert drop in [hit.id for hit in store.search_fts("周末睡到十点")]

    lines = [line for line in _read(settings).splitlines() if f"[{drop}]" not in line]
    _write(settings, "\n".join(lines))

    report = sync.sync_memory_md(conn)

    assert report.deleted == 1
    assert int(conn.execute("SELECT deleted FROM facts WHERE id = ?", (drop,)).fetchone()["deleted"]) == 1
    assert drop not in _alive(conn)
    # 不再注入：核心区文本里没有它
    assert f"[{drop}]" not in core_files.core_memory_text(_read(settings))
    # 不再检索：FTS 索引已重建
    assert drop not in [hit.id for hit in store.search_fts("周末睡到十点")]
    # 删的是哪一条就是哪一条，别的条目不受影响
    assert keep in _alive(conn)


def test_d06_deleted_entry_can_be_restored_from_recycle_bin(registry, conn, settings, mem):
    """软删不是物理删：``manage_memory(restore)`` 能把被删的记忆捞回来。"""
    fact_id = _save(registry, "用户对花生过敏")
    lines = [line for line in _read(settings).splitlines() if f"[{fact_id}]" not in line]
    _write(settings, "\n".join(lines))
    sync.sync_memory_md(conn)

    payload = json.loads(registry.execute("manage_memory", {"action": "restore", "id": fact_id}))

    assert payload["action"] == "restore"
    assert fact_id in _alive(conn)
    assert f"[{fact_id}] 用户对花生过敏" in _read(settings)


# ------------------------------------------------------------------ D-07
def test_d07_manual_line_without_id_is_imported_and_written_back(registry, conn, settings, mem):
    text = _read(settings).replace("## 偏好", "## 偏好\n- 用户偏爱深色主题", 1)
    _write(settings, text)

    report = sync.sync_memory_md(conn)

    assert report.inserted == 1
    row = conn.execute(
        "SELECT id, subject, source FROM facts WHERE content = ?", ("用户偏爱深色主题",)
    ).fetchone()
    assert row is not None
    assert row["subject"] == "偏好"
    assert row["source"] == "file"  # 来自文件而不是工具
    # 回写：新导入的条目拿到了 id，人就靠它继续编辑
    assert f"- [{int(row['id'])}] 用户偏爱深色主题" in _read(settings)
    assert sync.verify(conn) == []


def test_d07_unchanged_file_skips_sync(registry, conn, settings, mem):
    """哈希一致就不重复跑同步（启动路径每轮都会调它）。"""
    _save(registry, "用户用 Windows 开发")

    report = sync.sync_memory_md(conn)

    assert report.skipped is True
    assert report.touched is False


# ------------------------------------------------------------------ D-08
def test_d08_conversational_governance_keeps_db_and_file_aligned(registry, conn, settings, mem):
    ids = [
        _save(registry, "用户喜欢看 NBA 篮球比赛"),
        _save(registry, "用户周末喜欢睡到十点"),
        _save(registry, "用户对花生过敏"),
    ]

    listing = registry.execute("manage_memory", {"action": "search"})
    for fact_id in ids:
        assert f"[{fact_id}]" in listing

    updated = json.loads(
        registry.execute(
            "manage_memory",
            {"action": "update", "id": ids[1], "content": "用户周末习惯睡到十点半"},
        )
    )
    deleted = json.loads(registry.execute("manage_memory", {"action": "delete", "id": ids[2]}))

    assert updated["action"] == "update"
    assert deleted["action"] == "delete"

    text = _read(settings)
    assert f"- [{ids[1]}] 用户周末习惯睡到十点半" in text
    assert f"[{ids[2]}]" not in text
    # 三方对账（文件 / 存活 fact / FTS 索引）零漂移
    assert sync.verify(conn) == []
    # 再渲染一次逐字符一致：渲染是幂等的，不会每次同步都抖动
    sync.write_memory_doc(conn)
    assert _read(settings) == text


def test_d08_tool_write_survives_restart_sync(registry, conn, settings, mem):
    """重启（再跑一次同步）不会把人机共治的结果改回去。"""
    fact_id = _save(registry, "用户的生日是 3 月 14 日")
    text = _read(settings)

    report = sync.sync_memory_md(conn)

    assert report.skipped is True
    assert _read(settings) == text
    assert fact_id in _alive(conn)


def test_manual_garbage_line_is_kept_as_handwritten_note(conn, mem, settings):
    """解析不了的行走"手写笔记"，不报错也不丢内容（§5 第 2 条）。"""
    text = _read(settings).replace("## 手写笔记", "## 手写笔记\n随手记一句：明天带伞", 1)
    _write(settings, text)

    report = sync.sync_memory_md(conn)

    assert report.inserted == 0
    assert "随手记一句：明天带伞" in _read(settings)
    assert _alive(conn) == []
