"""D-26 迁移：空库 → ``migrate()`` → ``user_version`` 最新、表与外键齐备（TECH §12.4 / §13.3）。

迁移是"每个迁移都必须能从空库一路跑到最新"这条规则的守门人：三处断言分别盯住
三件会各自被改坏的事：

  ① 版本号——``user_version`` 真的被推进到 ``SCHEMA_VERSION``，不靠代码里的常量自证；
  ② 表清单——``EXPECTED_TABLES``（含两张 FTS5 虚表）一个不少，PART 2/3 才能直接开工；
  ③ 外键——``foreign_keys=ON`` 不只是打开开关，违反引用完整性要真的报错。

另外把 ``migrate()`` 的**幂等**钉住：重复调用不改版本、不重建、不报错。
"""

from __future__ import annotations

import sqlite3

import pytest

from yixiang import db

CREATED_AT = "2026-09-19T10:00:00+08:00"


def test_d26_empty_db_migrates_to_latest_version_with_all_tables():
    conn = db.connect(":memory:")
    try:
        assert db.user_version(conn) == 0  # 空库的起点：一条迁移都没跑过

        version = db.migrate(conn)

        assert version == db.SCHEMA_VERSION
        assert db.user_version(conn) == db.SCHEMA_VERSION
        missing = sorted(set(db.EXPECTED_TABLES) - db.table_names(conn))
        assert missing == []
    finally:
        conn.close()


def test_migrate_is_idempotent():
    conn = db.connect(":memory:")
    try:
        db.migrate(conn)
        tables_after_first = db.table_names(conn)

        assert db.migrate(conn) == db.SCHEMA_VERSION  # 再跑一次仍返回最新版本

        assert db.user_version(conn) == db.SCHEMA_VERSION
        assert db.table_names(conn) == tables_after_first
    finally:
        conn.close()


def test_foreign_keys_are_declared_and_enforced():
    conn = db.connect(":memory:")
    try:
        db.migrate(conn)

        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA foreign_key_list(plan_items)").fetchall()  # plan_id → plans.id
        assert conn.execute("PRAGMA foreign_key_list(recommend_log)").fetchall()  # media_id → media.id

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO plan_items(plan_id, date, content, created_at) VALUES (?, ?, ?, ?)",
                (999, "2026-09-19", "悬空计划项", CREATED_AT),
            )

        conn.execute(
            "INSERT INTO plans(title, status, created_at) VALUES (?, 'active', ?)",
            ("两周 RAG 复习", CREATED_AT),
        )
        plan_id = conn.execute("SELECT id FROM plans").fetchone()["id"]
        with conn:
            conn.execute(
                "INSERT INTO plan_items(plan_id, date, content, created_at) VALUES (?, ?, ?, ?)",
                (plan_id, "2026-09-19", "读 RAG 论文", CREATED_AT),
            )
        assert conn.execute("SELECT COUNT(*) AS n FROM plan_items").fetchone()["n"] == 1
    finally:
        conn.close()


def test_file_db_is_created_with_parent_dirs_and_wal(tmp_path):
    path = tmp_path / "nested" / "state.db"

    conn = db.connect(path)
    try:
        db.migrate(conn)
        assert path.is_file()
        assert db.user_version(conn) == db.SCHEMA_VERSION
        assert str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal"
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    finally:
        conn.close()


def test_migration_from_v1_keeps_chat_log_and_adds_session_titles():
    """v1 老库升到最新：只加表加索引，一行数据都不许动。

    用户手上那个 ``data/state.db`` 现在就是 ``user_version=1``——升级路径要是要求
    "删库重来"，等于让每个人丢掉自己的聊天记录。
    """
    conn = db.connect(":memory:")
    try:
        for statement in db.MIGRATIONS[0]:  # 手工停在 v1：这就是用户手上那个库的样子
            conn.execute(statement)
        conn.execute("PRAGMA user_version = 1")
        conn.execute(
            "INSERT INTO chat_log(session_id, source, user_text, reply_text, tools_json,"
            " created_at) VALUES ('web:default', 'web', 'Q1', 'R1', '[]', ?)",
            (CREATED_AT,),
        )
        conn.commit()

        assert db.migrate(conn) == db.SCHEMA_VERSION == 2
        assert "session_titles" in db.table_names(conn)
        assert [
            (row["user_text"], row["reply_text"])
            for row in conn.execute("SELECT user_text, reply_text FROM chat_log").fetchall()
        ] == [("Q1", "R1")]
    finally:
        conn.close()
