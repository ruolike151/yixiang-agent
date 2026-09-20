"""恢复演练：把快照当成唯一的库源，在临时目录里重建一份 ``data/`` 并逐项对账。

为什么单独写一个脚本：**"备份生成了"和"备份能恢复"是两件不同的事**。前者一条
``yixiang backup`` 就证明了，后者必须真的把快照当源数据启动一次（PART-4 §1 的验收项
"演练过一次恢复"）。写成脚本而不是一段手工命令，是因为这件事以后反复要做，做的时候
不该有"上次是怎么敲的"这种记忆负担——脚本本身就是演练记录。

四步，每步都是一条独立断言（任何一步失败即退出码 1）：

  1. **重建**：快照 → 临时目录的 ``state.db``；三文件与 ``skills/`` 原样拷过去。
     恢复的语义是"把 ``data/`` 退回某个时刻"，所以只拷快照与三文件，不合并现役数据。
  2. **表结构**：临时库与现役库的表集合、``user_version`` 必须一致——少了表说明快照是
     旧版本或坏文件，恢复上来会缺功能。
  3. **行数**：逐表 ``count`` 相等（快照与现役库理应一致；不等就打印明细，让"备份之后
     又聊过天"和"恢复丢了数据"能被区分开）。
  4. **三方对账**：在恢复目录里跑一次 ``memory verify``（``memory.md`` / 数据库 / FTS
     索引），必须"无漂移"——这一条才是"记忆真的回来了"。

用法::

    uv run python scripts/restore_drill.py
    uv run python scripts/restore_drill.py --snapshot data/backups/state-20260919.db
    uv run python scripts/restore_drill.py --keep     # 留下来恢复出来的目录，人工翻看

退出码：0 = 恢复可用；1 = 有对账失败。
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:  # 让脚本不依赖"当前目录在哪"也能直接跑
    sys.path.insert(0, str(PROJECT_ROOT))

from yixiang import db, memory  # noqa: E402 - 必须先补 sys.path 再 import 本仓库的包
from yixiang.memory import sync  # noqa: E402

CORE_FILES = ("soul.md", "user.md", "memory.md")


def latest_snapshot(data_dir: Path) -> Path | None:
    """``backups/`` 里文件名日期最大的那份（文件名就是快照自己的时间戳）。"""
    backups = data_dir / "backups"
    if not backups.is_dir():
        return None
    snapshots = sorted(backups.glob("state-*.db"))
    return snapshots[-1] if snapshots else None


def rebuild(snapshot: Path, data_dir: Path, target: Path) -> Path:
    """把快照与三文件拷进 ``target``，返回恢复后的库路径。"""
    target.mkdir(parents=True, exist_ok=True)
    restored = target / "state.db"
    shutil.copy2(snapshot, restored)
    for name in CORE_FILES:
        source = data_dir / name
        if source.is_file():
            shutil.copy2(source, target / name)
    skills = data_dir / "skills"
    if skills.is_dir():
        shutil.copytree(skills, target / "skills")
    return restored


def _tables(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' OR type = 'view' ORDER BY name"
    )
    return [str(row[0]) for row in rows if not str(row[0]).startswith("sqlite_")]


def compare_schema(live: sqlite3.Connection, restored: sqlite3.Connection) -> list[str]:
    """表集合 + ``user_version``：对不上就是"恢复上来的库不是这个版本的应用该有的样子"。"""
    problems: list[str] = []
    live_tables, restored_tables = _tables(live), _tables(restored)
    if live_tables != restored_tables:
        problems.append(f"表集合不一致：现役 {live_tables} / 恢复 {restored_tables}")
    live_version = db.user_version(live)
    restored_version = db.user_version(restored)
    if live_version != restored_version:
        problems.append(f"user_version 不一致：现役 {live_version} / 恢复 {restored_version}")
    return problems


def compare_rows(
    live: sqlite3.Connection, restored: sqlite3.Connection
) -> tuple[list[str], list[str]]:
    """逐表行数；返回 ``(不一致的表, 读不了的虚拟表)``。

    ``vec0`` 虚拟表的读法依赖 sqlite-vec 扩展；扩展不可用时**跳过并说明**，而不是
    把"索引读不了"报成"恢复失败"——索引本来就可以用 ``rag reindex`` 重建，记忆正文
    与 ``facts`` 表才是不可重建的资产。
    """
    mismatched: list[str] = []
    skipped: list[str] = []
    for table in _tables(restored):
        try:
            live_count = live.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            restored_count = restored.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        except sqlite3.OperationalError as exc:
            skipped.append(f"{table}（{exc}）")
            continue
        if live_count != restored_count:
            mismatched.append(f"{table}：现役 {live_count} 行 / 恢复 {restored_count} 行")
    return mismatched, skipped


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python scripts/restore_drill.py",
        description="恢复演练：快照 → 临时目录 → 表结构 / 行数 / 记忆三方对账",
    )
    parser.add_argument("--data-dir", default=str(PROJECT_ROOT / "data"), help="现役数据目录")
    parser.add_argument("--snapshot", default=None, help="指定快照文件（默认取最新的一份）")
    parser.add_argument("--keep", action="store_true", help="保留恢复出来的临时目录")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    data_dir = Path(args.data_dir)
    snapshot = Path(args.snapshot) if args.snapshot else latest_snapshot(data_dir)
    if snapshot is None or not snapshot.is_file():
        print(f"找不到快照（{snapshot}）：先跑一次 yixiang backup")
        return 1

    workdir = Path(tempfile.mkdtemp(prefix="yixiang-restore-"))
    size_kb = snapshot.stat().st_size / 1024
    print(f"恢复演练：快照 {snapshot.name}（{size_kb:.1f} KB）→ {workdir}")
    problems: list[str] = []
    try:
        restored_path = rebuild(snapshot, data_dir, workdir)
        restored = db.connect(restored_path)
        live_path = data_dir / "state.db"
        live = db.connect(live_path) if live_path.is_file() else None
        vec_ready = db.load_sqlite_vec(restored)
        try:
            integrity = str(restored.execute("PRAGMA integrity_check").fetchone()[0])
            print(
                f"① 重建：{restored_path.name} · integrity_check = {integrity}"
                f" · sqlite-vec {'已加载' if vec_ready else '不可用'}"
            )
            if integrity != "ok":
                problems.append(f"integrity_check = {integrity}")

            if live is None:
                print("② 表结构：现役库不存在，跳过对照")
                print("③ 行数：现役库不存在，跳过对照")
            else:
                db.load_sqlite_vec(live)
                schema = compare_schema(live, restored)
                problems.extend(schema)
                tables = len(_tables(restored))
                version = db.user_version(restored)
                if schema:
                    print("② 表结构：" + "；".join(schema))
                else:
                    print(f"② 表结构：{tables} 张表 / user_version {version} 与现役一致")
                mismatched, skipped = compare_rows(live, restored)
                problems.extend(mismatched)
                if mismatched:
                    print("③ 行数：" + "；".join(mismatched))
                else:
                    tail = f"（{len(skipped)} 张虚拟表跳过：" + "、".join(skipped) + "）"
                    print("③ 行数：逐表与现役一致" + (tail if skipped else ""))

            memory.reset()
            memory.configure(restored, data_dir=workdir)
            drift = sync.verify(restored)
            problems.extend(drift)
            print(
                "④ 三方对账："
                + ("memory.md / 数据库 / FTS 索引一致" if not drift else "；".join(drift))
            )
        finally:
            restored.close()
            if live is not None:
                live.close()
            memory.reset()
    finally:
        if args.keep:
            print(f"保留恢复目录：{workdir}")
        else:
            shutil.rmtree(workdir, ignore_errors=True)

    if problems:
        print(f"恢复演练失败：{len(problems)} 项对不上")
        return 1
    print("恢复演练通过：这份快照可以当作唯一的库源启动")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
