"""备份与保留：``VACUUM INTO`` 日快照 + ``data/`` 私有仓 + 30 天回收（TECH §12.4）。

一句话：**记忆是本项目最有价值的产物，它不是可有可无的缓存**。所以备份分两条腿：

  1. **``state.db`` 日用快照**——``VACUUM INTO`` 在 WAL 下是安全的（不需要停机，
     拿到的是一个一致的、紧凑的副本）。文件名带日期，同一天重复跑只留一份。
  2. **三文件进 ``data/`` 私有仓**——``soul.md`` / ``user.md`` / ``memory.md`` 是
     人机共治的正文，diff 就是"记忆这几天怎么变的"。仓库**只版本化**这三样加
     ``skills/`` 与 ``briefs/``，其余（库 / 日志 / trace / 用量 / 备份）全部不进
     ——它们要么能重新生成，要么体积大，要么含隐私（§14.2 T-9）。

两条纪律：

  * ``data/`` 私有仓**永远不推远端**：它是本机资产的版本控制，不是分享通道；
  * 回收只删 ``backups/state-*.db``：其它文件（包括用户手放的）一律不碰。
"""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
from datetime import date, datetime, timedelta
from pathlib import Path

from yixiang.runtime.models import Clock, SystemClock

BACKUP_DIRNAME = "backups"
SNAPSHOT_PREFIX = "state-"
SNAPSHOT_SUFFIX = ".db"
DEFAULT_KEEP_DAYS = 30

GIT_USER_NAME = "yixiang"
GIT_USER_EMAIL = "yixiang@local"

# ``data/`` 私有仓的忽略规则：只留记忆三文件 / skills / briefs（§12.4 冻结口径）
PRIVATE_GITIGNORE = """\
# data/ 私有仓：**只版本化** soul.md / user.md / memory.md / skills / briefs
# （TECH §12.4、§14.2 T-9）。其余都是能重新生成、体积大或含隐私的东西。
*.db
*.db-wal
*.db-shm
*.tmp
backups/
logs/
traces/
usage.jsonl
reports/
raw/
media/
"""


def snapshot_path(data_dir: Path | str, day: date) -> Path:
    return Path(data_dir) / BACKUP_DIRNAME / f"{SNAPSHOT_PREFIX}{day:%Y%m%d}{SNAPSHOT_SUFFIX}"


def backup_now(
    data_dir: Path | str,
    *,
    clock: Clock | None = None,
    commit: bool = True,
) -> Path:
    """做一份当日快照并（可选）在 ``data/`` 私有仓里提交一次。

    ``commit=False`` 只做快照——恢复演练与用例用它，免得在临时目录里留下一堆
    提交记录。返回快照文件路径。
    """
    data_dir = Path(data_dir)
    day = (clock or SystemClock()).now().astimezone().date()
    data_dir.mkdir(parents=True, exist_ok=True)
    db_path = data_dir / "state.db"
    if not db_path.is_file():  # 干净环境里第一条命令不该直接失败：建一张空库再备份
        from yixiang import db as db_module

        connection = db_module.connect(db_path)
        try:
            db_module.migrate(connection)
        finally:
            connection.close()

    target = snapshot_path(data_dir, day)
    target.parent.mkdir(parents=True, exist_ok=True)
    # 先写临时文件再替换：VACUUM INTO 不覆盖已存在的目标，且中途失败不能留下
    # 半截的 .db 冒充"今天的快照"。
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.unlink(missing_ok=True)
    _vacuum_into(db_path, tmp)
    tmp.replace(target)

    if commit:
        _commit_private_repo(data_dir, day)
    return target


def _vacuum_into(source: Path, target: Path) -> None:
    connection = sqlite3.connect(source)
    try:
        connection.execute("VACUUM INTO ?", (str(target),))
    finally:
        connection.close()


