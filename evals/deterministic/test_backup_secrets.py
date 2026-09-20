"""密钥可恢复性：把 ``.env`` 复制到 ``data/backups/secrets/``（TECH §12.4）。

两条纪律一起测：
  * 副本落在 ``data/`` 之内（``.gitignore`` 已挡），不新增一个要记得忽略的路径；
  * ``.env`` 不存在时返回 ``None`` 而不是造一个空文件（干净 clone 也是合法状态）。
"""

from __future__ import annotations

from yixiang.ops import backup


def test_backup_secrets_copies_env_into_the_gitignored_data_dir(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("YIXIANG_API_KEY=sk-test-not-real\n", encoding="utf-8")
    data_dir = tmp_path / "data"

    path = backup.backup_secrets(data_dir, env_file)

    assert path is not None
    assert path.is_file()
    assert path.read_text(encoding="utf-8") == env_file.read_text(encoding="utf-8")
    assert (data_dir / backup.BACKUP_DIRNAME) in path.parents
    assert path.name.startswith(".env.")


def test_backup_secrets_is_a_noop_when_there_is_no_env_file(tmp_path):
    assert backup.backup_secrets(tmp_path / "data", tmp_path / ".env") is None


def test_backup_secrets_is_idempotent_within_the_same_day(tmp_path, clock):
    env_file = tmp_path / ".env"
    env_file.write_text("YIXIANG_API_KEY=sk-one\n", encoding="utf-8")
    data_dir = tmp_path / "data"

    first = backup.backup_secrets(data_dir, env_file, clock=clock)
    env_file.write_text("YIXIANG_API_KEY=sk-two\n", encoding="utf-8")
    second = backup.backup_secrets(data_dir, env_file, clock=clock)

    assert first == second  # 同一天只留一份，覆盖即可
    assert second.read_text(encoding="utf-8") == "YIXIANG_API_KEY=sk-two\n"
