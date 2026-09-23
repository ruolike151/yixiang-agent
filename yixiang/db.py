"""数据层：SQLite 连接、DDL 与迁移（TECH-DESIGN §12）。

设计要点：
  * 迁移用 SQLite 自带的 ``PRAGMA user_version``，不引迁移框架（§12.4）；
    迁移只增不改，每个迁移都要能从空库一路跑到最新。
  * 连接统一带 ``WAL`` / ``busy_timeout=5000`` / ``foreign_keys=ON``（§12.3）；
    单进程单连接 + 写操作串行，不用 ``check_same_thread=False`` 开多写连接。
  * PART 1 只用到 ``memos`` / ``plans`` / ``plan_items`` / ``chat_log`` / ``meta``，
    其余表按 §12.1 一次性建好，PART 2/3 直接可用。
  * 向量表（``vec0``）依赖 sqlite-vec 扩展，属于 PART 2/3 的索引重建路径，
    这里只提供 ``load_sqlite_vec`` 供 doctor 探测（§1.2 检查项 3）。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 2

# 迁移 m001：建表 + 建索引（§12.1 的完整 DDL，逐条语句列出便于事务包裹）
_M001_INIT: tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS meta(
        key TEXT PRIMARY KEY,
        value TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS facts(
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        subject      TEXT NOT NULL,
        content      TEXT NOT NULL,
        source       TEXT NOT NULL DEFAULT 'user',
        pinned       INTEGER NOT NULL DEFAULT 0,
        deleted      INTEGER NOT NULL DEFAULT 0,
        deleted_at   TEXT,
        created_at   TEXT NOT NULL,
        updated_at   TEXT NOT NULL,
        last_used_at TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_facts_alive ON facts(deleted, subject)",
    "CREATE INDEX IF NOT EXISTS idx_facts_used ON facts(last_used_at)",
    """CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts
       USING fts5(content_tok, content='', content_rowid='rowid')""",
    """CREATE TABLE IF NOT EXISTS chat_log(
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id   TEXT NOT NULL,
        source       TEXT NOT NULL,
        user_text    TEXT NOT NULL,
        reply_text   TEXT NOT NULL,
        tools_json   TEXT,
        created_at   TEXT NOT NULL,
        consolidated INTEGER NOT NULL DEFAULT 0
    )""",
    "CREATE INDEX IF NOT EXISTS idx_chatlog_session ON chat_log(session_id, id)",
    "CREATE INDEX IF NOT EXISTS idx_chatlog_unconsolidated ON chat_log(consolidated, id)",
    """CREATE TABLE IF NOT EXISTS episodes(
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        happened_at    TEXT NOT NULL,
        summary        TEXT NOT NULL,
        session_id     TEXT,
        source_chat_id INTEGER,
        created_at     TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS plans(
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        title      TEXT NOT NULL,
        goal       TEXT,
        start_date TEXT,
        end_date   TEXT,
        status     TEXT NOT NULL DEFAULT 'active',
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS plan_items(
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        plan_id     INTEGER NOT NULL REFERENCES plans(id),
        date        TEXT NOT NULL,
        content     TEXT NOT NULL,
        est_minutes INTEGER,
        status      TEXT NOT NULL DEFAULT 'todo',
        created_at  TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_plan_items_date ON plan_items(date, status)",
    """CREATE TABLE IF NOT EXISTS memos(
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        content         TEXT NOT NULL,
        due_at          TEXT,
        done            INTEGER NOT NULL DEFAULT 0,
        created_at      TEXT NOT NULL,
        done_at         TEXT,
        idempotency_key TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_memos_open ON memos(done, due_at)",
    # idempotency_key 是 §9.1 对写类工具的硬要求（调度 / QQ 重投递必须带），
    # 因此比 §12.1 的示例 DDL 多一列，并配唯一索引（NULL 不参与唯一约束）。
    """CREATE UNIQUE INDEX IF NOT EXISTS idx_memos_idem
       ON memos(idempotency_key) WHERE idempotency_key IS NOT NULL""",
    """CREATE TABLE IF NOT EXISTS media(
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        source_id       TEXT NOT NULL UNIQUE,
        title           TEXT NOT NULL,
        title_zh        TEXT,
        mtype           TEXT,
        year            INTEGER,
        genres          TEXT,
        rating          REAL,
        synopsis        TEXT,
        cover_url       TEXT,
        embed_text_hash TEXT,
        created_at      TEXT NOT NULL,
        updated_at      TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_media_mtype_year ON media(mtype, year)",
    """CREATE VIRTUAL TABLE IF NOT EXISTS media_fts
       USING fts5(title_tok, synopsis_tok, content='', content_rowid='rowid')""",
    """CREATE TABLE IF NOT EXISTS recommend_log(
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        media_id       INTEGER NOT NULL REFERENCES media(id),
        recommended_on TEXT NOT NULL,
        channel        TEXT,
        feedback       TEXT,
        created_at     TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_recommend_date ON recommend_log(recommended_on)",
    """CREATE TABLE IF NOT EXISTS embedding_cache(
        content_hash TEXT PRIMARY KEY,
        model        TEXT NOT NULL,
        dim          INTEGER NOT NULL,
        vector       BLOB NOT NULL,
        created_at   TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS scheduled_runs(
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        job        TEXT NOT NULL,
        run_date   TEXT NOT NULL,
        status     TEXT NOT NULL,
        detail     TEXT,
        created_at TEXT NOT NULL,
        UNIQUE(job, run_date)
    )""",
    """CREATE TABLE IF NOT EXISTS processed_messages(
        message_id  TEXT PRIMARY KEY,
        received_at TEXT NOT NULL,
        handled_at  TEXT
    )""",
)

# 迁移 m002：会话自定义标题（§10.1 历史面板的"改名"）。
# 新开一张表而不是给 chat_log 加列：标题是会话的元数据，不是某一条流水的一部分；
# 而且加列会碰到已有的 INSERT 语句（只增不改）。
_M002_SESSION_TITLES: tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS session_titles(
        session_id TEXT PRIMARY KEY,
        title      TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_session_titles_updated ON session_titles(updated_at)",
)

MIGRATIONS: tuple[tuple[str, ...], ...] = (_M001_INIT, _M002_SESSION_TITLES)

# D-26 / doctor 用的表清单（含 FTS 虚表）
EXPECTED_TABLES: tuple[str, ...] = (
    "meta",
    "facts",
    "facts_fts",
    "chat_log",
    "episodes",
    "plans",
    "plan_items",
    "memos",
    "media",
    "media_fts",
    "recommend_log",
    "embedding_cache",
    "scheduled_runs",
    "processed_messages",
    "session_titles",
)


def connect(path: Path | str) -> sqlite3.Connection:
    """打开（必要时创建）数据库，并设置 WAL / busy_timeout / foreign_keys。"""
    db_path = Path(path)
    if str(db_path) != ":memory:":
        db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def user_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def migrate(conn: sqlite3.Connection) -> int:
    """把 schema 迁到最新版本，返回迁移后的 ``user_version``（幂等）。"""
    version = user_version(conn)
    for index in range(version, len(MIGRATIONS)):
        statements = MIGRATIONS[index]
        conn.execute("BEGIN")
        try:
            for statement in statements:
                conn.execute(statement)
            conn.execute(f"PRAGMA user_version = {index + 1}")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
    return user_version(conn)


def table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table', 'view') ORDER BY name"
    ).fetchall()
    return {row["name"] for row in rows}


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    with conn:
        conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def get_meta(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def load_sqlite_vec(conn: sqlite3.Connection) -> bool:
    """尝试加载 sqlite-vec 扩展；失败返回 False（doctor 只告警，不阻断）。

    向量表与索引重建（``rag reindex`` / ``memory rebuild``）归 PART 2/3，
    这里只作为"环境是否具备能力"的探测点。
    """
    try:
        import sqlite_vec  # 延迟导入：PART 1 的确定性用例不需要它

        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        conn.execute("SELECT vec_version()").fetchone()
        return True
    except Exception:
        return False