def gc_backups(
    data_dir: Path | str, keep_days: int = DEFAULT_KEEP_DAYS, *, clock: Clock | None = None
) -> int:
    """删掉 ``backups/state-*.db`` 里超过 ``keep_days`` 的快照，返回删除条数。

    只看这一个目录、只认这一个文件名模式：回收是"清理自己的产物"，不是"扫盘"。
    日期从**文件名**读（这是快照自己的时间戳），读不出来才退回文件 mtime。
    """
    backups = Path(data_dir) / BACKUP_DIRNAME
    if not backups.is_dir():
        return 0
    today = (clock or SystemClock()).now().astimezone().date()
    cutoff = today - timedelta(days=max(keep_days, 0))
    removed = 0
    for path in sorted(backups.glob(f"{SNAPSHOT_PREFIX}*{SNAPSHOT_SUFFIX}")):
        if not path.is_file() or path.parent.resolve() != backups.resolve():
            continue  # 解析后必须仍在 backups/ 里（第 1 层防御：路径不逃逸）
        day = _snapshot_day(path)
        if day is None or day >= cutoff:
            continue
        path.unlink()
        removed += 1
    return removed


def _snapshot_day(path: Path) -> date | None:
    stem = path.stem
    if not stem.startswith(SNAPSHOT_PREFIX):
        return None
    raw = stem[len(SNAPSHOT_PREFIX) :]
    try:
        return datetime.strptime(raw, "%Y%m%d").date()
    except ValueError:
        try:  # 名字被改过就按 mtime 算，宁可留着也不误删
            return datetime.fromtimestamp(path.stat().st_mtime).date()
        except OSError:
            return None


# ------------------------------------------------------------------ 私有仓
def _commit_private_repo(data_dir: Path, day: date) -> str:
    """``git init``（缺则建）+ 写 ``.gitignore`` + 有变化才提交；返回一句人话。"""
    git = shutil.which("git")
    if git is None:
        return "跳过私有仓提交：这台机器上没找到 git"
    gitignore = data_dir / ".gitignore"
    if not gitignore.is_file() or gitignore.read_text(encoding="utf-8") != PRIVATE_GITIGNORE:
        gitignore.write_text(PRIVATE_GITIGNORE, encoding="utf-8")
    if not (data_dir / ".git").exists():
        _git(git, data_dir, "init")
    _git(git, data_dir, "add", "-A")
    staged = _git(git, data_dir, "diff", "--cached", "--quiet", check=False)
    if staged.returncode == 0:
        return "私有仓没有变化，跳过提交"
    message = f"snapshot {day.isoformat()}"
    _git(
        git,
        data_dir,
        "-c",
        f"user.name={GIT_USER_NAME}",
        "-c",
        f"user.email={GIT_USER_EMAIL}",
        "commit",
        "-m",
        message,
    )
    return f"私有仓已提交：{message}"


def _git(git: str, cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [git, *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=check,
    )


def private_repo_summary(data_dir: Path | str, *, limit: int = 3) -> str:
    """私有仓现在长什么样（``backup`` 命令打印用）：有没有仓、最近几次提交。"""
    data_dir = Path(data_dir)
    git = shutil.which("git")
    if git is None:
        return "私有仓：本机没找到 git（快照仍然可用）"
    if not (data_dir / ".git").exists():
        return f"私有仓：{data_dir} 还没有 git 仓（下一次 backup 会建）"
    log = _git(git, data_dir, "log", f"-{max(limit, 1)}", "--pretty=%h %s", check=False)
    commits = [line.strip() for line in (log.stdout or "").splitlines() if line.strip()]
    body = " / ".join(commits) if commits else "（还没有提交）"
    return f"私有仓：{data_dir} · 最近提交 {body}"


__all__ = [
    "BACKUP_DIRNAME",
    "DEFAULT_KEEP_DAYS",
    "PRIVATE_GITIGNORE",
    "backup_now",
    "gc_backups",
    "private_repo_summary",
    "snapshot_path",
]
