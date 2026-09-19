"""容量与淘汰（§7.11）：活跃区 ≤150 行、归档只提示不删、置顶保护。

批量灌数据走 SQL 直插：这里测的是"渲染 / 淘汰"这条路径，不是写入工具，
没必要为 500 条记忆付 500 次嵌入。
"""

from __future__ import annotations

import pytest

from yixiang import memory
from yixiang.memory import core_files, semantic, sync

NOW = "2026-09-19T10:00:00+08:00"


@pytest.fixture
def mem(settings, conn, clock):
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


def _bulk(conn, count: int, *, subject: str = "偏好", prefix: str = "备忘条目") -> None:
    with conn:
        conn.executemany(
            "INSERT INTO facts(subject, content, source, pinned, created_at, updated_at,"
            " last_used_at) VALUES(?, ?, 'tool', 0, ?, ?, ?)",
            [(subject, f"{prefix}第 {i} 条", NOW, NOW, NOW) for i in range(1, count + 1)],
        )


def _archive_span(text: str) -> int:
    bounds = core_files.section_bounds(text.splitlines(), core_files.ARCHIVE_SECTION)
    return 0 if bounds is None else bounds[1] - bounds[0]


def test_active_area_stays_within_the_line_budget(conn, settings, mem):
    _bulk(conn, 200)

    sync.write_memory_doc(conn)

    text = _read(settings)
    assert core_files.active_line_count(text) <= core_files.MEMORY_MAX_LINES
    assert _archive_span(text) > 0  # 溢出的条目进了停尸房
    # 淘汰只搬家，不物理删：一条都没少
    assert int(conn.execute("SELECT COUNT(*) FROM facts WHERE deleted = 0").fetchone()[0]) == 200


def test_pinned_entries_are_not_evicted(conn, settings, mem):
    _bulk(conn, 200)
    with conn:
        conn.execute("UPDATE facts SET pinned = 1 WHERE id = 1")  # 最老、最弱的一条

    sync.write_memory_doc(conn)

    text = _read(settings)
    core = core_files.core_memory_text(text)
    assert "- [1]* 备忘条目第 1 条" in core  # 置顶 + 存活
    assert core_files.active_line_count(text) <= core_files.MEMORY_MAX_LINES
    archived = text.split(f"## {core_files.ARCHIVE_SECTION}", 1)[1]
    assert "[1]*" not in archived


def test_eviction_prefers_the_least_recently_used(conn, settings, mem):
    _bulk(conn, 200)
    with conn:
        conn.execute(
            "UPDATE facts SET last_used_at = '2026-09-19T11:00:00+08:00' WHERE id = 7"
        )

    sync.write_memory_doc(conn)

    archived = _read(settings).split(f"## {core_files.ARCHIVE_SECTION}", 1)[1]
    assert "[7]" not in archived  # 最近用过 → 留下
    assert "[1]" in archived  # 从没用过 → 先搬走


def test_oversized_archive_only_warns(conn, settings, mem):
    """归档段超 300 行只提示人工清理——自动删记忆是不可接受的（§7.11）。"""
    _bulk(conn, 520)

    sync.write_memory_doc(conn)

    text = _read(settings)
    assert _archive_span(text) > core_files.MEMORY_ARCHIVE_MAX_LINES
    problems = core_files.validate_memory_md(text)
    assert any("建议人工清理" in problem for problem in problems)
    assert int(conn.execute("SELECT COUNT(*) FROM facts WHERE deleted = 0").fetchone()[0]) == 520


def test_capacity_maintenance_keeps_three_way_alignment(conn, settings, mem):
    _bulk(conn, 200, subject="用户")
    sync.write_memory_doc(conn)
    semantic.rebuild_facts_fts(conn)

    assert sync.verify(conn) == []
